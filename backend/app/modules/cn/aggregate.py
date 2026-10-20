"""Session-aware aggregation for A-share intraday bars.

Epoch-based bucketing (the crypto path) produces bars like a 09:00 bar covering
09:30-10:30 and a separate 11:00 bar for the last half hour before lunch. A-share
charts and strategies expect bars aligned to the trading session: 30m bars end at
10:00/10:30/11:00/11:30/13:30/14:00/14:30/15:00, and 1h bars cover
09:30-10:30 and 10:30-11:30 in the morning.

So we group consecutive bars by the session that contains them and then chunk by
the requested ratio. A session shorter than the ratio yields one shorter bar
rather than merging across the lunch break.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.modules.cn.calendar import session_bounds
from app.modules.market.feed import timeframe_ms
from app.modules.market.service_types import CandlePoint

CN_BASE_MS = timeframe_ms("15m")


def aggregate_session_bars(
    candles: Sequence[CandlePoint], timeframe: str, *, base_ms: int = CN_BASE_MS
) -> list[CandlePoint]:
    if timeframe == "15m":
        return list(candles)
    ratio = max(1, timeframe_ms(timeframe) // base_ms)
    out: list[CandlePoint] = []
    chunk: list[CandlePoint] = []

    def flush() -> None:
        if not chunk:
            return
        out.append(
            CandlePoint(
                time=chunk[0].time,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                volume=round(sum(c.volume for c in chunk), 4),
            )
        )
        chunk.clear()

    session_key: tuple[int, int] | None = None
    for candle in candles:
        bounds = session_bounds(candle.time)
        # A datum outside any session (should not happen) is kept standalone so
        # it is visible in the chart instead of being silently merged away.
        if bounds is None:
            flush()
            out.append(candle)
            session_key = None
            continue
        if bounds != session_key:
            flush()
            session_key = bounds
        chunk.append(candle)
        if len(chunk) >= ratio:
            flush()
    flush()
    return out


def aggregate_multi_session(candles: Iterable[CandlePoint], timeframe: str) -> list[CandlePoint]:
    return aggregate_session_bars(list(candles), timeframe)
