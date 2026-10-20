from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_accounts, build_trading
from app.core.errors import ForbiddenError
from app.modules.trading.schemas import ClosePositionRequest, OrderCreate


router = APIRouter(prefix="/trading", tags=["trading"])


def build_service(request: Request, session: Session):
    return build_trading(request, session, with_brokers=True)


def _assert_live_confirmation(request: Request, session: Session, account_id: str) -> None:
    """Live orders need an explicit opt-in header in addition to the server switch."""
    settings = request.app.state.settings
    account = build_accounts(request, session).require(account_id)
    if account.kind != "live":
        return
    if not settings.live_trading_enabled:
        raise ForbiddenError("live trading is disabled by server configuration")
    if request.headers.get("X-Live-Confirm", "").lower() != "true":
        raise ForbiddenError("live order requires header X-Live-Confirm: true")


@router.post("/orders", status_code=201)
def place_order(payload: OrderCreate, request: Request, session: Session = Depends(get_session)):
    _assert_live_confirmation(request, session, payload.account_id)
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
    _assert_live_confirmation(request, session, payload.account_id)
    return build_service(request, session).close_position(payload.account_id, symbol)


@router.post("/settle")
def settle(request: Request, session: Session = Depends(get_session)):
    """Manually release T+1 holds (the scheduler does this at each session open)."""
    return {"unlocked": build_service(request, session).settle_t1()}


@router.post("/match")
def trigger_matching(request: Request, session: Session = Depends(get_session)):
    """Manual nudge for demos and tests; the background loop calls the same path."""
    count = build_service(request, session).match_open_orders()
    return {"matched": count}
