from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_backtests
from app.modules.backtest.schemas import BacktestRequest
from app.modules.backtest.service import BacktestService


router = APIRouter(prefix="/backtests", tags=["backtest"])


def build_service(request: Request, session: Session) -> BacktestService:
    return build_backtests(request, session)


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
