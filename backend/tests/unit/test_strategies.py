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
    # Give every strategy a series long enough to clear its own warmup. A model
    # that must be fitted before its first signal (the ML strategy) otherwise
    # looks like it "produces no signals" purely because the fixture was short.
    warmup = strategy.warmup(parsed)
    candles = trending_candles(max(400, warmup + 150))
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
    "xgb_hybrid",
}


def test_catalog_contains_all_builtins():
    keys = {meta["key"] for meta in catalog()}
    assert keys == EXPECTED_KEYS
    assert len(list(all_strategies())) == 10


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


def test_buy_and_hold_signals_every_bar_from_warmup():
    """The signal is intentionally repeatable.

    The runtime evaluates only the latest bar, so a "fire once at index 1"
    signal would never reach a live instance. The backtester's single-position
    rule prevents repeated entries.
    """
    signals = run_strategy("buy_and_hold")
    assert len(signals) == len(trending_candles()) - 1
    assert all(action == "buy" for _, signal in signals for action in [signal.action])


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


def test_ema_trend_enters_when_adx_confirms_after_cross():
    """ADX often crosses its threshold *after* price crossed the EMA.

    The original rule required both on the same bar and produced zero entries on
    real data; the second trigger must cover the trend-confirmation case.
    """
    from app.modules.strategies.library.classics import EmaTrend

    strategy = EmaTrend()
    params = strategy.validate_params({"ema_period": 10, "adx_period": 5, "adx_threshold": 18})
    candles = trending_candles(300)
    state = strategy.prepare(candles, params)
    signals = [
        strategy.evaluate(state, i) for i in range(strategy.warmup(params), len(candles))
    ]
    buys = [s for s in signals if s.action == "buy"]
    assert buys, "ema_trend produced no entry on a trending series"
    assert any("ADX" in s.reason for s in buys)


def test_ema_trend_regime_filter_avoids_bear_market_entries():
    """A long-only trend strategy must not buy inside a downtrend.

    Real 茅台 data (800 bars, -19.7% buy-and-hold) produced 24 trades and a 4%
    win rate before the regime filter; ADX measures strength, not direction.
    """
    strategy = get("ema_trend")
    candles = trending_candles(600)
    with_filter = strategy.validate_params({"require_regime": True})
    without = strategy.validate_params({"require_regime": False})

    def count_buys(params) -> int:
        state = strategy.prepare(candles, params)
        return sum(
            1
            for i in range(strategy.warmup(params), len(candles))
            if strategy.evaluate(state, i).action == "buy"
        )

    filtered = count_buys(with_filter)
    unfiltered = count_buys(without)
    assert filtered <= unfiltered, "regime filter must not increase entries"
    # The series starts with a downtrend, so the filter must suppress entries there.
    state = strategy.prepare(candles, with_filter)
    regime = state.series["regime"]
    early_buys = [
        i
        for i in range(strategy.warmup(with_filter), 200)
        if strategy.evaluate(state, i).action == "buy"
    ]
    assert all(
        regime[i] is not None and candles[i].close > regime[i] for i in early_buys
    )


def test_ema_trend_regime_is_configurable():
    strategy = get("ema_trend")
    defaults = strategy.Params()
    # ema_trend ships with the filter ON: without it the real-data win rate was 4%.
    assert defaults.require_regime is True
    assert defaults.regime_period == 120


def test_regime_filter_defaults_differ_by_strategy():
    """sma_cross/donchian keep the filter off so short series remain usable."""
    assert get("sma_cross").Params().require_regime is False
    assert get("donchian_breakout").Params().require_regime is False
