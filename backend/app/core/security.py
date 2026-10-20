"""Optional bearer-token auth and a fixed-window rate limiter.

Defaults keep local single-user usage frictionless (`auth_enabled=False`, generous
limits). Both switch on via environment for shared deployments:

    LQ_AUTH_ENABLED=true
    LQ_AUTH_TOKEN=<long random string>
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, token: str, enabled: bool) -> None:
        super().__init__(app)
        self.token = token
        self.enabled = enabled

    async def dispatch(self, request: Request, call_next):
        if not self.enabled or request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        # Constant-time comparison; an empty configured token never matches.
        if not self.token or not hmac.compare_digest(supplied, self.token):
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "unauthorized", "message": "missing or invalid token"}},
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window limiter keyed by client IP.

    Intentionally simple: per-process state, no Redis. Adequate for a single-node
    deployment, and it fails open only when the limiter is disabled.
    """

    def __init__(self, app, *, limit: int = 240, window_seconds: float = 60.0) -> None:
        super().__init__(app)
        self.limit = max(0, limit)
        self.window = window_seconds
        self._hits: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))

    async def dispatch(self, request: Request, call_next):
        if self.limit <= 0 or request.url.path in {"/health"}:
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window_start, count = self._hits[client]
        if now - window_start >= self.window:
            window_start, count = now, 0
        count += 1
        self._hits[client] = (window_start, count)
        if count > self.limit:
            retry = max(1, int(self.window - (now - window_start)))
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": f"too many requests; retry in {retry}s",
                    }
                },
                headers={"Retry-After": str(retry)},
            )
        return await call_next(request)
