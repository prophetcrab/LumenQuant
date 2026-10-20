from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SymbolCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=24)
    name: str = Field(default="", max_length=64)
    market: Literal["crypto", "cn_stock"] = "crypto"
    exchange: str = Field(default="", max_length=8)
    board: Literal["", "main", "gem", "star", "bse"] = ""
    initial_price: float = Field(gt=0)
    base: str = Field(default="", max_length=12)
    quote: str = Field(default="", min_length=0, max_length=12)
    tick_size: float = Field(default=0.01, gt=0)
    lot_size: float = Field(default=0.0, ge=0)
    prev_close: float = Field(default=0.0, ge=0)
    limit_pct: float = Field(default=0.0, ge=0, le=0.5)
    is_st: bool = False

    @model_validator(mode="after")
    def _defaults_per_market(self) -> "SymbolCreate":
        if self.market == "cn_stock":
            if not self.quote:
                self.quote = "CNY"
            if self.lot_size <= 0:
                self.lot_size = 100.0
            if not self.board:
                self.board = "main"
            if not self.exchange:
                suffix = self.code.upper().rsplit(".", 1)[-1] if "." in self.code else ""
                self.exchange = suffix if suffix in {"SH", "SZ", "BJ"} else "SH"
        else:
            if not self.quote:
                self.quote = "USDT"
            if self.lot_size <= 0:
                self.lot_size = 0.0001
        return self


class SymbolUpdate(BaseModel):
    enabled: bool
