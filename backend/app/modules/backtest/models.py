from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    strategy: Mapped[str] = mapped_column(String(48), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), default="1h")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    equity_json: Mapped[str] = mapped_column(Text, default="[]")
    # Rebased strategy-vs-benchmark series for the overlay chart.
    comparison_json: Mapped[str] = mapped_column(Text, default="[]")
    trades_json: Mapped[str] = mapped_column(Text, default="[]")
    initial_capital: Mapped[float] = mapped_column(Float, default=100_000.0)
    final_equity: Mapped[float] = mapped_column(Float, default=0.0)
    bars: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    # "" for a normal run, "multi" for a sleeve written by a basket backtest.
    # Sleeves are ordinary runs (same shape, same analysis path) but should not
    # clutter the single-symbol history list.
    origin: Mapped[str] = mapped_column(String(16), default="", index=True)


class MultiBacktestRun(Base):
    """A basket backtest: one strategy, several symbols, one combined curve.

    Kept in its own table rather than reusing `BacktestRun` with a comma-joined
    symbol: the portfolio analysis joins runs by aligned timestamps and expects
    one instrument per row, so mixing shapes would corrupt it.
    """

    __tablename__ = "multi_backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    strategy: Mapped[str] = mapped_column(String(48), index=True)
    symbols_json: Mapped[str] = mapped_column(Text, default="[]")
    timeframe: Mapped[str] = mapped_column(String(8), default="1d")
    market: Mapped[str] = mapped_column(String(16), default="crypto")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    weights_json: Mapped[str] = mapped_column(Text, default="[]")
    rebalance_bars: Mapped[int] = mapped_column(Integer, default=0)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    equity_json: Mapped[str] = mapped_column(Text, default="[]")
    comparison_json: Mapped[str] = mapped_column(Text, default="[]")
    components_json: Mapped[str] = mapped_column(Text, default="[]")
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    initial_capital: Mapped[float] = mapped_column(Float, default=100_000.0)
    final_equity: Mapped[float] = mapped_column(Float, default=0.0)
    bars: Mapped[int] = mapped_column(Integer, default=0)
    # BacktestRun ids for each sleeve, in the same order as `symbols_json`.
    # Portfolio analysis consumes these directly.
    child_run_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
