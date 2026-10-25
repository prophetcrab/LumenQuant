"""The hybrid strategy must be causal and must actually fire.

The dangerous failure for this class is look-ahead: `prepare` sees the whole
series, so any full-sample statistic (a global z-score, a normalisation using the
last price) leaks the future and produces a beautiful, fictional equity curve.
The truncation test below is the direct check: recompute on a prefix and assert
every score in that prefix is bit-identical.
"""

from __future__ import annotations

import math
import random

import pytest

from app.modules.market.service_types import CandlePoint
from app.modules.strategies.library.cn_hybrid import CnMultiHorizonHybrid
from app.modules.strategies.registry import get


def _series(count: int, *, seed: int = 7, drift: float = 0.0004) -> list[CandlePoint]:
    """Regime-switching walk with volume clustering — enough structure to score."""
    rng = random.Random(seed)
    candles: list[CandlePoint] = []
    price = 50.0
    for i in range(count):
        regime = 0.0012 if (i // 200) % 2 == 0 else -0.0008
        step = regime + drift + rng.gauss(0.0, 0.016)
        prev = price
        price = max(1.0, price * (1.0 + step))
        volume = 1_000_000 * (1.0 + abs(step) * 20) * (0.7 + rng.random() * 0.6)
        candles.append(
            CandlePoint(
                time=1_600_000_000_000 + i * 86_400_000,
                open=prev,
                high=max(prev, price) * 1.004,
                low=min(prev, price) * 0.996,
                close=price,
                volume=volume,
            )
        )
    return candles


@pytest.fixture(scope="module")
def candles() -> list[CandlePoint]:
    return _series(1200)


def test_registered_and_catalogued():
    strategy = get("cn_hybrid")
    assert isinstance(strategy, CnMultiHorizonHybrid)
    assert strategy.category == "hybrid"
    meta = strategy.meta()
    # The frontend only renders bool/checkbox and numeric inputs.
    for name, schema in meta["params_schema"]["properties"].items():
        assert schema["type"] in {"boolean", "number", "integer"}, name
    assert meta["defaults"]["entry_score"] > meta["defaults"]["exit_score"], (
        "entry threshold must sit above the exit threshold, otherwise the signal "
        "oscillates every bar and pays the round trip repeatedly"
    )


def test_warmup_is_honest(candles):
    strategy = get("cn_hybrid")
    params = strategy.validate_params({})
    warmup = strategy.warmup(params)
    state = strategy.prepare(candles, params)
    # Nothing may be scored before the warmup, and something must be after.
    assert all(v is None for v in state.series["score"][:60])
    assert any(v is not None for v in state.series["score"][warmup:])


def test_no_look_ahead(candles):
    """Truncating the future must not change any earlier score."""
    strategy = get("cn_hybrid")
    params = strategy.validate_params({})
    full = strategy.prepare(candles, params)
    cutoff = 800
    truncated = strategy.prepare(candles[:cutoff], params)

    for i in range(cutoff):
        a = full.series["score"][i]
        b = truncated.series["score"][i]
        if a is None or b is None:
            assert a is None and b is None, f"bar {i}: presence differs"
            continue
        assert abs(a - b) < 1e-9, f"bar {i}: score changed ({a} vs {b})"

        sa = full.series["strength"][i]
        sb = truncated.series["strength"][i]
        if sa is not None and sb is not None:
            assert abs(sa - sb) < 1e-9, f"bar {i}: strength changed"


def test_emits_both_directions(candles):
    """A strategy that only ever buys (or only ever sells) cannot be traded."""
    strategy = get("cn_hybrid")
    params = strategy.validate_params({})
    state = strategy.prepare(candles, params)
    actions = {
        strategy.evaluate(state, i).action
        for i in range(strategy.warmup(params), len(candles))
    }
    assert actions == {"buy", "sell", "hold"}, actions


def test_strength_is_bounded_and_varies(candles):
    """Volatility targeting must stay inside [min_strength, 1] and actually move."""
    strategy = get("cn_hybrid")
    params = strategy.validate_params({})
    state = strategy.prepare(candles, params)
    values = [v for v in state.series["strength"] if v is not None]
    assert values
    assert min(values) >= params.min_strength - 1e-9
    assert max(values) <= 1.0 + 1e-9, "must never imply leverage"
    # If it never varies, the sizing is a constant and adds nothing.
    assert max(values) - min(values) > 0.05


def test_overbought_filter_blocks_entries(candles):
    """RSI above the threshold must suppress the entry, not the whole score."""
    strategy = get("cn_hybrid")
    params = strategy.validate_params({"overbought_rsi": 50.0})
    state = strategy.prepare(candles, params)
    blocked = 0
    for i in range(strategy.warmup(params), len(candles)):
        rsi = state.series["rsi_14"][i]
        signal = strategy.evaluate(state, i)
        if (
            signal.action == "hold"
            and rsi is not None
            and rsi > 50.0
            and "追高回避" in signal.reason
        ):
            blocked += 1
    assert blocked > 0, "an aggressive overbought threshold must block some entries"


def test_family_switches_change_the_score(candles):
    """Turning a family off must change the result, not be silently ignored."""
    strategy = get("cn_hybrid")
    both = strategy.prepare(
        candles, strategy.validate_params({"use_trend": True, "use_liquidity": True})
    )
    trend_only = strategy.prepare(
        candles, strategy.validate_params({"use_trend": True, "use_liquidity": False})
    )
    differences = sum(
        1
        for a, b in zip(both.series["score"], trend_only.series["score"])
        if a is not None and b is not None and abs(a - b) > 1e-9
    )
    assert differences > 0, "disabling the liquidity family had no effect"


def test_all_families_off_yields_no_score(candles):
    strategy = get("cn_hybrid")
    params = strategy.validate_params({"use_trend": False, "use_liquidity": False})
    state = strategy.prepare(candles, params)
    assert all(v is None for v in state.series["score"])


def test_scores_are_bounded(candles):
    """The tanh squash keeps thresholds comparable across symbols."""
    strategy = get("cn_hybrid")
    state = strategy.prepare(candles, strategy.validate_params({}))
    for value in state.series["score"]:
        if value is None:
            continue
        assert math.isfinite(value)
        assert -1.0 <= value <= 1.0
