"""FastAPI entrypoint for the dashboard with auth and secure exposure guards."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from dashboard.auth import (
    clear_session_cookie,
    csrf_token_for_session,
    extract_csrf_token,
    unauthorized_dashboard_response,
    verify_signed_session,
)
from dashboard.config import load_dashboard_config, validate_dashboard_config
from dashboard.routes import router


logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class DashboardAuthMiddleware(BaseHTTPMiddleware):
    """Protect private dashboard routes with a signed Zach session."""

    async def dispatch(self, request: Request, call_next):
        config = getattr(request.app.state, "dashboard_config", None)
        if config is None:
            config = validate_dashboard_config(load_dashboard_config())
            request.app.state.dashboard_config = config
        session_cookie = request.cookies.get(config.cookie_name)
        session = verify_signed_session(session_cookie, config.secret_key)
        request.state.dashboard_config = config
        request.state.dashboard_session = session
        request.state.dashboard_actor = session.actor if session else None
        request.state.dashboard_authenticated = session is not None
        request.state.dashboard_csrf_token = (
            csrf_token_for_session(config.secret_key, session.nonce)
            if session is not None
            else ""
        )
        request.state.dashboard_session_nonce = session.nonce if session else None

        if _is_public_path(request.url.path):
            response = await call_next(request)
            if session_cookie and session is None:
                clear_session_cookie(response, config)
            return response

        if session is None:
            response = unauthorized_dashboard_response(request)
            if session_cookie:
                clear_session_cookie(response, config)
            return response

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            actual = await extract_csrf_token(request)
            expected = request.state.dashboard_csrf_token
            if not actual or not secrets.compare_digest(actual, expected):
                return unauthorized_csrf_response()

        return await call_next(request)


def unauthorized_csrf_response():
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse("CSRF validation failed", status_code=403)


def create_app() -> FastAPI:
    """Create the dashboard app with runtime-loaded config and auth middleware."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = validate_dashboard_config(load_dashboard_config())
        app.state.dashboard_config = config
        logger.info(
            "Dashboard startup: exposure=%s host=%s port=%s auth=%s secure_cookies=%s",
            config.exposure,
            config.host,
            config.port,
            "enabled" if config.auth_enabled else "disabled",
            config.secure_cookies,
        )
        if config.exposure == "lan":
            logger.warning(
                "Dashboard LAN mode is enabled for %s:%s. LAN mode is a fallback, not the preferred mobile path.",
                config.host,
                config.port,
            )
        yield
        logger.info("Dashboard shutdown")

    app = FastAPI(title="Purcival Goals Dashboard", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_middleware(DashboardAuthMiddleware)
    app.include_router(router)
    return app
def _is_public_path(path: str) -> bool:
    return path.startswith("/static/") or path in {"/login"}


app = create_app()
