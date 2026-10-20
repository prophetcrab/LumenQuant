from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_market
from app.modules.strategies.schemas import SignalRequest
from app.modules.strategies.service import StrategyService


router = APIRouter(prefix="/strategies", tags=["strategies"])


def _service(request: Request, session: Session) -> StrategyService:
    return StrategyService(build_market(request, session))


@router.get("")
def list_strategies(request: Request, session: Session = Depends(get_session)):
    return _service(request, session).catalog()


@router.get("/{key}")
def strategy_detail(key: str, request: Request, session: Session = Depends(get_session)):
    return _service(request, session).detail(key)


@router.post("/{key}/signals")
def strategy_signals(
    key: str,
    payload: SignalRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    return _service(request, session).signals(
        key,
        symbol=payload.symbol,
        timeframe=payload.timeframe,
        limit=payload.limit,
        params=payload.params,
    )
