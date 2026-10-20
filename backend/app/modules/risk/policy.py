"""Pre-trade risk policy.

Pure functions and dataclasses only: the trading module owns enforcement, this
module owns the rules. R12 extends this with daily-loss and drawdown breakers.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import ConflictError, ValidationError
from app.core.money import round_money
from app.modules.market.service_types import SymbolView


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float = 1_000_000.0
    max_position_notional: float = 5_000_000.0
    min_order_notional: float = 10.0
    max_orders_per_minute: int = 120


@dataclass(frozen=True)
class OrderIntent:
    side: str
    quantity: float
    price: float
    fee_bps: float


@dataclass(frozen=True)
class RiskContext:
    cash: float
    position_qty: float
    position_avg_price: float
    orders_last_minute: int


class RiskPolicy:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

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
            projected = ctx.position_qty * ctx.position_avg_price + notional
            if projected > self.limits.max_position_notional:
                raise ConflictError("projected position exceeds the notional limit")
        else:
            if intent.quantity > ctx.position_qty + 1e-9:
                raise ConflictError(
                    f"insufficient position: hold {ctx.position_qty}, sell {intent.quantity}"
                )
