"""Pure OHLCV aggregation used by the API and backtester."""

from __future__ import annotations

from collections.abc import Iterable

from app.modules.market.feed import align_down, timeframe_ms
from app.modules.market.service_types import CandlePoint


def aggregate(candles: Iterable[CandlePoint], timeframe: str) -> list[CandlePoint]:
    step = timeframe_ms(timeframe)
    out: list[CandlePoint] = []
    bucket: int | None = None
    current: list[CandlePoint] = []

    def flush() -> None:
        if not current:
            return
        out.append(
            CandlePoint(
                time=current[0].time,
                open=current[0].open,
                high=max(c.high for c in current),
                low=min(c.low for c in current),
                close=current[-1].close,
                volume=sum(c.volume for c in current),
            )
        )

    for candle in candles:
        key = align_down(candle.time, step)
        if bucket is None:
            bucket = key
        if key != bucket:
            flush()
            current = []
            bucket = key
        # Re-home the first candle of a bucket so charts align to the timeframe.
        current.append(
            CandlePoint(
                time=key if not current else candle.time,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
        )
    flush()
    return out
