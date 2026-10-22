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


class MultiBacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(min_length=2, max_length=48)
    symbols: list[str] = Field(min_length=2, max_length=12)
    timeframe: str = "1d"
    bars: int = Field(default=500, ge=50, le=5000)
    params: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    weights: list[float] | None = Field(
        default=None, description="与 symbols 一一对应；省略则等权"
    )
    rebalance_bars: int = Field(
        default=0, ge=0, le=2000, description="每 N 根 K 线恢复到目标权重；0 表示不再平衡"
    )


class PortfolioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(min_length=2, max_length=8)
    weights: list[float] | None = Field(
        default=None, description="与 run_ids 一一对应；省略则等权"
    )
    initial_capital: float = Field(default=100_000.0, gt=0)


class ApplyWeightsRequest(BaseModel):
    """Re-run the basket that produced these sleeves, with adjusted weights."""

    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(min_length=2, max_length=12)
    weights: list[float] = Field(min_length=2, max_length=12)


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
