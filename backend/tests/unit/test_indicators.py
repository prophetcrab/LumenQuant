from __future__ import annotations

import math

import pytest

from app.modules.market.indicators import adx, atr, bollinger, ema, macd, rsi, sma, stochastic
from app.modules.market.service_types import CandlePoint


def make_candles(closes: list[float]) -> list[CandlePoint]:
    out = []
    previous = closes[0]
    for i, close in enumerate(closes):
        high = max(previous, close) * 1.01
        low = min(previous, close) * 0.99
        out.append(CandlePoint(time=i * 900_000, open=previous, high=high, low=low, close=close, volume=100 + i))
        previous = close
    return out


def test_sma_exact():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_exact():
    values = ema([1, 2, 3, 4, 5], 3)
    assert values[:2] == [None, None]
    assert values[2] == pytest.approx(2.0)
    assert values[3] == pytest.approx(3.0)
    assert values[4] == pytest.approx(4.0)


def test_rsi_monotonic_is_100():
    values = [float(i) for i in range(1, 30)]
    result = rsi(values, 14)
    assert result[13] is None
    assert all(v == pytest.approx(100.0) for v in result[14:])


def test_rsi_known_whipsaw():
    values = rsi([1, 2, 3, 2, 4], 2)
    assert values[2] == pytest.approx(100.0)
    assert values[3] == pytest.approx(50.0)
    assert values[4] == pytest.approx(83.3333, rel=1e-3)


def test_macd_alignment_and_histogram():
    closes = [100 + math.sin(i / 5) * 3 + i * 0.1 for i in range(120)]
    result = macd(closes)
    assert len(result["macd"]) == len(closes)
    for i in range(len(closes)):
        if result["histogram"][i] is not None:
            assert result["histogram"][i] == pytest.approx(result["macd"][i] - result["signal"][i])


def test_bollinger_constant_series_collapses():
    result = bollinger([10.0] * 30, 20, 2)
    assert result["middle"][19] == pytest.approx(10.0)
    assert result["upper"][19] == pytest.approx(10.0)
    assert result["lower"][19] == pytest.approx(10.0)


def test_atr_positive_after_warmup():
    candles = make_candles([100 + i * 0.7 for i in range(60)])
    values = atr(candles, 14)
    assert values[13] is None
    assert values[20] is not None and values[20] > 0


def test_stochastic_bounds():
    candles = make_candles([100 + math.sin(i / 3) * 5 for i in range(80)])
    result = stochastic(candles, 14, 3)
    for value in result["k"][20:]:
        assert 0.0 <= value <= 100.0


def test_adx_bounds():
    candles = make_candles([100 + math.sin(i / 4) * 8 + i * 0.05 for i in range(120)])
    result = adx(candles, 14)
    assert result["adx"][-1] is not None
    assert 0.0 <= result["adx"][-1] <= 100.0
    assert 0.0 <= result["plus_di"][-1] <= 100.0


def test_invalid_inputs():
    with pytest.raises(ValueError):
        sma([1, 2, 3], 0)
    with pytest.raises(ValueError):
        ema([1, 2, 3], -1)
