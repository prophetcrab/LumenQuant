from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(min_length=2, max_length=48)
    symbol: str = Field(default="BTCUSDT", min_length=2, max_length=24)
    timeframe: str = "1h"
    bars: int = Field(default=1500, ge=50, le=20_000)
    params: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class OptimiseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(min_length=2, max_length=48)
    symbol: str = Field(default="BTCUSDT", min_length=2, max_length=24)
    timeframe: str = "1h"
    bars: int = Field(default=800, ge=250, le=5000)
    space: dict[str, list[Any]] = Field(
        description="参数空间，例如 {\"fast\": [5, 10, 15], \"slow\": [20, 30]}"
    )
    base_params: dict[str, Any] = Field(default_factory=dict)
    objective: str = Field(default="risk_adjusted")
    config: dict[str, Any] = Field(default_factory=dict)
    top_n: int = Field(default=10, ge=1, le=50)


class WalkForwardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(min_length=2, max_length=48)
    symbol: str = Field(default="BTCUSDT", min_length=2, max_length=24)
    timeframe: str = "1h"
    bars: int = Field(default=1200, ge=400, le=5000)
    space: dict[str, list[Any]] = Field(
        description="在每个训练窗口内搜索的参数空间"
    )
    base_params: dict[str, Any] = Field(default_factory=dict)
    objective: str = Field(default="risk_adjusted")
    config: dict[str, Any] = Field(default_factory=dict)
    windows: int = Field(default=4, ge=2, le=12)
