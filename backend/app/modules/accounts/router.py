from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.modules.accounts.schemas import AccountCreate, AccountUpdate, BalanceMove
from app.modules.accounts.service import AccountService


router = APIRouter(prefix="/accounts", tags=["accounts"])


def _service(request: Request, session: Session) -> AccountService:
    return AccountService(
        session, live_trading_enabled=request.app.state.settings.live_trading_enabled
    )


@router.get("")
def list_accounts(request: Request, session: Session = Depends(get_session)):
    return _service(request, session).list()


@router.post("", status_code=201)
def create_account(payload: AccountCreate, request: Request, session: Session = Depends(get_session)):
    return _service(request, session).create(
        name=payload.name,
        kind=payload.kind,
        initial_balance=payload.initial_balance,
        base_currency=payload.base_currency,
        broker=payload.broker,
        leverage=payload.leverage,
        market=payload.market,
    )


@router.get("/{account_id}")
def get_account(account_id: str, request: Request, session: Session = Depends(get_session)):
    return _service(request, session).get(account_id)


@router.patch("/{account_id}")
def update_account(
    account_id: str,
    payload: AccountUpdate,
    request: Request,
    session: Session = Depends(get_session),
):
    return _service(request, session).update(
        account_id, name=payload.name, status=payload.status, leverage=payload.leverage
    )


@router.post("/{account_id}/deposit")
def deposit(
    account_id: str,
    payload: BalanceMove,
    request: Request,
    session: Session = Depends(get_session),
):
    return _service(request, session).deposit(account_id, payload.amount, note=payload.note)


@router.post("/{account_id}/withdraw")
def withdraw(
    account_id: str,
    payload: BalanceMove,
    request: Request,
    session: Session = Depends(get_session),
):
    return _service(request, session).withdraw(account_id, payload.amount, note=payload.note)


@router.get("/{account_id}/ledger")
def ledger(
    account_id: str,
    request: Request,
    limit: int = 100,
    session: Session = Depends(get_session),
):
    return _service(request, session).ledger(account_id, limit=max(1, min(limit, 500)))
