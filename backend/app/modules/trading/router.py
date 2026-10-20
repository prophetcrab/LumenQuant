from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.modules.accounts.service import AccountService
from app.modules.market.service import MarketService
from app.modules.risk.policy import RiskLimits, RiskPolicy
from app.modules.risk.service import RiskService
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.schemas import ClosePositionRequest, OrderCreate
from app.modules.trading.service import TradingService


router = APIRouter(prefix="/trading", tags=["trading"])


def build_service(request: Request, session: Session) -> TradingService:
    settings = request.app.state.settings
    engine = getattr(request.app.state, "market_engine", None)
    accounts = AccountService(
        session, live_trading_enabled=settings.live_trading_enabled
    )
    market = MarketService(
        session, clock=getattr(engine, "clock", None), live=engine if engine else None
    )
    limits = RiskLimits(
        max_order_notional=settings.risk_max_order_notional,
        max_position_notional=settings.risk_max_position_notional,
        min_order_notional=settings.risk_min_order_notional,
        max_orders_per_minute=settings.risk_max_orders_per_minute,
        max_daily_loss_pct=settings.risk_max_daily_loss_pct,
        max_drawdown_pct=settings.risk_max_drawdown_pct,
    )
    return TradingService(
        session,
        cash=AccountCashAdapter(accounts),
        prices=MarketPriceAdapter(market),
        policy=RiskPolicy(limits),
        bus=request.app.state.bus,
        fee_bps=settings.fee_bps,
        slippage_bps=settings.slippage_bps,
        risk=RiskService(session),
    )


@router.post("/orders", status_code=201)
def place_order(payload: OrderCreate, request: Request, session: Session = Depends(get_session)):
    service = build_service(request, session)
    return service.place_order(
        account_id=payload.account_id,
        symbol=payload.symbol,
        side=payload.side,
        order_type=payload.order_type,
        quantity=payload.quantity,
        limit_price=payload.limit_price,
        source="manual",
        client_order_id=payload.client_order_id,
    )


@router.get("/orders")
def list_orders(
    request: Request,
    account_id: str,
    status: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    return build_service(request, session).list_orders(account_id, status=status, limit=limit)


@router.delete("/orders/{order_id}")
def cancel_order(
    order_id: str,
    request: Request,
    account_id: str | None = None,
    session: Session = Depends(get_session),
):
    return build_service(request, session).cancel_order(order_id, account_id=account_id)


@router.get("/positions")
def positions(
    request: Request,
    account_id: str,
    session: Session = Depends(get_session),
):
    return build_service(request, session).positions(account_id)


@router.get("/fills")
def fills(
    request: Request,
    account_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    return build_service(request, session).fills(account_id, limit=limit)


@router.post("/positions/{symbol}/close")
def close_position(
    symbol: str,
    payload: ClosePositionRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    return build_service(request, session).close_position(payload.account_id, symbol)


@router.post("/match")
def trigger_matching(request: Request, session: Session = Depends(get_session)):
    """Manual nudge for demos and tests; the background loop calls the same path."""
    count = build_service(request, session).match_open_orders()
    return {"matched": count}
