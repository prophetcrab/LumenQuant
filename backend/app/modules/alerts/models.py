"""User-facing alerts.

Distinct from the audit log: `audit_logs` is an append-only record of everything
that happened, kept for forensics. An alert is something the operator must decide
about — a tripped breaker, a stop that fired, an order the gateway refused. It
carries an acknowledgement flag so "have I looked at this?" is answerable, which
an audit row can never be.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow

LEVELS = ("info", "warning", "critical")


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_account_time", "account_id", "created_at"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    account_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    level: Mapped[str] = mapped_column(String(12), default="warning", index=True)
    # Stable machine-readable kind ("breaker", "stop_loss", "gateway_failure", …)
    category: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(160))
    message: Mapped[str] = mapped_column(Text, default="")
    ref_type: Mapped[str] = mapped_column(String(32), default="")
    ref_id: Mapped[str] = mapped_column(String(64), default="")
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
