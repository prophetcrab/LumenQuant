"""Real-market data source endpoints.

Kept separate from `/api/cn` (rules/session) so the data plane can be enabled,
disabled or swapped without touching trading semantics.
"""

from __future__ import annotations


from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_market, settings_of
from app.core.cache import invalidate_symbol_caches
from app.core.errors import ConflictError
from app.modules.cn.sources.base import SourceUnavailable
from app.modules.cn.sources.tencent import TencentSource
from app.modules.cn.sync import CnDataSyncService
from app.modules.cn.watchlist import build_watchlist
from app.modules.screener.service import ScreenerService
from app.modules.screener.sources.sina import SinaMarketSource


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
    engine = getattr(request.app.state, "cn_quote_engine", None)
    if engine is not None:
        health = engine.health()
        return {
            "configured": settings.cn_data_source,
            "available": not health["last_error"],
            "detail": "实时轮询中"
            if not health["last_error"]
            else f"最近一次轮询失败：{health['last_error']}",
            "latency_ms": health["latency_ms"],
            "mode": health["mode"],
            "poll_seconds": health["poll_seconds"],
            "quotes": health["quotes"],
            "last_poll_age_ms": health["last_poll_age_ms"],
            "session_open": health["session_open"],
        }
    source = _source(request)
    if source is None:
        return {
            "configured": settings.cn_data_source,
            "available": False,
            "detail": "synthetic 模式：使用本地合成行情，不访问外网",
            "latency_ms": 0.0,
            "mode": "synthetic",
        }
    health = source.health()
    return {
        "configured": settings.cn_data_source,
        "available": health.available,
        "detail": health.detail,
        "latency_ms": health.latency_ms,
        "mode": "on-demand",
    }


@router.get("/quotes")
def quotes(request: Request):
    """Latest polled quotes with their age.

    `fresh=false` is reported rather than hidden: a stale price that looks live is
    worse than an obvious gap, especially for anything a trader reads off screen.
    """
    engine = getattr(request.app.state, "cn_quote_engine", None)
    if engine is None:
        # 409, not 502: nothing upstream failed — the feature is switched off.
        # Reporting "bad gateway" sent operators looking at the network for a
        # problem that is one env var away (and made the two markets answer the
        # same situation with different status codes).
        raise ConflictError(
            "实时报价未启用：设置 LQ_CN_DATA_SOURCE=tencent 后重启"
        )
    return {"health": engine.health(), "quotes": engine.snapshot_rows()}


@router.post("/poll")
def poll_now(request: Request):
    """Force one poll; useful right after starting the app or after a network gap."""
    engine = getattr(request.app.state, "cn_quote_engine", None)
    if engine is None:
        # 409, not 502: nothing upstream failed — the feature is switched off.
        # Reporting "bad gateway" sent operators looking at the network for a
        # problem that is one env var away (and made the two markets answer the
        # same situation with different status codes).
        raise ConflictError(
            "实时报价未启用：设置 LQ_CN_DATA_SOURCE=tencent 后重启"
        )
    updated = engine.poll_once()
    return {"updated": updated, "health": engine.health()}


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
    # The purge replaced every bar the poller had been accumulating against, so
    # its in-memory live state now refers to deleted timestamps. Dropping it makes
    # the next poll rebuild from the imported history instead of re-emitting a
    # bar that already exists with different volume.
    engine = getattr(request.app.state, "cn_quote_engine", None)
    if engine is not None:
        engine.reset_live()
    return report.as_dict()


@router.get("/watchlist")
def watchlist(
    request: Request,
    codes: str | None = Query(default=None, description="逗号分隔的代码；省略则全部已登记标的"),
    limit: int = Query(default=200, ge=1, le=500),
    session: Session = Depends(get_session),
):
    """One row per symbol with every field this screen can actually obtain.

    Merges three upstreams (realtime quote, whole-market snapshot, local candles)
    and reports which field groups are unavailable *with a reason*, so a missing
    column is never mistaken for a zero.
    """
    settings = settings_of(request)
    market = build_market(request, session)
    screener = ScreenerService(
        SinaMarketSource(timeout=settings.request_timeout_seconds),
        market=market,
        cache=getattr(request.app.state, "screener_cache", None),
    )
    engine = getattr(request.app.state, "cn_quote_engine", None)
    quote_rows = engine.snapshot_rows() if engine is not None else None
    code_list = [item for item in (codes or "").split(",") if item.strip()] or None
    return build_watchlist(
        session=session,
        market=market,
        screener=screener,
        quote_rows=quote_rows,
        codes=code_list,
        limit=limit,
    )


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
