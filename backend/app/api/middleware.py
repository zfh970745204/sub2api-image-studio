from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.domain.ids import uuid7
from app.services.logging import (
    bind_request_context,
    bind_security_context,
    reset_request_context,
    reset_security_context,
)

logger = logging.getLogger("app.access")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else uuid7().hex
        request.state.request_id = request_id
        token = bind_request_context(request_id)
        client_ip = request.client.host if request.client else "unknown"
        auth_service = getattr(request.app.state, "auth_service", None)
        ip_hash = auth_service.fingerprint(client_ip) if auth_service else "0" * 64
        request.state.ip_hash = ip_hash
        security_tokens = bind_security_context(
            ip_hash, request.headers.get("user-agent", "unknown")
        )
        started = time.perf_counter()
        status_code = 500
        try:
            if self._csrf_rejected(request):
                response = JSONResponse(
                    status_code=403,
                    content={
                        "code": "CROSS_SITE_REQUEST_REJECTED",
                        "message": "请求来源校验失败",
                        "details": None,
                        "request_id": request_id,
                    },
                )
            else:
                response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
                "form-action 'self'; object-src 'none'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; "
                "connect-src 'self'"
            )
            if request.app.state.settings.production:
                response.headers["Strict-Transport-Security"] = (
                    "max-age=31536000; includeSubDomains"
                )
            if request.url.path.startswith(
                ("/api/v1/auth", "/api/v1/admin", "/api/v1/app", "/api/v1/me")
            ):
                response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            logger.info(
                "request completed",
                extra={
                    "operation": f"{request.method} {request.url.path}",
                    "user_id": str(request.state.principal.user_id)
                    if hasattr(request.state, "principal")
                    else None,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "status": status_code,
                },
            )
            reset_request_context(token)
            reset_security_context(security_tokens)

    @staticmethod
    def _csrf_rejected(request: Request) -> bool:
        settings = request.app.state.settings
        if (
            not settings.production
            or request.method not in {"POST", "PUT", "PATCH", "DELETE"}
            or not request.url.path.startswith("/api/")
            or settings.session_cookie_name not in request.cookies
        ):
            return False
        source = request.headers.get("origin") or request.headers.get("referer")
        if not source:
            return True
        expected = urlsplit(settings.public_app_url)
        actual = urlsplit(source)
        return (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc)
