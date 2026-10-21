"""Stop-loss / take-profit / trailing-stop evaluation.

Shared by the backtester and the live runner so a strategy cannot behave
differently in research and in production — which it did before: the backtest
honoured stop_loss_pct/take_profit_pct while the automatic runner had no
protection at all.

All functions are pure and take prices in the same units as the position.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StopRules:
    """Fractional thresholds (0.05 == 5%). Zero disables a rule."""

    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    trailing_stop_pct: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.stop_loss_pct or self.take_profit_pct or self.trailing_stop_pct)

    def as_dict(self) -> dict[str, float]:
        return {
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "trailing_stop_pct": self.trailing_stop_pct,
        }


@dataclass(frozen=True)
class StopDecision:
    triggered: bool
    exit_price: float = 0.0
    reason: str = ""
    new_peak: float = 0.0

    @property
    def is_stop_loss(self) -> bool:
        return self.reason == "stop_loss"

    @property
    def is_take_profit(self) -> bool:
        return self.reason == "take_profit"

    @property
    def is_trailing_stop(self) -> bool:
        return self.reason == "trailing_stop"


BENIGN = StopDecision(triggered=False)


def update_peak(entry_price: float, current_peak: float, high: float) -> float:
    """Running highest price since entry, seeded from the entry price."""
    base = max(entry_price, current_peak or 0.0)
    return max(base, high)


def evaluate(
    *,
    entry_price: float,
    current_peak: float,
    bar_open: float,
    bar_high: float,
    bar_low: float,
    rules: StopRules,
) -> StopDecision:
    """Evaluate protective exits for one bar (or one live tick).

    Gaps are handled conservatively: if the bar opens beyond a stop level, the
    fill is the *open*, not the stop price, because that is what the market would
    actually have paid.

    Priority is stop-loss → trailing → take-profit. A bar that touches both a stop
    and a target is resolved as a stop (worst case for the trader), which avoids
    the classic "assume the good fill" backtest bias.
    """
    if entry_price <= 0 or not rules.enabled:
        return BENIGN

    # Peak *before* this bar. The trail must not be created by the very bar that
    # breaches it: OHLC gives no intra-bar ordering, so using this bar's high
    # would fire on any wide bar (high far above, low far below) and make trailing
    # stops unusable. Using the prior peak is the standard convention.
    peak_before = update_peak(entry_price, current_peak, entry_price)
    new_peak = max(peak_before, bar_high)

    if rules.stop_loss_pct > 0:
        stop_price = entry_price * (1 - rules.stop_loss_pct)
        if bar_low <= stop_price:
            fill = bar_open if bar_open < stop_price else stop_price
            return StopDecision(True, round_price_like(fill), "stop_loss", new_peak)

    if rules.trailing_stop_pct > 0:
        # Only armed once the position has actually advanced.
        if peak_before > entry_price:
            trail_price = peak_before * (1 - rules.trailing_stop_pct)
            if bar_low <= trail_price:
                fill = bar_open if bar_open < trail_price else trail_price
                return StopDecision(True, round_price_like(fill), "trailing_stop", new_peak)

    if rules.take_profit_pct > 0:
        target_price = entry_price * (1 + rules.take_profit_pct)
        if bar_high >= target_price:
            fill = bar_open if bar_open > target_price else target_price
            return StopDecision(True, round_price_like(fill), "take_profit", new_peak)

    return StopDecision(False, 0.0, "", new_peak)


def evaluate_tick(
    *,
    entry_price: float,
    current_peak: float,
    last_price: float,
    rules: StopRules,
) -> StopDecision:
    """Live-price variant: a tick has no OHLC, so the quote is both open and close."""
    return evaluate(
        entry_price=entry_price,
        current_peak=current_peak,
        bar_open=last_price,
        bar_high=last_price,
        bar_low=last_price,
        rules=rules,
    )


def round_price_like(value: float, decimals: int = 8) -> float:
    return round(float(value), decimals)


def parse_rules(
    *,
    stop_loss_pct: float = 0.0,
    take_profit_pct: float = 0.0,
    trailing_stop_pct: float = 0.0,
) -> StopRules:
    """Normalise percent-or-fraction input into fractions.

    Users (and older API payloads) sometimes send `5` meaning 5%. Anything above
    1 is treated as a percentage so `5` and `0.05` both mean five percent.
    """

    def normalise(value: float) -> float:
        if value <= 0:
            return 0.0
        return value / 100.0 if value > 1 else value

    return StopRules(
        stop_loss_pct=normalise(float(stop_loss_pct)),
        take_profit_pct=normalise(float(take_profit_pct)),
        trailing_stop_pct=normalise(float(trailing_stop_pct)),
    )
