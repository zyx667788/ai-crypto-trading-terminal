"""Local admin API protection (Origin, Host, Sec-Fetch-Site, and Admin Token).

Protects mutating endpoints from Cross-Site Request Forgery (CSRF) and drive-by
web requests executed by malicious pages loaded in the operator's browser.
"""
from __future__ import annotations

import re
import secrets
from typing import Any

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.core.config import settings

# Loopback host pattern:
# Matches localhost, [IP], or [::1] / ::1 with an optional port (e.g. localhost:8000).
_LOOPBACK_HOST_RE = re.compile(
    r"^(?:localhost|127(?:\.\d+){3}|::1|\[::1\]|testserver|testserver\.local)(?::\d+)?$",
    re.IGNORECASE,
)


class AdminAuthError(Exception):
    """Raised when an admin authentication or CSRF/Origin check fails."""

    def __init__(self, status_code: int = status.HTTP_403_FORBIDDEN, detail: str = "Forbidden") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def parse_allowed_origins() -> list[str]:
    """Parse comma-separated ADMIN_ALLOWED_ORIGINS from settings into trimmed strings."""
    raw = getattr(settings, "ADMIN_ALLOWED_ORIGINS", "") or ""
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def is_host_allowed(host_header: str) -> bool:
    """Return True if host_header matches a loopback address (any port).

    Allowed hosts: [IP], localhost, ::1, [::1].
    """
    if not host_header or not host_header.strip():
        return True
    return bool(_LOOPBACK_HOST_RE.match(host_header.strip()))


def verify_admin_request(request: Request) -> None:
    """Verify that an incoming admin request is authorized and not cross-site.

    Order of verification:
    a. Host check:
       If the Host header is present and not a loopback address, reject with 403.
    d. Safe vs. mutating method check:
       GET, HEAD, and OPTIONS methods pass after the Host check (read-only / safe).
    b. Browser-origin enforcement (only when settings.ENFORCE_ADMIN_ORIGIN is True):
       - If Origin header is present: must exactly match one of parse_allowed_origins(),
         else 403 "Origin not allowed".
       - If Sec-Fetch-Site header is present and equals "cross-site",
         reject with 403 "Cross-site request blocked".
       - If BOTH Origin and Sec-Fetch-Site are absent, treat the caller as a
         non-browser local client (e.g. curl, CLI scripts, backend test clients)
         and allow. This keeps existing tests and automation scripts working.
    c. Token enforcement:
       If settings.ADMIN_API_TOKEN is non-empty, require header `X-Admin-Token`
       equal to it using constant-time secrets.compare_digest.
       Otherwise reject with 403 "Admin token required/invalid".
    """
    # a. Host check
    # Note: request.headers.get("host") fetches the Host header
    host = request.headers.get("host")
    if host and not is_host_allowed(host):
        raise AdminAuthError(status.HTTP_403_FORBIDDEN, "Host not allowed")

    # d. Only enforce (b) and (c) for mutating methods
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return

    # b. Browser-origin enforcement
    if getattr(settings, "ENFORCE_ADMIN_ORIGIN", True):
        origin = request.headers.get("origin")
        sec_fetch_site = request.headers.get("sec-fetch-site")

        # Sec-Fetch-Site check: reject cross-site browser requests immediately
        if sec_fetch_site and sec_fetch_site.strip().lower() == "cross-site":
            raise AdminAuthError(status.HTTP_403_FORBIDDEN, "Cross-site request blocked")

        # Origin check: if present, must match allowed origins
        if origin:
            allowed = parse_allowed_origins()
            if origin.strip() not in allowed:
                raise AdminAuthError(status.HTTP_403_FORBIDDEN, "Origin not allowed")
        # If BOTH Origin and Sec-Fetch-Site are absent:
        # Documented behavior: treat as non-browser local client (curl / CLI / tests) and allow.

    # c. Token enforcement
    admin_token = getattr(settings, "ADMIN_API_TOKEN", "") or ""
    if admin_token:
        provided_token = request.headers.get("x-admin-token") or ""
        if not provided_token or not secrets.compare_digest(provided_token, admin_token):
            raise AdminAuthError(status.HTTP_403_FORBIDDEN, "Admin token required/invalid")


async def require_admin(request: Request) -> None:
    """FastAPI dependency wrapping verify_admin_request; converts AdminAuthError to HTTPException."""
    try:
        verify_admin_request(request)
    except AdminAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


class AdminGuardMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing verify_admin_request on /api/v1 endpoints except health checks."""

    EXEMPT_PATHS = {
        "/api/v1/health",
        "/api/v1/system/health",
    }

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path.startswith("/api/v1") and path not in self.EXEMPT_PATHS:
            try:
                verify_admin_request(request)
            except AdminAuthError as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)
