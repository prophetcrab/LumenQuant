from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.modules.accounts.models import new_id


class Symbol(Base):
    __tablename__ = "market_symbols"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    asset_class: Mapped[str] = mapped_column(String(24), default="crypto")
    base: Mapped[str] = mapped_column(String(12), default="")
    quote: Mapped[str] = mapped_column(String(12), default="USDT")
    tick_size: Mapped[float] = mapped_column(Float, default=0.01)
    lot_size: Mapped[float] = mapped_column(Float, default=0.0001)
    initial_price: Mapped[float] = mapped_column(Float, default=100.0)
    volume_bias: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Candle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "open_time", name="uq_candle"),)

    # Integer (not BigInteger) so SQLite treats it as the rowid alias and autoincrements.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), default="15m")
    open_time: Mapped[int] = mapped_column(BigInteger, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
