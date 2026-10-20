"""Append-only audit trail (shared kernel)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, Session

from app.core.db import Base
from app.core.timeutil import utcnow


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str] = mapped_column(String(64), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


def record_audit(
    session: Session,
    *,
    action: str,
    entity: str,
    entity_id: str,
    detail: str = "{}",
    actor: str = "local",
) -> None:
    session.add(
        AuditLog(
            id=str(uuid.uuid4()),
            actor=actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail,
        )
    )
