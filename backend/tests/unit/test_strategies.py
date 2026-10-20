from __future__ import annotations

import random

import pytest

from app.core.errors import NotFoundError, ValidationError
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.registry import all_strategies, catalog, get


def trending_candles(count: int = 400) -> list[CandlePoint]:
    """Regime-switching walk with enough noise for crossings and breakouts."""
    rng = random.Random(20261020)
    candle_list = []
    price = 100.0
    for i in range(count):
        drift = -0.0012 if i < 150 else 0.0018
        previous = price
        price = max(1.0, price * (1.0 + drift + rng.gauss(0.0, 0.006)))
        high = max(previous, price) * (1.0 + abs(rng.gauss(0.0, 0.002)))
        low = min(previous, price) * (1.0 - abs(rng.gauss(0.0, 0.002)))
        candle_list.append(
            CandlePoint(
                time=i * 900_000,
                open=previous,
                high=high,
                low=low,
                close=price,
                volume=1000 + rng.random() * 500,
            )
        )
    return candle_list


def run_strategy(key: str, params: dict | None = None):
    strategy = get(key)
    parsed = strategy.validate_params(params)
    candles = trending_candles()
    state = strategy.prepare(candles, parsed)
    signals = []
    for index in range(strategy.warmup(parsed), len(candles)):
        signal = strategy.evaluate(state, index)
        if signal.is_actionable:
            signals.append((index, signal))
    return signals


EXPECTED_KEYS = {
    "sma_cross",
    "ema_trend",
    "rsi_reversion",
    "bollinger_reversion",
    "macd_momentum",
    "donchian_breakout",
    "atr_channel",
    "stochastic_reversal",
    "buy_and_hold",
}


def test_catalog_contains_all_builtins():
    keys = {meta["key"] for meta in catalog()}
    assert keys == EXPECTED_KEYS
    assert len(list(all_strategies())) == 9


def test_catalog_exposes_param_schema():
    for meta in catalog():
        assert meta["params_schema"]["type"] == "object"
        assert isinstance(meta["defaults"], dict)
        assert meta["name"]
        assert meta["description"]


@pytest.mark.parametrize("key", sorted(EXPECTED_KEYS - {"buy_and_hold"}))
def test_every_strategy_produces_signals_on_trending_data(key):
    signals = run_strategy(key)
    assert signals, f"{key} produced no signals"


def test_buy_and_hold_single_entry():
    signals = run_strategy("buy_and_hold")
    assert len(signals) == 1
    assert signals[0][1].action == "buy"


def test_sma_cross_obeys_warmup():
    signals = run_strategy("sma_cross", {"fast": 5, "slow": 20})
    assert all(index >= 21 for index, _ in signals)


def test_invalid_params_rejected():
    strategy = get("sma_cross")
    with pytest.raises(Exception):
        strategy.validate_params({"fast": 100, "slow": 10})
    with pytest.raises(Exception):
        strategy.validate_params({"unknown": 1})


def test_unknown_strategy():
    with pytest.raises(NotFoundError):
        get("grail")
