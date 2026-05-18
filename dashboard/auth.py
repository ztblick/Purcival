"""Password, session, CSRF, and login-rate helpers for the dashboard."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from dashboard.config import DashboardConfig


SESSION_VERSION = 1
SESSION_ACTOR = "zach_dashboard"
PBKDF2_NAME = "pbkdf2_sha256"


@dataclass(frozen=True)
class DashboardSession:
    """Verified dashboard session extracted from the signed cookie."""

    actor: str
    nonce: str
    issued_at: int
    expires_at: int


class LoginRateLimiter:
    """Small in-memory failure limiter keyed by client host."""

    def __init__(self) -> None:
        self._attempts: dict[str, list[float]] = {}

    def is_locked(
        self,
        client_host: str,
        max_failures: int,
        window_seconds: int,
        now: float | None = None,
    ) -> bool:
        entries = self._prune(client_host, window_seconds, now)
        return len(entries) >= max_failures

    def register_failure(
        self,
        client_host: str,
        window_seconds: int,
        now: float | None = None,
    ) -> None:
        current = now if now is not None else time.time()
        entries = self._prune(client_host, window_seconds, current)
        entries.append(current)
        self._attempts[client_host] = entries

    def clear(self, client_host: str) -> None:
        self._attempts.pop(client_host, None)

    def _prune(
        self,
        client_host: str,
        window_seconds: int,
        now: float | None = None,
    ) -> list[float]:
        current = now if now is not None else time.time()
        threshold = current - window_seconds
        entries = [value for value in self._attempts.get(client_host, []) if value >= threshold]
        if entries:
            self._attempts[client_host] = entries
        else:
            self._attempts.pop(client_host, None)
        return entries


LOGIN_LIMITER = LoginRateLimiter()


def hash_dashboard_password(
    password: str,
    *,
    iterations: int = 600_000,
    salt: bytes | None = None,
) -> str:
    """Return a PBKDF2-HMAC-SHA256 hash string for dashboard login."""
    if not password:
        raise ValueError("Password cannot be empty")
    chosen_salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        chosen_salt,
        iterations,
    )
    salt_b64 = base64.b64encode(chosen_salt).decode("ascii")
    hash_b64 = base64.b64encode(derived).decode("ascii")
    return f"{PBKDF2_NAME}${iterations}${salt_b64}${hash_b64}"


def verify_dashboard_password(password: str, stored_hash: str) -> bool:
    """Verify a dashboard password hash, failing closed on malformed values."""
    try:
        algorithm, iterations_text, salt_b64, digest_b64 = stored_hash.split("$", 3)
        if algorithm != PBKDF2_NAME:
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_b64.encode("ascii"), validate=True)
        expected = base64.b64decode(digest_b64.encode("ascii"), validate=True)
    except (TypeError, ValueError, binascii.Error, AttributeError):
        return False

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return secrets.compare_digest(derived, expected)


def create_signed_session(
    secret_key: str,
    *,
    session_days: int,
    actor: str = SESSION_ACTOR,
    now: int | None = None,
) -> str:
    """Create a signed cookie payload for the dashboard session."""
    issued_at = now or int(time.time())
    payload = {
        "v": SESSION_VERSION,
        "sub": actor,
        "iat": issued_at,
        "exp": issued_at + (session_days * 24 * 60 * 60),
        "nonce": secrets.token_urlsafe(24),
    }
    payload_b64 = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(
        secret_key.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{signature}"


def verify_signed_session(
    token: str | None,
    secret_key: str,
    *,
    now: int | None = None,
) -> DashboardSession | None:
    """Validate the signed dashboard session cookie."""
    if not token or "." not in token:
        return None
    payload_b64, signature = token.split(".", 1)
    expected = hmac.new(
        secret_key.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not secrets.compare_digest(signature, expected):
        return None

    try:
        payload = json.loads(_urlsafe_b64decode(payload_b64).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    current = now or int(time.time())
    if payload.get("v") != SESSION_VERSION:
        return None
    if payload.get("sub") != SESSION_ACTOR:
        return None
    if not isinstance(payload.get("nonce"), str):
        return None
    issued_at = int(payload.get("iat", 0))
    expires_at = int(payload.get("exp", 0))
    if expires_at <= current or issued_at <= 0:
        return None
    return DashboardSession(
        actor=SESSION_ACTOR,
        nonce=payload["nonce"],
        issued_at=issued_at,
        expires_at=expires_at,
    )


def csrf_token_for_session(secret_key: str, nonce: str) -> str:
    """Derive a stable CSRF token from the signed session nonce."""
    return hmac.new(
        secret_key.encode("utf-8"),
        f"csrf:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def set_session_cookie(
    response: Response,
    config: DashboardConfig,
    token: str,
) -> None:
    """Attach the signed dashboard session cookie to a response."""
    response.set_cookie(
        key=config.cookie_name,
        value=token,
        max_age=config.session_days * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=config.secure_cookies,
        path="/",
    )


def clear_session_cookie(response: Response, config: DashboardConfig) -> None:
    """Remove the dashboard session cookie from the browser."""
    response.delete_cookie(
        key=config.cookie_name,
        httponly=True,
        samesite="lax",
        secure=config.secure_cookies,
        path="/",
    )


async def extract_csrf_token(request: Request) -> str | None:
    """Read a CSRF token from the header or submitted form body."""
    header_token = request.headers.get("X-CSRF-Token")
    if header_token:
        return header_token
    content_type = request.headers.get("content-type", "").lower()
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        token = form.get("csrf_token")
        return str(token) if token else None
    return None


def client_host(request: Request) -> str:
    """Return the direct client host without trusting identity headers."""
    return request.client.host if request.client else "unknown"


def sanitize_next_path(next_path: str | None) -> str:
    """Keep post-login redirects on this app only."""
    if not next_path:
        return "/"
    parsed = urlsplit(next_path)
    if parsed.scheme or parsed.netloc:
        return "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


def unauthorized_dashboard_response(request: Request) -> Response:
    """Return a redirect or 401 depending on the route type."""
    path = request.url.path
    if request.method in {"GET", "HEAD"} and not path.startswith("/chat/streams/"):
        next_path = quote(path, safe="/?=&")
        return RedirectResponse(url=f"/login?next={next_path}", status_code=303)
    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept:
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return PlainTextResponse("Authentication required", status_code=401)


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * ((4 - (len(value) % 4)) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))
