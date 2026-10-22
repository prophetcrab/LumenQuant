"""Crypto realtime data endpoints.

Mirrors `/cn/data/*` so the two markets can be inspected the same way. Kept in
the market module because it only concerns price data, not trading rules.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import settings_of
from app.core.cache import invalidate_symbol_caches
from app.core.errors import AppError
from app.modules.market.models import Candle, Symbol


router = APIRouter(prefix="/market/data", tags=["market"])


class CryptoDataDisabled(AppError):
    status_code = 409
    code = "crypto_data_disabled"


def _engine(request: Request):
    from app.modules.market.crypto_quotes import CryptoQuoteEngine

    engine = getattr(request.app.state, "market_engine", None)
    return engine if isinstance(engine, CryptoQuoteEngine) else None


@router.get("/source")
def source_status(request: Request):
    settings = settings_of(request)
    engine = _engine(request)
    if engine is None:
        return {
            "configured": settings.crypto_data_source,
            "mode": "synthetic",
            "available": False,
            "detail": "synthetic 模式：使用本地合成行情，不访问外网",
            "latency_ms": 0.0,
        }
    health = engine.health()
    return {
        "configured": settings.crypto_data_source,
        "mode": health["mode"],
        "available": not health["last_error"],
        "detail": "实时轮询中" if not health["last_error"] else f"最近一次轮询失败：{health['last_error']}",
        "latency_ms": health["latency_ms"],
        "poll_seconds": health["poll_seconds"],
        "symbols": health["symbols"],
        "last_poll_age_ms": health["last_poll_age_ms"],
    }


@router.get("/quotes")
def quotes(request: Request):
    engine = _engine(request)
    if engine is None:
        raise CryptoDataDisabled(
            "实时行情未启用：设置 LQ_CRYPTO_DATA_SOURCE=binance 后重启"
        )
    return {"health": engine.health(), "quotes": engine.rows()}


@router.post("/poll")
def poll_now(request: Request):
    engine = _engine(request)
    if engine is None:
        raise CryptoDataDisabled(
            "实时行情未启用：设置 LQ_CRYPTO_DATA_SOURCE=binance 后重启"
        )
    updated = engine.poll_once()
    return {"updated": updated, "health": engine.health()}


@router.post("/sync")
def sync_history(request: Request, session: Session = Depends(get_session)):
    """Re-import Binance history, replacing the stored series.

    Exposed because the startup import is best-effort: after a network outage the
    database can be missing days, and re-running the whole import is the only way
    to fill the gap without a manual script.
    """
    engine = _engine(request)
    if engine is None:
        raise CryptoDataDisabled(
            "实时行情未启用：设置 LQ_CRYPTO_DATA_SOURCE=binance 后重启"
        )
    imported = engine.sync_history(force=True)
    codes = [
        row.code
        for row in session.scalars(select(Symbol).where(Symbol.market == "crypto")).all()
    ]
    for code in codes:
        invalidate_symbol_caches(request.app.state, code)
    rows = {
        code: int(
            session.scalar(
                select(func.count(Candle.id)).where(Candle.symbol == code)
            )
            or 0
        )
        for code in codes
    }
    return {"imported": imported, "symbols": rows}
