"""Indicator library: pure functions over aligned price series.

Every function returns a list with the same length as the input; warm-up slots
are None so charts and strategies can align without index arithmetic.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.modules.market.service_types import CandlePoint

Series = list[float | None]


def _empty(length: int) -> Series:
    return [None] * length


def sma(values: Sequence[float], period: int) -> Series:
    if period <= 0:
        raise ValueError("period must be positive")
    out = _empty(len(values))
    window = 0.0
    for i, value in enumerate(values):
        window += value
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    if period <= 0:
        raise ValueError("period must be positive")
    out = _empty(len(values))
    if len(values) < period:
        return out
    multiplier = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    previous = seed
    for i in range(period, len(values)):
        previous = (values[i] - previous) * multiplier + previous
        out[i] = previous
    return out


def rsi(values: Sequence[float], period: int = 14) -> Series:
    out = _empty(len(values))
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, Series]:
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)
    diff = _empty(len(values))
    for i in range(len(values)):
        if fast_line[i] is not None and slow_line[i] is not None:
            diff[i] = float(fast_line[i]) - float(slow_line[i])  # type: ignore[arg-type]
    compact: list[float] = [v for v in diff if v is not None]
    signal_compact = ema(compact, signal)
    signal_line: Series = _empty(len(values))
    cursor = 0
    for i, value in enumerate(diff):
        if value is None:
            continue
        signal_line[i] = signal_compact[cursor]
        cursor += 1
    histogram = _empty(len(values))
    for i in range(len(values)):
        if diff[i] is not None and signal_line[i] is not None:
            histogram[i] = float(diff[i]) - float(signal_line[i])  # type: ignore[arg-type]
    return {"macd": diff, "signal": signal_line, "histogram": histogram}


def bollinger(values: Sequence[float], period: int = 20, deviations: float = 2.0) -> dict[str, Series]:
    middle = sma(values, period)
    upper = _empty(len(values))
    lower = _empty(len(values))
    for i in range(len(values)):
        if middle[i] is None:
            continue
        window = values[i - period + 1 : i + 1]
        mean = float(middle[i])  # type: ignore[arg-type]
        variance = sum((v - mean) ** 2 for v in window) / period
        std = variance**0.5
        upper[i] = mean + deviations * std
        lower[i] = mean - deviations * std
    return {"middle": middle, "upper": upper, "lower": lower}


def true_range(candles: Sequence[CandlePoint]) -> Series:
    out = _empty(len(candles))
    for i, candle in enumerate(candles):
        if i == 0:
            out[i] = candle.high - candle.low
            continue
        prev_close = candles[i - 1].close
        out[i] = max(
            candle.high - candle.low,
            abs(candle.high - prev_close),
            abs(candle.low - prev_close),
        )
    return out


def atr(candles: Sequence[CandlePoint], period: int = 14) -> Series:
    tr = true_range(candles)
    out = _empty(len(candles))
    if len(candles) < period:
        return out
    first = sum(float(v) for v in tr[1 : period + 1]) / period
    out[period] = first
    previous = first
    for i in range(period + 1, len(candles)):
        previous = (previous * (period - 1) + float(tr[i])) / period  # type: ignore[arg-type]
        out[i] = previous
    return out


def stochastic(
    candles: Sequence[CandlePoint], period: int = 14, smooth: int = 3
) -> dict[str, Series]:
    k_raw = _empty(len(candles))
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1 : i + 1]
        highest = max(c.high for c in window)
        lowest = min(c.low for c in window)
        span = highest - lowest
        k_raw[i] = 50.0 if span == 0 else (candles[i].close - lowest) / span * 100.0
    k_compact = [float(v) for v in k_raw if v is not None]
    d_compact = sma(k_compact, smooth)
    k_line = _empty(len(candles))
    d_line = _empty(len(candles))
    cursor = 0
    for i, value in enumerate(k_raw):
        if value is None:
            continue
        k_line[i] = value
        d_line[i] = d_compact[cursor]
        cursor += 1
    return {"k": k_line, "d": d_line}


def adx(candles: Sequence[CandlePoint], period: int = 14) -> dict[str, Series]:
    """Wilder ADX with +DI / -DI."""
    length = len(candles)
    plus_di = _empty(length)
    minus_di = _empty(length)
    adx_line = _empty(length)
    if length < period * 2 + 1:
        return {"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di}

    plus_dm = [0.0] * length
    minus_dm = [0.0] * length
    tr = [0.0] * length
    for i in range(1, length):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        tr[i] = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )

    smooth_tr = sum(tr[1 : period + 1])
    smooth_plus = sum(plus_dm[1 : period + 1])
    smooth_minus = sum(minus_dm[1 : period + 1])
    dx = [0.0] * length
    for i in range(period, length):
        if i > period:
            smooth_tr = smooth_tr - smooth_tr / period + tr[i]
            smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i]
            smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i]
        plus = 100.0 * smooth_plus / smooth_tr if smooth_tr else 0.0
        minus = 100.0 * smooth_minus / smooth_tr if smooth_tr else 0.0
        plus_di[i] = plus
        minus_di[i] = minus
        total = plus + minus
        dx[i] = 100.0 * abs(plus - minus) / total if total else 0.0

    # ADX(2p) averages DX(p..2p-1); afterwards it smooths DX(i-1) at each step.
    previous = sum(dx[period : 2 * period]) / period
    adx_line[2 * period] = previous
    for i in range(2 * period + 1, length):
        previous = (previous * (period - 1) + dx[i - 1]) / period
        adx_line[i] = previous
    return {"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di}


def closes(candles: Sequence[CandlePoint]) -> list[float]:
    return [c.close for c in candles]


def compute(name: str, candles: Sequence[CandlePoint], params: dict[str, float]) -> dict[str, Series]:
    """Dispatch used by the API; unknown names raise ValueError."""
    values = closes(candles)
    if name == "sma":
        return {"sma": sma(values, int(params.get("period", 20)))}
    if name == "ema":
        return {"ema": ema(values, int(params.get("period", 20)))}
    if name == "rsi":
        return {"rsi": rsi(values, int(params.get("period", 14)))}
    if name == "macd":
        return macd(
            values,
            int(params.get("fast", 12)),
            int(params.get("slow", 26)),
            int(params.get("signal", 9)),
        )
    if name == "bollinger":
        return bollinger(
            values, int(params.get("period", 20)), float(params.get("deviations", 2.0))
        )
    if name == "atr":
        return {"atr": atr(candles, int(params.get("period", 14)))}
    if name == "stochastic":
        return stochastic(
            candles, int(params.get("period", 14)), int(params.get("smooth", 3))
        )
    if name == "adx":
        return adx(candles, int(params.get("period", 14)))
    raise ValueError(f"unknown indicator '{name}'")
