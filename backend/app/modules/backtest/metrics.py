"""Performance statistics computed from an equity curve and trade list."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.money import safe_div
from app.modules.market.feed import timeframe_ms


@dataclass(frozen=True)
class TradeRecord:
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    fee: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry_price": round(self.entry_price, 6),
            "exit_price": round(self.exit_price, 6),
            "quantity": round(self.quantity, 8),
            "pnl": round(self.pnl, 4),
            "pnl_pct": round(self.pnl_pct, 4),
            "fee": round(self.fee, 4),
            "reason": self.reason,
        }


# Bars per year for a 24/7 market versus a 4-hour A-share session day.
CRYPTO_BARS_PER_YEAR = {
    "1m": 365 * 24 * 60,
    "5m": 365 * 24 * 12,
    "15m": 365 * 24 * 4,
    "30m": 365 * 24 * 2,
    "1h": 365 * 24,
    "4h": 365 * 6,
    "1d": 365,
}
# A-share: 242 trading days, 4h per day -> 16 x 15m bars, 4 x 1h bars.
CN_TRADING_DAYS = 242
CN_BARS_PER_YEAR = {
    "15m": CN_TRADING_DAYS * 16,
    "30m": CN_TRADING_DAYS * 8,
    "1h": CN_TRADING_DAYS * 4,
    "4h": CN_TRADING_DAYS,
    "1d": CN_TRADING_DAYS,
}


def periods_per_year(timeframe: str, market: str = "crypto") -> float:
    if market == "cn_stock":
        return float(CN_BARS_PER_YEAR.get(timeframe, CN_TRADING_DAYS * 4))
    return float(CRYPTO_BARS_PER_YEAR.get(timeframe, 365 * 24))


# How much history an annualised figure needs before it says anything useful.
# Below one year, compounding is extrapolation: the same +187% that is real over
# 15 days becomes a 5e13% "annual return", and every ratio built on it (Sharpe,
# Sortino, Calmar, alpha) is scaled by the same factor. Half a year is the point
# where a run covers at least one full seasonal cycle in practice; anything
# shorter reports the unannualised total return and leaves the annualised fields
# empty.
MIN_YEARS_FOR_ANNUALISED = 0.5

# Below this per-bar standard deviation, a Sharpe/Sortino is dividing by rounding
# noise rather than measuring risk. 1e-9 per bar is far under any real market's
# variation (a 15m BTC bar rarely moves less than 1e-4) and far above float error.
MIN_VOLATILITY_FOR_RATIO = 1e-9


def _annualised(value: float, meaningful: bool) -> float | None:
    """Serialize an annualised metric, or None when the sample is too short.

    `None` rather than NaN: the metric travels through JSON, where NaN is not
    valid, and the UI renders a missing value as "—" instead of a number.
    """
    if not meaningful or math.isnan(value) or math.isinf(value):
        return None
    return round(value, 4)


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def compute_metrics(
    *,
    initial: float,
    equity: list[float],
    trades: list[TradeRecord],
    timeframe: str,
    bars_in_market: int,
    market: str = "crypto",
) -> dict:
    final = equity[-1] if equity else initial
    total_return = safe_div(final - initial, initial)
    returns = [
        safe_div(equity[i] - equity[i - 1], equity[i - 1])
        for i in range(1, len(equity))
        if equity[i - 1] > 0
    ]
    ppy = periods_per_year(timeframe, market)
    mean_return = sum(returns) / len(returns) if returns else 0.0
    volatility = _std(returns)
    # A volatility floor, not just a zero check. A curve with almost no
    # bar-to-bar variation (a straight-line synthetic series, or a strategy that
    # sat in cash) yields a denominator of ~1e-17 and a Sharpe in the 1e14 range.
    # That is division by rounding noise, not a risk-adjusted result.
    sharpe = (
        safe_div(mean_return, volatility) * math.sqrt(ppy)
        if volatility > MIN_VOLATILITY_FOR_RATIO
        else 0.0
    )
    downside = [r for r in returns if r < 0]
    downside_std = _std(downside) if len(downside) > 1 else 0.0
    sortino = (
        safe_div(mean_return, downside_std) * math.sqrt(ppy)
        if downside_std > MIN_VOLATILITY_FOR_RATIO
        else 0.0
    )

    peak = equity[0] if equity else initial
    max_drawdown = 0.0
    max_drawdown_duration = 0
    current_duration = 0
    for value in equity:
        if value >= peak:
            peak = value
            current_duration = 0
        else:
            current_duration += 1
            max_drawdown_duration = max(max_drawdown_duration, current_duration)
            drawdown = safe_div(peak - value, peak)
            max_drawdown = max(max_drawdown, drawdown)

    growth = safe_div(final, initial) if initial else 0.0
    years = safe_div(len(equity), ppy)
    cagr = growth ** (1 / years) - 1 if growth > 0 and years > 0 else 0.0
    # Compounding a few days of returns up to a year is arithmetic, not a
    # forecast. 1500 15m bars is 15.6 days; +187% over that span extrapolates to
    # ~5e13 % a year, and every annualised figure (CAGR, volatility, Sharpe,
    # Sortino, Calmar, alpha) inherits the same distortion. Below the threshold
    # the annualised values are withheld rather than shown as if they meant
    # something — the unannualised total return is still exact.
    annualised_is_meaningful = years >= MIN_YEARS_FOR_ANNUALISED
    if not annualised_is_meaningful:
        cagr = float("nan")
    calmar = safe_div(cagr, max_drawdown) if max_drawdown and annualised_is_meaningful else float("nan")

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    if not trades:
        profit_factor_display = None  # undefined without any closed trade
    elif gross_loss == 0:
        profit_factor_display = None  # infinite; the UI renders it as ∞
    else:
        profit_factor_display = round(gross_profit / gross_loss, 4)

    return {
        "initial_capital": round(initial, 2),
        "final_equity": round(final, 2),
        "total_return_pct": round(total_return * 100, 4),
        "cagr_pct": _annualised(cagr * 100, annualised_is_meaningful),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "max_drawdown_duration_bars": max_drawdown_duration,
        "volatility_pct": _annualised(volatility * math.sqrt(ppy) * 100, annualised_is_meaningful),
        "sharpe": _annualised(sharpe, annualised_is_meaningful),
        "sortino": _annualised(sortino, annualised_is_meaningful),
        "calmar": _annualised(calmar, annualised_is_meaningful),
        "trade_count": len(trades),
        "win_rate_pct": round(safe_div(len(wins), len(trades)) * 100, 4),
        "profit_factor": profit_factor_display,
        "avg_trade_pct": round(
            safe_div(sum(t.pnl_pct for t in trades), len(trades)) * 100, 4
        ),
        "best_trade_pct": round(max((t.pnl_pct for t in trades), default=0.0) * 100, 4),
        "worst_trade_pct": round(min((t.pnl_pct for t in trades), default=0.0) * 100, 4),
        # Timestamps are epoch milliseconds; convert to bar counts.
        "avg_hold_bars": round(
            safe_div(
                sum((t.exit_time - t.entry_time) for t in trades),
                len(trades) * timeframe_ms(timeframe),
            ),
            2,
        ),
        "exposure_pct": round(safe_div(bars_in_market, len(equity)) * 100, 4),
        "bars": len(equity),
        "market": market,
        "periods_per_year": round(ppy, 2),
        # How long the sample actually spans, so the reader can judge whether an
        # annualised figure is credible without recomputing it from `bars`.
        # Calendar days for crypto; trading days for A-shares (where 242 days
        # make a "year", so the two are not directly comparable).
        "sample_days": round(
            years * (CN_TRADING_DAYS if market == "cn_stock" else 365), 2
        ),
        "annualised_available": annualised_is_meaningful,
    }


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()
