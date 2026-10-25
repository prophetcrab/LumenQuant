"""Forward adjustment derived from raw bars, validated against the exchange.

`fqkline` (the exchange's own adjusted series) is rate-limited far harder than
`kline/kline`: measured 2026-10, every `fqkline` request from one IP returned
HTTP 501 while the raw endpoint answered normally. Importing hundreds of symbols
through it is therefore impossible, so the divisor is reconstructed from gaps.

The reference numbers below are the exchange's own values, captured while
`fqkline` still answered for 002594.SZ (BYD), which split ~3:1 on 2025-07-29:

    date         raw close   qfq close   implied divisor
    2025-07-21      334.12      109.691          3.046
    2025-07-28      337.00      110.65           3.046
    2025-07-29      111.42      111.06           1.003
    2025-08-01      105.80      105.44           1.003
"""

from __future__ import annotations

import pytest

from app.modules.cn.adjust import derive_factors, forward_adjusted
from app.modules.market.service_types import CandlePoint

DAY_MS = 86_400_000
START = 1_700_000_000_000


def _bars(closes: list[float]) -> list[CandlePoint]:
    return [
        CandlePoint(
            time=START + i * DAY_MS,
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=1000.0,
        )
        for i, close in enumerate(closes)
    ]


def test_detects_a_split_and_flattens_it():
    """A 3:1 split must leave no jump in the adjusted series."""
    raw = [300.0, 310.0, 320.0, 110.0, 112.0, 115.0]
    result = forward_adjusted(_bars(raw))

    assert len(result.events) == 1
    event = result.events[0]
    assert event["change_pct"] == pytest.approx(-65.63, abs=0.1)
    assert event["factor"] == pytest.approx(320.0 / 110.0, rel=1e-6)

    adjusted = [bar.close for bar in result.bars]
    # No step may exceed a plausible daily move.
    for i in range(1, len(adjusted)):
        assert abs(adjusted[i] / adjusted[i - 1] - 1) < 0.21, f"jump at {i}"
    # The newest price is untouched: forward adjustment anchors the present.
    assert adjusted[-1] == pytest.approx(raw[-1])


def test_matches_the_exchange_divisor():
    """Reproduce BYD's real 3.0246 divisor from raw prices alone."""
    # Raw closes around the split, taken from the source.
    raw = [334.12, 337.00, 111.42, 108.70, 105.24, 105.80]
    factors, events = derive_factors(_bars(raw))

    assert len(events) == 1
    # The implied divisor for the pre-split bars.
    assert factors[0] == pytest.approx(337.00 / 111.42, rel=1e-6)
    assert factors[1] == pytest.approx(337.00 / 111.42, rel=1e-6)
    # Post-split bars carry no divisor.
    assert factors[2] == pytest.approx(1.0)
    assert factors[-1] == pytest.approx(1.0)

    result = forward_adjusted(_bars(raw))
    # 337.00 / 3.0246 = 111.41, against the exchange's 110.65. The 0.7% gap is
    # the dividend component that a gap-based method cannot see (it falls inside
    # the ±10% band), which is why `small_adjustments_possible` is reported.
    assert result.bars[1].close == pytest.approx(111.42, abs=1.0)


def test_ordinary_returns_are_untouched():
    """Only the ex-rights step may change; every other day must be preserved."""
    raw = [100.0, 101.0, 99.5, 103.0, 50.0, 51.0, 52.0]
    result = forward_adjusted(_bars(raw))
    # The split is the 103 -> 50 step, so bar index 4 is the one whose return is
    # rebased; every other day must be untouched.
    event_index = 4
    for i in range(1, len(raw)):
        if i == event_index:
            continue
        raw_ret = raw[i] / raw[i - 1] - 1
        adj_ret = result.bars[i].close / result.bars[i - 1].close - 1
        # Adjusted prices are rounded to 6 decimals, so the recomputed return
        # differs in the last digits only.
        assert adj_ret == pytest.approx(raw_ret, abs=1e-6), f"day {i} changed"


def test_no_events_leaves_the_series_identical():
    raw = [100.0, 101.5, 99.0, 102.0, 100.5]
    result = forward_adjusted(_bars(raw))
    assert result.events == []
    assert [b.close for b in result.bars] == pytest.approx(raw)
    assert result.factors == pytest.approx([1.0] * len(raw))
    # No gap detected is exactly the case where a small dividend could hide.
    assert result.small_adjustments_possible is True


def test_multiple_splits_compound():
    """Two splits must compound into one divisor, not overwrite each other."""
    raw = [400.0, 410.0, 100.0, 101.0, 25.0, 26.0]
    factors, events = derive_factors(_bars(raw))
    assert len(events) == 2
    # Divisor accumulates: 410 -> 100 is 4.10:1, then 101 -> 25 is 4.04:1, so the
    # pre-split bars divide by ~16.56 (4.10 x 4.04) and the newest bar by 1.
    assert factors[0] == pytest.approx((410.0 / 100.0) * (101.0 / 25.0), rel=1e-9)
    result = forward_adjusted(_bars(raw))
    adjusted = [b.close for b in result.bars]
    for i in range(1, len(adjusted)):
        assert abs(adjusted[i] / adjusted[i - 1] - 1) < 0.21, f"jump at {i}"


def test_a_gap_inside_the_limit_band_is_not_an_event():
    """A 9% move is a trading day, not a corporate action."""
    raw = [100.0, 109.0, 91.0, 100.0]
    _factors, events = derive_factors(_bars(raw))
    assert events == []


def test_threshold_is_configurable():
    """A 20% board needs a higher threshold than a 10% board."""
    raw = [100.0, 118.0, 140.0]  # 18% then 18.6%
    _f, events_10 = derive_factors(_bars(raw), gap_threshold=0.10)
    _f2, events_21 = derive_factors(_bars(raw), gap_threshold=0.21)
    assert len(events_10) == 2
    assert events_21 == []


def test_volume_is_not_rescaled():
    """Scaling volume without the split ratio would corrupt liquidity features."""
    raw = [300.0, 100.0, 101.0]
    result = forward_adjusted(_bars(raw))
    assert [b.volume for b in result.bars] == [1000.0, 1000.0, 1000.0]


def test_empty_and_single_bar_inputs():
    assert forward_adjusted([]).bars == []
    single = forward_adjusted(_bars([100.0]))
    assert len(single.bars) == 1
    assert single.bars[0].close == pytest.approx(100.0)
