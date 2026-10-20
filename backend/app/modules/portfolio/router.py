from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.modules.accounts.service import AccountService
from app.modules.market.service import MarketService
from app.modules.portfolio.service import PortfolioService
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.service import TradingService


router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def build_service(request: Request, session: Session) -> PortfolioService:
    settings = request.app.state.settings
    engine = getattr(request.app.state, "market_engine", None)
    accounts = AccountService(session, live_trading_enabled=settings.live_trading_enabled)
    market = MarketService(
        session, clock=getattr(engine, "clock", None), live=engine if engine else None
    )
    trading = TradingService(
        session,
        cash=AccountCashAdapter(accounts),
        prices=MarketPriceAdapter(market),
        bus=request.app.state.bus,
        fee_bps=settings.fee_bps,
        slippage_bps=settings.slippage_bps,
    )
    return PortfolioService(session, accounts, trading)


@router.get("/overview")
def overview(
    request: Request,
    account_id: str | None = None,
    record: bool = True,
    session: Session = Depends(get_session),
):
    return asdict(build_service(request, session).overview(account_id, record=record))


@router.get("/analytics")
def analytics(
    request: Request,
    account_id: str | None = None,
    limit: int = Query(default=1000, ge=2, le=5000),
    session: Session = Depends(get_session),
):
    return build_service(request, session).analytics(account_id, limit=limit)


@router.get("/history")
def history(
    request: Request,
    account_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    session: Session = Depends(get_session),
):
    return build_service(request, session).history(account_id, limit=limit)
