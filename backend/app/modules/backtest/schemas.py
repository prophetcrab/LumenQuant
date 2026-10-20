from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BacktestRequest(BaseModel):
    strategy: str = Field(min_length=2, max_length=48)
    symbol: str = Field(default="BTCUSDT", min_length=2, max_length=24)
    timeframe: str = "1h"
    bars: int = Field(default=1500, ge=50, le=20_000)
    params: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
