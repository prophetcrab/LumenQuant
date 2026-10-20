"""A-share market information: rules, session state, calendar."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_market, cn_fee_schedule, settings_of
from app.core.errors import ValidationError
from app.modules.cn.calendar import (
    is_trading_day,
    next_trading_day,
    prev_trading_day,
    session_state,
    trading_days,
)
from app.modules.cn.rules import BOARD_LIMITS, CN_LOT, ST_LIMIT


router = APIRouter(prefix="/cn", tags=["cn_stock"])


@router.get("/rules")
def rules(request: Request, session: Session = Depends(get_session)):
    settings = settings_of(request)
    return {
        "lot_size": CN_LOT,
        "boards": {name: {"limit_pct": pct} for name, pct in BOARD_LIMITS.items()},
        "st_limit_pct": ST_LIMIT,
        "t_plus_one": True,
        "settlement": "T+1（当日买入次日可卖）",
        "sessions": [{"open": "09:30", "close": "11:30"}, {"open": "13:00", "close": "15:00"}],
        "fees": cn_fee_schedule(settings).as_dict(),
        "session_enforced_for_paper": settings.cn_session_enforced,
        "symbols": len(build_market(request, session).list_symbols(market="cn_stock")),
    }


@router.get("/session")
def session(request: Request):
    engine = getattr(request.app.state, "cn_market_engine", None)
    if engine is not None:
        return engine.clock.info()
    return session_state().as_dict()


@router.get("/calendar")
def calendar(
    days: int = Query(default=30, ge=1, le=400),
):
    today = date.today()
    upcoming = trading_days(today, today + timedelta(days=days * 2))[:days]
    return {
        "today": today.isoformat(),
        "is_trading_day": is_trading_day(today),
        "prev_trading_day": prev_trading_day(today).isoformat(),
        "next_trading_day": next_trading_day(today).isoformat(),
        "upcoming": [day.isoformat() for day in upcoming],
    }


@router.get("/symbols")
def symbols(request: Request, session: Session = Depends(get_session)):
    market = build_market(request, session)
    rows = market.list_symbols(market="cn_stock")
    out = []
    for view in rows:
        try:
            snap = market.snapshot(view.code)
        except Exception:
            snap = None
        out.append(
            {
                **asdict(view),
                "price": snap.price if snap else 0.0,
                "change_pct": snap.change_24h_pct if snap else 0.0,
                "limit_up": snap.limit_up if snap else 0.0,
                "limit_down": snap.limit_down if snap else 0.0,
            }
        )
    return out


@router.post("/symbols/{code}/suspend")
def suspend(code: str, suspended: bool, request: Request, session: Session = Depends(get_session)):
    if not code.upper().endswith((".SH", ".SZ", ".BJ")):
        raise ValidationError("A股代码需带交易所后缀，例如 600519.SH")
    return asdict(build_market(request, session).set_suspended(code, suspended))


@router.post("/settle")
def settle(request: Request, session: Session = Depends(get_session)):
    """Release T+1 holds for every A-share position."""
    from app.api.wiring import build_trading

    return {"unlocked": build_trading(request, session).settle_t1()}
