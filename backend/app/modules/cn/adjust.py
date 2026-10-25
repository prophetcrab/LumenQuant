"""Forward-adjustment factors derived from raw daily bars.

Why this exists: the exchange's own adjusted series (`fqkline` … `qfq`) is the
authority, but that endpoint is rate-limited far more aggressively than the raw
one — measured 2026-10, `kline/kline` answered while every `fqkline` request from
the same IP returned HTTP 501 for minutes. Importing a few hundred symbols through
it is not viable.

So the factor is reconstructed from the raw series. An ex-rights day is
arithmetically a discontinuity: the cash flow to the holder is unchanged, so the
price drops by the split/dividend ratio in one step. Any gap larger than the
board's own limit cannot be a trading move, which makes it identifiable without
extra data:

    factor_i = cumulative product of (previous_close / today_close) over every
               gap that exceeds the limit threshold

Two consequences worth stating plainly:

* **Small dividends are not captured.** A 1% dividend gap sits inside the ±10%
  band and is indistinguishable from a normal day. The adjustment therefore
  removes splits and large ex-rights events (the ones that destroy indicators)
  and leaves ordinary dividends in the series. That is a real limitation, not an
  oversight — it is reported as `small_adjustments_possible` so a study can say
  so instead of implying exchange-grade precision.
* **Detection needs a threshold.** Too low flags ordinary limit moves; too high
  misses 2-for-1 splits on a 20% board. The default is the board limit plus a
  small margin.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.market.service_types import CandlePoint

# A real trading day cannot exceed the board limit; anything beyond this is a
# corporate action. 21% sits above the widest mainland band (20%) with margin.
DEFAULT_GAP_THRESHOLD = 0.21


@dataclass(frozen=True)
class AdjustmentResult:
    bars: list[CandlePoint]
    factors: list[float]
    events: list[dict]
    small_adjustments_possible: bool


def derive_factors(
    bars: list[CandlePoint], *, gap_threshold: float = DEFAULT_GAP_THRESHOLD
) -> tuple[list[float], list[dict]]:
    """Divisor per bar: `adjusted = raw / factor`, with 1.0 at the newest bar.

    Forward adjustment is a *division* of history by the cumulative split ratio,
    verified against real data (002594.SZ): raw 337.00 and 334.12 divide by 3.0456
    to give the exchange's own 110.65 and 109.69, while post-split bars divide by
    ~1.0. So walking backwards, each ex-rights step multiplies the divisor by that
    step's ratio; every ordinary day carries the previous divisor unchanged —
    which is exactly what keeps ordinary-day returns intact.
    """
    n = len(bars)
    if n == 0:
        return [], []
    factors = [1.0] * n
    events: list[dict] = []
    for i in range(n - 2, -1, -1):
        prev_close = bars[i].close
        next_close = bars[i + 1].close
        if prev_close <= 0 or next_close <= 0:
            factors[i] = factors[i + 1]
            continue
        ret = next_close / prev_close - 1.0
        if abs(ret) > gap_threshold:
            # Corporate action: extend the divisor by this step's ratio. The event
            # is stamped on the bar whose return changes once history is rebased
            # (i+1), which is the bar an analysis must not read as a crash.
            ratio = prev_close / next_close
            factors[i] = factors[i + 1] * ratio
            events.append(
                {
                    "date": _iso(bars[i + 1].time),
                    "prev_date": _iso(bars[i].time),
                    "prev_close": round(prev_close, 4),
                    "close": round(next_close, 4),
                    "change_pct": round(ret * 100, 2),
                    "factor": round(ratio, 6),
                }
            )
        else:
            factors[i] = factors[i + 1]
    return factors, events


def forward_adjusted(
    bars: list[CandlePoint], *, gap_threshold: float = DEFAULT_GAP_THRESHOLD
) -> AdjustmentResult:
    """Divide history by the cumulative split ratio; the newest bar is unchanged."""
    factors, events = derive_factors(bars, gap_threshold=gap_threshold)
    adjusted: list[CandlePoint] = []
    for bar, divisor in zip(bars, factors):
        scale = 1.0 / divisor if divisor else 1.0
        adjusted.append(
            CandlePoint(
                time=bar.time,
                open=round(bar.open * scale, 6),
                high=round(bar.high * scale, 6),
                low=round(bar.low * scale, 6),
                close=round(bar.close * scale, 6),
                # Volume is not scaled: a split multiplies shares outstanding, and
                # adjusting volume without knowing the split ratio would corrupt
                # every volume-derived feature. Raw volume stays comparable to the
                # source, which is what a liquidity factor wants anyway.
                volume=bar.volume,
            )
        )
    return AdjustmentResult(
        bars=adjusted,
        factors=factors,
        events=events,
        small_adjustments_possible=not events,
    )


def _iso(epoch_ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
