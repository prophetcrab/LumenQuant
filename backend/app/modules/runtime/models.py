from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow


def new_id() -> str:
    return str(uuid.uuid4())


class StrategyInstance(Base):
    __tablename__ = "strategy_instances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    strategy_key: Mapped[str] = mapped_column(String(48), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), default="1h")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    position_pct: Mapped[float] = mapped_column(Float, default=0.25)
    max_order_notional: Mapped[float] = mapped_column(Float, default=50_000.0)
    running: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_bar_time: Mapped[int] = mapped_column(BigInteger, default=0)
    last_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(400), default="")
    executions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RuntimeLog(Base):
    __tablename__ = "runtime_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategy_instances.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[str] = mapped_column(String(12), default="info")
    event: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    order_id: Mapped[str] = mapped_column(String(36), default="")
    bar_time: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
