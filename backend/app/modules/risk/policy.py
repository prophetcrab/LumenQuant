"""Pre-trade risk policy and account-level circuit breakers.

Pure functions plus a small quota object: the trading module owns enforcement,
this module owns the rules. No database or network access here, which keeps the
rules trivially testable and reusable by backtests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.errors import ConflictError, ValidationError
from app.core.money import round_money, safe_div
from app.modules.market.service_types import SymbolView


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float = 1_000_000.0
    max_position_notional: float = 5_000_000.0
    min_order_notional: float = 10.0
    max_orders_per_minute: int = 120
    max_daily_loss_pct: float = 0.0  # 0 disables
    max_drawdown_pct: float = 0.0  # 0 disables


@dataclass(frozen=True)
class OrderIntent:
    side: str
    quantity: float
    price: float
    fee_bps: float


@dataclass(frozen=True)
class RiskContext:
    cash: float
    # Shares that may be sold right now (post T+1, minus reservations). This is
    # the sell-side guard and must stay separate from exposure.
    position_qty: float
    position_avg_price: float
    orders_last_minute: int
    equity: float = 0.0
    day_start_equity: float = 0.0
    peak_equity: float = 0.0
    # Total holding for exposure purposes. For A-shares this exceeds
    # `position_qty` on the day of purchase: the shares are owned but not yet
    # sellable. Valuing exposure from `position_qty` made the notional limit
    # disappear entirely for same-day A-share buying.
    exposure_qty: float = 0.0
    # Current market price of the existing position. The notional limit is about
    # *exposure*, so it must value the holding at today's price: cost-based
    # valuation understates the risk of adding to a position that has run up.
    # Zero means "unknown", and the policy then falls back to cost.
    position_market_price: float = 0.0


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""


class RiskPolicy:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def check(self, intent: OrderIntent, ctx: RiskContext, symbol: SymbolView) -> RiskDecision:
        """Non-raising evaluation used by preflight and the UI."""
        try:
            self.validate(intent, ctx, symbol)
        except (ValidationError, ConflictError) as exc:
            return RiskDecision(False, exc.message)
        return RiskDecision(True)

    def breaker(self, ctx: RiskContext) -> RiskDecision:
        """Account-level kill switch: daily loss and peak drawdown."""
        limits = self.limits
        if limits.max_daily_loss_pct and ctx.day_start_equity > 0:
            change = safe_div(ctx.equity - ctx.day_start_equity, ctx.day_start_equity)
            if change <= -limits.max_daily_loss_pct:
                return RiskDecision(
                    False,
                    f"daily loss limit hit ({change * 100:.2f}% <= -{limits.max_daily_loss_pct * 100:.2f}%)",
                )
        if limits.max_drawdown_pct and ctx.peak_equity > 0:
            drawdown = safe_div(ctx.peak_equity - ctx.equity, ctx.peak_equity)
            if drawdown >= limits.max_drawdown_pct:
                return RiskDecision(
                    False,
                    f"drawdown circuit breaker ({drawdown * 100:.2f}% >= {limits.max_drawdown_pct * 100:.2f}%)",
                )
        return RiskDecision(True)

    def validate(self, intent: OrderIntent, ctx: RiskContext, symbol: SymbolView) -> None:
        if intent.quantity <= 0:
            raise ValidationError("quantity must be positive")
        notional = intent.quantity * intent.price
        if notional < self.limits.min_order_notional:
            raise ValidationError(
                f"order notional {round_money(notional)} is below the minimum "
                f"{self.limits.min_order_notional}"
            )
        if notional > self.limits.max_order_notional:
            raise ConflictError(
                f"order notional {round_money(notional)} exceeds the limit "
                f"{self.limits.max_order_notional}"
            )
        if ctx.orders_last_minute >= self.limits.max_orders_per_minute:
            raise ConflictError("order rate limit reached, slow down")

        if symbol.lot_size > 0:
            steps = intent.quantity / symbol.lot_size
            if abs(steps - round(steps)) > 1e-6:
                raise ValidationError(f"quantity must be a multiple of lot size {symbol.lot_size}")

        if intent.side == "buy":
            cost = notional + notional * intent.fee_bps / 10_000.0
            if cost > ctx.cash + 1e-9:
                raise ConflictError(
                    f"insufficient cash: need {round_money(cost)}, available {round_money(ctx.cash)}"
                )
            # Exposure is quantity × *market* value, not quantity × cost. A
            # position bought at 100 and now trading at 200 carries twice the risk
            # the cost figure implies, so a cost-based projection let a buy double
            # the real exposure while appearing to stay under the limit.
            mark = ctx.position_market_price or ctx.position_avg_price
            held = ctx.exposure_qty if ctx.exposure_qty > 0 else ctx.position_qty
            projected = held * mark + notional
            if projected > self.limits.max_position_notional:
                raise ConflictError(
                    f"projected position value {round_money(projected)} exceeds "
                    f"the notional limit {self.limits.max_position_notional}"
                )
        else:
            if intent.quantity > ctx.position_qty + 1e-9:
                raise ConflictError(
                    f"insufficient position: hold {ctx.position_qty}, sell {intent.quantity}"
                )
