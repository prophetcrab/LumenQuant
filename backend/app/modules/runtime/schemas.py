from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    strategy_key: str = Field(min_length=2, max_length=48)
    symbol: str = Field(default="BTCUSDT", min_length=2, max_length=24)
    timeframe: str = "1h"
    params: dict[str, Any] = Field(default_factory=dict)
    position_pct: float = Field(default=0.25, ge=0.01, le=1)
    max_order_notional: float = Field(default=50_000, gt=0, le=10_000_000)
    start: bool = False


class InstanceUpdate(BaseModel):
    params: dict[str, Any] | None = None
    position_pct: float | None = Field(default=None, ge=0.01, le=1)
    max_order_notional: float | None = Field(default=None, gt=0, le=10_000_000)


class RunningToggle(BaseModel):
    running: bool
