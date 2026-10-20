from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.modules.backtest.schemas import BacktestRequest
from app.modules.backtest.service import BacktestService
from app.modules.market.service import MarketService


router = APIRouter(prefix="/backtests", tags=["backtest"])


def build_service(request: Request, session: Session) -> BacktestService:
    settings = request.app.state.settings
    engine = getattr(request.app.state, "market_engine", None)
    market = MarketService(
        session, clock=getattr(engine, "clock", None), live=None
    )
    return BacktestService(
        session,
        market,
        fee_bps=settings.fee_bps,
        slippage_bps=settings.slippage_bps,
    )


@router.post("", status_code=201)
def run_backtest(
    payload: BacktestRequest, request: Request, session: Session = Depends(get_session)
):
    return build_service(request, session).run(
        strategy_key=payload.strategy,
        symbol=payload.symbol,
        timeframe=payload.timeframe,
        bars=payload.bars,
        params=payload.params,
        config=payload.config,
    )


@router.get("")
def list_backtests(
    request: Request,
    limit: int = Query(default=30, ge=1, le=200),
    session: Session = Depends(get_session),
):
    return build_service(request, session).list(limit=limit)


@router.get("/{run_id}")
def get_backtest(run_id: str, request: Request, session: Session = Depends(get_session)):
    return build_service(request, session).get(run_id)


@router.delete("/{run_id}", status_code=204)
def delete_backtest(run_id: str, request: Request, session: Session = Depends(get_session)):
    build_service(request, session).delete(run_id)
