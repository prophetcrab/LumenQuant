from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.modules.alerts.service import AlertService


router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
def list_alerts(
    account_id: str | None = None,
    unacknowledged_only: bool = False,
    level: str | None = None,
    category: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
):
    return [
        asdict(item)
        for item in AlertService(session).list(
            account_id=account_id,
            unacknowledged_only=unacknowledged_only,
            level=level,
            category=category,
            limit=limit,
        )
    ]


@router.get("/categories")
def categories(account_id: str | None = None, session: Session = Depends(get_session)):
    return AlertService(session).categories(account_id=account_id)


@router.post("/prune")
def prune(
    keep_days: int = Query(default=30, ge=0, le=3650),
    account_id: str | None = None,
    session: Session = Depends(get_session),
):
    """Drop acknowledged alerts older than `keep_days`.

    Unacknowledged alerts are kept regardless of age — nobody has acted on them.
    """
    return {"deleted": AlertService(session).prune(keep_days=keep_days, account_id=account_id)}


@router.get("/summary")
def summary(account_id: str | None = None, session: Session = Depends(get_session)):
    return AlertService(session).summary(account_id=account_id)


@router.post("/{alert_id}/ack")
def acknowledge(alert_id: str, session: Session = Depends(get_session)):
    return asdict(AlertService(session).acknowledge(alert_id))


@router.post("/ack-all")
def acknowledge_all(account_id: str | None = None, session: Session = Depends(get_session)):
    return {"acknowledged": AlertService(session).acknowledge_all(account_id=account_id)}
