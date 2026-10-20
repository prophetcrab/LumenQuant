from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CandlePoint:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class SymbolView:
    code: str
    name: str
    asset_class: str
    base: str
    quote: str
    tick_size: float
    lot_size: float
    initial_price: float
    enabled: bool
    market: str = "crypto"
    exchange: str = ""
    board: str = ""
    prev_close: float = 0.0
    limit_pct: float = 0.0
    is_st: bool = False
    suspended: bool = False
    base_timeframe: str = "15m"

    @property
    def is_cn(self) -> bool:
        return self.market == "cn_stock"


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    price: float
    change_24h_pct: float
    high_24h: float
    low_24h: float
    volume_24h: float
    time: int
    market: str = "crypto"
    prev_close: float = 0.0
    limit_up: float = 0.0
    limit_down: float = 0.0
