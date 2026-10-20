from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.audit import AuditLog
from app.core.timeutil import iso


router = APIRouter(prefix="/system", tags=["system"])


@router.get("/metrics")
def metrics(request: Request):
    collector = getattr(request.app.state, "metrics", None)
    caches = {
        "market": getattr(request.app.state, "market_cache", None),
        "candles": getattr(request.app.state, "candle_cache", None),
        "stats": getattr(request.app.state, "stats_cache", None),
    }
    return {
        "requests": collector.snapshot() if collector else {},
        "caches": {name: cache.stats() for name, cache in caches.items() if cache is not None},
        "engines": {
            "market": getattr(request.app.state, "market_engine", None) is not None,
            "trading": getattr(request.app.state, "trading_engine", None) is not None,
            "runtime": getattr(request.app.state, "runtime_engine", None) is not None,
            "portfolio": getattr(request.app.state, "portfolio_engine", None) is not None,
        },
    }


@router.get("/audit")
def audit(
    request: Request,
    action: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
):
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = session.scalars(stmt.limit(limit)).all()
    return [
        {
            "id": row.id,
            "actor": row.actor,
            "action": row.action,
            "entity": row.entity,
            "entity_id": row.entity_id,
            "detail": json.loads(row.detail) if row.detail else {},
            "created_at": iso(row.created_at),
        }
        for row in rows
    ]


@router.get("/audit/summary")
def audit_summary(session: Session = Depends(get_session)):
    rows = session.execute(
        select(AuditLog.action, func.count(AuditLog.id)).group_by(AuditLog.action)
    ).all()
    return [{"action": action, "count": count} for action, count in rows]
