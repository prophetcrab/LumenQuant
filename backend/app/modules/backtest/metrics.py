"""Performance statistics computed from an equity curve and trade list."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.money import safe_div


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


def periods_per_year(timeframe: str) -> float:
    mapping = {
        "1m": 365 * 24 * 60,
        "5m": 365 * 24 * 12,
        "15m": 365 * 24 * 4,
        "30m": 365 * 24 * 2,
        "1h": 365 * 24,
        "4h": 365 * 6,
        "1d": 365,
    }
    return float(mapping.get(timeframe, 365 * 24))


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
) -> dict:
    final = equity[-1] if equity else initial
    total_return = safe_div(final - initial, initial)
    returns = [
        safe_div(equity[i] - equity[i - 1], equity[i - 1])
        for i in range(1, len(equity))
        if equity[i - 1] > 0
    ]
    ppy = periods_per_year(timeframe)
    mean_return = sum(returns) / len(returns) if returns else 0.0
    volatility = _std(returns)
    sharpe = safe_div(mean_return, volatility) * math.sqrt(ppy) if volatility else 0.0
    downside = [r for r in returns if r < 0]
    downside_std = _std(downside) if len(downside) > 1 else 0.0
    sortino = safe_div(mean_return, downside_std) * math.sqrt(ppy) if downside_std else 0.0

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
    calmar = safe_div(cagr, max_drawdown) if max_drawdown else 0.0

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
        "cagr_pct": round(cagr * 100, 4),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "max_drawdown_duration_bars": max_drawdown_duration,
        "volatility_pct": round(volatility * math.sqrt(ppy) * 100, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "calmar": round(calmar, 4),
        "trade_count": len(trades),
        "win_rate_pct": round(safe_div(len(wins), len(trades)) * 100, 4),
        "profit_factor": profit_factor_display,
        "avg_trade_pct": round(
            safe_div(sum(t.pnl_pct for t in trades), len(trades)) * 100, 4
        ),
        "best_trade_pct": round(max((t.pnl_pct for t in trades), default=0.0) * 100, 4),
        "worst_trade_pct": round(min((t.pnl_pct for t in trades), default=0.0) * 100, 4),
        "avg_hold_bars": round(safe_div(sum((t.exit_time - t.entry_time) for t in trades), len(trades)), 2),
        "exposure_pct": round(safe_div(bars_in_market, len(equity)) * 100, 4),
        "bars": len(equity),
    }


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()
