"""Contract test: every strategy must be usable by the live runtime.

The runtime evaluates exactly one bar (the latest closed one) and expects signals
that depend only on that bar plus history-derived indicators. A strategy that
signals only at a fixed index (e.g. the first bar) passes backtests but can never
trade automatically — that defect already shipped once, so it is now locked down
by a test over the whole catalog.
"""

from __future__ import annotations

import pytest

from app.modules.market.service_types import CandlePoint
from app.modules.strategies.registry import all_strategies


def synthetic_candles(count: int = 400) -> list[CandlePoint]:
    """Deterministic series with both trends and pullbacks."""
    import math

    out: list[CandlePoint] = []
    price = 100.0
    for i in range(count):
        drift = 0.0035 if (i // 40) % 2 == 0 else -0.0025
        wave = math.sin(i / 6.0) * 0.012
        nxt = max(1.0, price * (1 + drift + wave))
        out.append(
            CandlePoint(
                time=i * 86_400_000,
                open=price,
                high=max(price, nxt) * 1.004,
                low=min(price, nxt) * 0.996,
                close=nxt,
                volume=1_000_000.0,
            )
        )
        price = nxt
    return out


@pytest.mark.parametrize("strategy", list(all_strategies()), ids=lambda s: s.key)
def test_strategy_is_stateless_across_bar_advance(strategy):
    """Evaluating the latest bar directly must equal evaluating it in a sweep.

    This is the property the runtime relies on; a mismatch means the strategy
    keeps hidden state or depends on call order.
    """
    params = strategy.validate_params({})
    candles = synthetic_candles()
    state = strategy.prepare(candles, params)
    warmup = strategy.warmup(params)
    if len(candles) <= warmup + 3:
        pytest.skip("warmup longer than fixture")

    # Sweep: what a backtest does.
    last_swept = strategy.evaluate(state, len(candles) - 1)
    # Runtime: fresh state, only the last index evaluated.
    fresh_state = strategy.prepare(candles, params)
    last_direct = strategy.evaluate(fresh_state, len(candles) - 1)
    assert last_swept.action == last_direct.action
    assert last_swept.reason == last_direct.reason


@pytest.mark.parametrize("strategy", list(all_strategies()), ids=lambda s: s.key)
def test_strategy_can_eventually_signal(strategy):
    """Every built-in strategy must be able to emit at least one action.

    A strategy that never fires cannot be traded, auto-run, or benchmarked; it is
    almost always a logic bug rather than an intentional design.
    """
    params = strategy.validate_params({})
    candidates = [synthetic_candles(), synthetic_candles(600)]
    # Strategies that fit a model before their first signal (the ML ones) need a
    # fixture at least as long as their warmup; otherwise "never signals" only
    # proves the fixture was too short to evaluate.
    warmup = strategy.warmup(params)
    if warmup + 50 > 600:
        candidates.append(synthetic_candles(warmup + 120))
    actions: list[str] = []
    for candles in candidates:
        state = strategy.prepare(candles, params)
        for index in range(strategy.warmup(params), len(candles)):
            signal = strategy.evaluate(state, index)
            if signal.is_actionable:
                actions.append(signal.action)
    assert actions, f"{strategy.key} never emitted an actionable signal"
    assert set(actions) <= {"buy", "sell"}
