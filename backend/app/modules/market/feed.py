"""Deterministic synthetic market data.

The feed is a pure function of (symbol, open_time, previous close) so restarts
rebuild the identical path. A simulated clock lets the demo move at 60x without
faking timestamps inconsistently.
"""

from __future__ import annotations

import hashlib
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def timeframe_ms(timeframe: str) -> int:
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe {timeframe}") from exc


def align_down(epoch_ms: int, step_ms: int) -> int:
    return epoch_ms - (epoch_ms % step_ms)


class MarketClock:
    """Simulated wall clock. speed=1 means real time."""

    def __init__(self, speed: float = 60.0, *, start_ms: int | None = None) -> None:
        self.speed = max(0.001, float(speed))
        self._started_wall = time.monotonic()
        self._start_ms = int(time.time() * 1000) if start_ms is None else int(start_ms)

    def now_ms(self) -> int:
        elapsed = (time.monotonic() - self._started_wall) * 1000
        return self._start_ms + int(elapsed * self.speed)

    def ensure_at_least(self, epoch_ms: int) -> None:
        """Shift the simulated origin forward so persisted history is never ahead."""
        current = self.now_ms()
        if epoch_ms > current:
            self._start_ms += epoch_ms - current


def _seed(symbol: str, bucket: int) -> int:
    digest = hashlib.sha256(f"{symbol}:{bucket}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


@dataclass
class Bar:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_tuple(self) -> tuple[float, float, float, float, float]:
        return (self.open, self.high, self.low, self.close, self.volume)


class SyntheticFeed:
    def __init__(self, *, volatility: float = 0.0016, spread: float = 0.0004) -> None:
        self.volatility = volatility
        self.spread = spread

    def generate(self, symbol: str, open_time: int, previous_close: float, step_ms: int) -> Bar:
        rng = random.Random(_seed(symbol, open_time // step_ms))
        # Slow regime: 6h trend blocks, so charts show swings instead of noise.
        regime_bucket = open_time // (21_600_000)
        regime_rng = random.Random(_seed(f"{symbol}:regime", regime_bucket))
        drift = regime_rng.uniform(-0.0006, 0.0006)
        vol_scale = regime_rng.uniform(0.55, 1.9)
        shock = 0.0
        if regime_rng.random() < 0.04:
            shock = regime_rng.choice((-1.0, 1.0)) * regime_rng.uniform(0.004, 0.02)

        ret = drift + rng.gauss(0.0, self.volatility * vol_scale) + shock
        open_price = previous_close * (1.0 + rng.gauss(0.0, self.spread))
        close = max(1e-6, open_price * (1.0 + ret))
        wick = abs(rng.gauss(0.0, self.volatility * vol_scale * 0.75))
        high = max(open_price, close) * (1.0 + wick)
        low = min(open_price, close) * (1.0 - wick)
        volume = abs(rng.gauss(1.0, 0.35)) * (1.0 + abs(ret) * 300) * 1000.0
        bars_per_year = 365 * 24 * 3_600_000 / step_ms
        drift_component = drift / bars_per_year
        return Bar(
            open_time=open_time,
            open=round(open_price, 6),
            high=round(high, 6),
            low=round(low, 6),
            close=round(close, 6),
            volume=round(volume, 4) + (abs(drift_component) * 0),
        )

    def history(
        self, symbol: str, *, end_ms: int, count: int, step_ms: int, initial_price: float
    ) -> list[Bar]:
        """Build `count` closed bars ending before end_ms, anchored to initial_price."""
        end_bucket = align_down(end_ms, step_ms)
        bars: list[Bar] = []
        # Anchor backwards from a stable starting price by walking forward from a
        # synthetic origin; deterministic and independent of history length.
        origin = end_bucket - count * step_ms
        price = initial_price * (1.0 + (math.sin(origin / 86_400_000) * 0.05))
        for index in range(count):
            open_time = origin + index * step_ms
            bar = self.generate(symbol, open_time, price, step_ms)
            bars.append(bar)
            price = bar.close
        return bars
