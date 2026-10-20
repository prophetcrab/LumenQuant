"""Market data source abstraction.

The platform must work with or without internet access, so data sourcing is a
pluggable concern rather than a hard dependency:

* `synthetic` — the deterministic generator (default, offline, reproducible);
* `tencent`   — real quotes and bars from Tencent's public endpoints.

Both return the same DTOs, so the sync service and the rest of the system stay
unaware of which one is active.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.core.errors import AppError
from app.modules.market.service_types import CandlePoint


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    volume: float
    timestamp_ms: int


@dataclass(frozen=True)
class BarBatch:
    code: str
    timeframe: str
    bars: list[CandlePoint] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.bars)


@dataclass(frozen=True)
class SourceHealth:
    name: str
    available: bool
    detail: str = ""
    latency_ms: float = 0.0


class MarketDataSource(Protocol):
    name: str

    def daily_bars(self, code: str, *, limit: int = 800) -> BarBatch: ...

    def intraday_bars(self, code: str, *, limit: int = 320) -> BarBatch: ...

    def quote(self, code: str) -> Quote: ...

    def health(self) -> SourceHealth: ...


class SourceUnavailable(AppError):
    """Raised when an upstream source cannot serve a request (maps to HTTP 502)."""

    status_code = 502
    code = "data_source_unavailable"


def split_code(code: str) -> tuple[str, str]:
    """`600519.SH` -> ("sh", "600519"). Missing suffix defaults to Shanghai."""
    code = code.strip().upper()
    if "." in code:
        digits, suffix = code.split(".", 1)
    else:
        digits, suffix = code, "SH"
    market = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix, "sh")
    return market, digits


def join_code(market: str, digits: str) -> str:
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(market.lower(), "SH")
    return f"{digits}.{suffix}"
