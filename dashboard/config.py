"""Runtime configuration for the dashboard auth and exposure layer."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


ALLOWED_EXPOSURE_MODES = {"local", "tailscale", "lan"}


class DashboardConfigError(RuntimeError):
    """Raised when the dashboard runtime configuration is unsafe."""


@dataclass(frozen=True)
class DashboardConfig:
    """Centralized dashboard settings loaded from the environment."""

    exposure: str
    host: str
    port: int
    public_base_url: str | None
    password_hash: str
    secret_key: str
    session_days: int
    goals_db: Path | None
    memory_data_dir: Path | None
    persona: str
    provider: str | None
    fake_response: str | None
    cookie_name: str = "purcival_dashboard_session"
    login_limit_window_seconds: int = 900
    login_limit_failures: int = 5

    @property
    def auth_enabled(self) -> bool:
        return bool(self.password_hash and self.secret_key)

    @property
    def secure_cookies(self) -> bool:
        if self.public_base_url:
            parsed = urlparse(self.public_base_url)
            return parsed.scheme.lower() == "https"
        return False


def load_dashboard_config(environ: dict[str, str] | None = None) -> DashboardConfig:
    """Load dashboard settings from environment variables."""
    env = environ or os.environ
    public_base_url = (env.get("PURCIVAL_DASHBOARD_PUBLIC_BASE_URL") or "").strip() or None
    goals_db = (env.get("PURCIVAL_GOALS_DB") or "").strip() or None
    memory_data_dir = (env.get("PURCIVAL_MEMORY_DATA_DIR") or "").strip() or None
    return DashboardConfig(
        exposure=(env.get("PURCIVAL_DASHBOARD_EXPOSURE") or "local").strip().lower(),
        host=(env.get("PURCIVAL_DASHBOARD_HOST") or "127.0.0.1").strip(),
        port=_parse_int(env.get("PURCIVAL_DASHBOARD_PORT"), default=8000),
        public_base_url=public_base_url,
        password_hash=(env.get("PURCIVAL_DASHBOARD_PASSWORD_HASH") or "").strip(),
        secret_key=(env.get("PURCIVAL_DASHBOARD_SECRET_KEY") or "").strip(),
        session_days=_parse_int(env.get("PURCIVAL_DASHBOARD_SESSION_DAYS"), default=30),
        goals_db=Path(goals_db) if goals_db else None,
        memory_data_dir=Path(memory_data_dir) if memory_data_dir else None,
        persona=(env.get("PURCIVAL_DASHBOARD_PERSONA") or "jo").strip().lower(),
        provider=(env.get("PURCIVAL_DASHBOARD_PROVIDER") or "").strip() or None,
        fake_response=(env.get("PURCIVAL_DASHBOARD_FAKE_RESPONSE") or "").strip() or None,
    )


def validate_dashboard_config(config: DashboardConfig) -> DashboardConfig:
    """Fail closed on unsafe or incomplete dashboard startup settings."""
    if config.exposure not in ALLOWED_EXPOSURE_MODES:
        choices = ", ".join(sorted(ALLOWED_EXPOSURE_MODES))
        raise DashboardConfigError(
            f"PURCIVAL_DASHBOARD_EXPOSURE must be one of: {choices}"
        )

    if config.port < 1 or config.port > 65535:
        raise DashboardConfigError("PURCIVAL_DASHBOARD_PORT must be between 1 and 65535")

    if config.session_days < 1:
        raise DashboardConfigError("PURCIVAL_DASHBOARD_SESSION_DAYS must be at least 1")

    if not config.password_hash:
        raise DashboardConfigError("PURCIVAL_DASHBOARD_PASSWORD_HASH is required")

    if len(config.secret_key) < 32:
        raise DashboardConfigError(
            "PURCIVAL_DASHBOARD_SECRET_KEY must be set and at least 32 characters long"
        )

    host_is_loopback = is_loopback_host(config.host)
    if config.exposure == "local" and not host_is_loopback:
        raise DashboardConfigError("Local exposure must bind to a loopback host")

    if config.exposure == "tailscale":
        if not host_is_loopback:
            raise DashboardConfigError("Tailscale exposure must keep Uvicorn on loopback")
        if not config.public_base_url:
            raise DashboardConfigError(
                "PURCIVAL_DASHBOARD_PUBLIC_BASE_URL is required for tailscale exposure"
            )
        parsed = urlparse(config.public_base_url)
        if parsed.scheme.lower() != "https":
            raise DashboardConfigError(
                "Tailscale exposure requires an https PURCIVAL_DASHBOARD_PUBLIC_BASE_URL"
            )

    if config.exposure == "lan" and host_is_loopback:
        raise DashboardConfigError("LAN exposure must use a non-loopback host")

    return config


def is_loopback_host(host: str) -> bool:
    """Return True when a host is clearly loopback-only."""
    lowered = host.strip().lower()
    if lowered == "localhost":
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _parse_int(value: str | None, default: int) -> int:
    if value is None or not str(value).strip():
        return default
    return int(str(value).strip())
