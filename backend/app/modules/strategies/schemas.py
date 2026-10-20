from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SignalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(default="BTCUSDT", min_length=2, max_length=24)
    timeframe: str = "15m"
    limit: int = Field(default=300, ge=20, le=5000)
    params: dict[str, Any] = Field(default_factory=dict)
