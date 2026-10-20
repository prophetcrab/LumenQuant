from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_portfolio
from app.modules.portfolio.service import PortfolioService


router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def build_service(request: Request, session: Session) -> PortfolioService:
    return build_portfolio(request, session)


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
