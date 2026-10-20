from __future__ import annotations

from pydantic import BaseModel, Field


class SymbolCreate(BaseModel):
    code: str = Field(min_length=2, max_length=24)
    name: str = Field(default="", max_length=64)
    initial_price: float = Field(gt=0)
    base: str = Field(default="", max_length=12)
    quote: str = Field(default="USDT", min_length=2, max_length=12)
    tick_size: float = Field(default=0.01, gt=0)
    lot_size: float = Field(default=0.0001, gt=0)


class SymbolUpdate(BaseModel):
    enabled: bool
