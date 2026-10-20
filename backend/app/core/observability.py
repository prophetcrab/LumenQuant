"""Request tracing middleware and in-process metrics.

Deliberately dependency-free: a dict of counters plus per-route latency buckets
is enough to answer "what is slow / failing" for a single-node platform.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("lumenquant.access")


@dataclass
class RouteStats:
    count: int = 0
    errors: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    def observe(self, elapsed_ms: float, failed: bool) -> None:
        self.count += 1
        self.total_ms += elapsed_ms
        self.max_ms = max(self.max_ms, elapsed_ms)
        if failed:
            self.errors += 1

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "errors": self.errors,
            "avg_ms": round(self.total_ms / self.count, 2) if self.count else 0.0,
            "max_ms": round(self.max_ms, 2),
        }


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.time)
    routes: dict[str, RouteStats] = field(default_factory=lambda: defaultdict(RouteStats))
    status_codes: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def observe(self, method: str, path: str, status: int, elapsed_ms: float) -> None:
        key = f"{method} {path}"
        self.routes[key].observe(elapsed_ms, status >= 500)
        self.status_codes[status] += 1

    def snapshot(self) -> dict:
        total = sum(stats.count for stats in self.routes.values())
        errors = sum(stats.errors for stats in self.routes.values())
        slowest = sorted(
            ((key, stats.as_dict()) for key, stats in self.routes.items()),
            key=lambda item: item[1]["max_ms"],
            reverse=True,
        )[:10]
        return {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "requests": total,
            "server_errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "status_codes": dict(sorted(self.status_codes.items())),
            "slowest_routes": slowest,
        }


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, metrics: Metrics) -> None:
        super().__init__(app)
        self.metrics = metrics

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - start) * 1000
            self.metrics.observe(request.method, request.url.path, 500, elapsed)
            logger.exception(
                "request failed id=%s method=%s path=%s elapsed_ms=%.1f",
                request_id,
                request.method,
                request.url.path,
                elapsed,
            )
            raise
        elapsed = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        self.metrics.observe(request.method, request.url.path, response.status_code, elapsed)
        if request.url.path not in {"/health", "/api/system/metrics"}:
            logger.info(
                "id=%s method=%s path=%s status=%s elapsed_ms=%.1f",
                request_id,
                request.method,
                request.url.path,
                response.status_code,
                elapsed,
            )
        return response
