"""Real-market data source endpoints.

Kept separate from `/api/cn` (rules/session) so the data plane can be enabled,
disabled or swapped without touching trading semantics.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_market, settings_of
from app.core.cache import invalidate_symbol_caches
from app.modules.cn.sources.base import SourceUnavailable
from app.modules.cn.sources.tencent import TencentSource
from app.modules.cn.sync import CnDataSyncService


router = APIRouter(prefix="/cn/data", tags=["cn_stock"])


def _source(request: Request):
    """The configured live source, or None when running synthetic-only."""
    settings = settings_of(request)
    if settings.cn_data_source == "tencent":
        return TencentSource(timeout=settings.request_timeout_seconds)
    return None


@router.get("/source")
def source_status(request: Request):
    settings = settings_of(request)
    source = _source(request)
    if source is None:
        return {
            "configured": settings.cn_data_source,
            "available": False,
            "detail": "synthetic 模式：使用本地合成行情，不访问外网",
            "latency_ms": 0.0,
        }
    health = source.health()
    return {
        "configured": settings.cn_data_source,
        "available": health.available,
        "detail": health.detail,
        "latency_ms": health.latency_ms,
    }


@router.post("/sync")
def sync(
    request: Request,
    codes: str | None = Query(default=None, description="逗号分隔，缺省同步全部 A 股标的"),
    daily_limit: int = Query(default=0, ge=0, le=2000),
    intraday_limit: int = Query(default=0, ge=0, le=1000),
    session: Session = Depends(get_session),
):
    settings = settings_of(request)
    source = _source(request)
    if source is None:
        raise SourceUnavailable(
            "未启用真实数据源：设置 LQ_CN_DATA_SOURCE=tencent 后重试"
        )
    service = CnDataSyncService(session, source)
    report = service.sync_symbols(
        [code.strip().upper() for code in codes.split(",") if code.strip()] if codes else None,
        daily_limit=daily_limit or settings.cn_sync_daily_limit,
        intraday_limit=intraday_limit or settings.cn_sync_intraday_limit,
    )
    # Sync rewrites bars, closes and quote fields for every symbol it touched, so
    # every cached view of them is now wrong. Without this the quality panel and
    # the K-line chart keep showing the pre-sync series for up to 30 seconds —
    # exactly when the user is looking at them to confirm the sync worked.
    for item in report.symbols:
        if not item.error:
            invalidate_symbol_caches(request.app.state, item.code)
    return report.as_dict()


@router.get("/staleness")
def staleness(request: Request, session: Session = Depends(get_session)):
    """How far each symbol's stored daily series lags today."""
    market = build_market(request, session)
    settings = settings_of(request)
    service = CnDataSyncService(session, TencentSource() if settings.cn_data_source == "tencent" else _NullSource())
    rows = []
    for view in market.list_symbols(market="cn_stock"):
        rows.append(
            {
                "code": view.code,
                "name": view.name,
                "latest_daily": service.latest_local_bar(view.code, "1d"),
                "stale_days": service.staleness_days(view.code),
            }
        )
    return rows


class _NullSource:
    """Placeholder so staleness works in synthetic mode (no network calls)."""

    name = "synthetic"
