from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    symbol: str = Field(min_length=2, max_length=24)
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"] = "market"
    quantity: float = Field(gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    client_order_id: str | None = Field(default=None, max_length=64)


class ClosePositionRequest(BaseModel):
    account_id: str
