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
    stop_loss_pct: float = Field(default=0.0, ge=0, le=50, description="止损百分比，0 关闭")
    take_profit_pct: float = Field(default=0.0, ge=0, le=500, description="止盈百分比，0 关闭")
    trailing_stop_pct: float = Field(
        default=0.0, ge=0, le=50, description="移动止损回撤百分比，0 关闭"
    )
    start: bool = False


class InstanceUpdate(BaseModel):
    params: dict[str, Any] | None = None
    position_pct: float | None = Field(default=None, ge=0.01, le=1)
    max_order_notional: float | None = Field(default=None, gt=0, le=10_000_000)
    stop_loss_pct: float | None = Field(default=None, ge=0, le=50)
    take_profit_pct: float | None = Field(default=None, ge=0, le=500)
    trailing_stop_pct: float | None = Field(default=None, ge=0, le=50)


class RunningToggle(BaseModel):
    running: bool
