from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["paper", "live"] = "paper"
    market: Literal["crypto", "cn_stock"] = "crypto"
    initial_balance: float = Field(default=100_000.0, gt=0, le=1_000_000_000)
    base_currency: str | None = Field(default=None, min_length=2, max_length=12)
    broker: str | None = Field(default=None, max_length=32)
    leverage: float = Field(default=1.0, ge=1, le=10)


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    status: Literal["active", "archived"] | None = None
    leverage: float | None = Field(default=None, ge=1, le=10)


class BalanceMove(BaseModel):
    amount: float = Field(gt=0, le=1_000_000_000)
    note: str = Field(default="", max_length=240)
