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


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    price: float
    change_24h_pct: float
    high_24h: float
    low_24h: float
    volume_24h: float
    time: int
