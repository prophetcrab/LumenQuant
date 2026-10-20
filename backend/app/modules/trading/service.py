"""Order lifecycle, matching and positions.

All writes for one order action happen in a single transaction: order row, fill,
position, cash ledger and audit. The service never imports another module's ORM
models; it talks to `CashPort` and `PricePort`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.events import EventBus, OrderFilled
from app.core.money import round_money, round_price, round_qty
from app.core.timeutil import iso, utcnow
from app.modules.risk.policy import OrderIntent, RiskContext, RiskPolicy
from app.modules.brokers.base import BrokerOrderRequest, BrokerPort
from app.modules.risk.service import RiskService
from app.modules.trading.models import Fill, Order, Position, new_id
from app.modules.trading.ports import CashPort, PricePort

OPEN_STATUSES = ("new", "open", "partially_filled")
TERMINAL_STATUSES = ("filled", "cancelled", "rejected")


@dataclass(frozen=True)
class OrderView:
    id: str
    account_id: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    limit_price: float | None
    filled_quantity: float
    avg_fill_price: float
    fee: float
    status: str
    source: str
    client_order_id: str | None
    reject_reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class FillView:
    id: str
    order_id: str
    account_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    created_at: str


@dataclass(frozen=True)
class PositionView:
    account_id: str
    symbol: str
    quantity: float
    avg_price: float
    realized_pnl: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    updated_at: str


class TradingService:
    def __init__(
        self,
        session: Session,
        *,
        cash: CashPort,
        prices: PricePort,
        policy: RiskPolicy | None = None,
        bus: EventBus | None = None,
        fee_bps: float = 10.0,
        slippage_bps: float = 3.0,
        risk: RiskService | None = None,
        broker: BrokerPort | None = None,
    ) -> None:
        self.session = session
        self.cash = cash
        self.prices = prices
        self.policy = policy or RiskPolicy()
        self.bus = bus
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.risk = risk
        self.broker = broker

    # ---------------------------------------------------------------- views
    def _order_view(self, order: Order) -> OrderView:
        return OrderView(
            id=order.id,
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            limit_price=order.limit_price,
            filled_quantity=order.filled_quantity,
            avg_fill_price=order.avg_fill_price,
            fee=round_money(order.fee),
            status=order.status,
            source=order.source,
            client_order_id=order.client_order_id,
            reject_reason=order.reject_reason,
            created_at=iso(order.created_at),
            updated_at=iso(order.updated_at),
        )

    def _fill_view(self, fill: Fill) -> FillView:
        return FillView(
            id=fill.id,
            order_id=fill.order_id,
            account_id=fill.account_id,
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.quantity,
            price=fill.price,
            fee=round_money(fill.fee),
            created_at=iso(fill.created_at),
        )

    def _position(self, account_id: str, symbol: str) -> Position | None:
        return self.session.scalar(
            select(Position).where(
                Position.account_id == account_id, Position.symbol == symbol.upper()
            )
        )

    def _position_view(self, position: Position) -> PositionView:
        price = self._safe_price(position.symbol)
        market_value = round_money(position.quantity * price)
        unrealized = round_money((price - position.avg_price) * position.quantity)
        return PositionView(
            account_id=position.account_id,
            symbol=position.symbol,
            quantity=position.quantity,
            avg_price=position.avg_price,
            realized_pnl=round_money(position.realized_pnl),
            market_price=price,
            market_value=market_value,
            unrealized_pnl=unrealized,
            updated_at=iso(position.updated_at),
        )

    def _safe_price(self, symbol: str) -> float:
        try:
            return self.prices.price(symbol)
        except Exception:
            return 0.0

    def list_orders(
        self, account_id: str, *, status: str | None = None, limit: int = 200
    ) -> list[OrderView]:
        stmt = select(Order).where(Order.account_id == account_id)
        if status:
            if status not in OPEN_STATUSES + TERMINAL_STATUSES:
                raise ValidationError(f"unknown order status {status}")
            stmt = stmt.where(Order.status == status)
        rows = self.session.scalars(
            stmt.order_by(Order.created_at.desc()).limit(max(1, min(limit, 1000)))
        ).all()
        return [self._order_view(o) for o in rows]

    def open_orders(self, *, account_id: str | None = None) -> list[Order]:
        stmt = select(Order).where(Order.status.in_(OPEN_STATUSES))
        if account_id:
            stmt = stmt.where(Order.account_id == account_id)
        return list(self.session.scalars(stmt.order_by(Order.created_at.asc())).all())

    def positions(self, account_id: str) -> list[PositionView]:
        rows = self.session.scalars(
            select(Position)
            .where(Position.account_id == account_id, Position.quantity > 0)
            .order_by(Position.symbol.asc())
        ).all()
        return [self._position_view(p) for p in rows]

    def fills(self, account_id: str, *, limit: int = 100) -> list[FillView]:
        rows = self.session.scalars(
            select(Fill)
            .where(Fill.account_id == account_id)
            .order_by(Fill.created_at.desc())
            .limit(max(1, min(limit, 1000)))
        ).all()
        return [self._fill_view(f) for f in rows]

    # ---------------------------------------------------------------- place
    def place_order(
        self,
        *,
        account_id: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        limit_price: float | None = None,
        source: str = "manual",
        client_order_id: str | None = None,
    ) -> OrderView:
        side = side.lower()
        order_type = order_type.lower()
        symbol = symbol.upper().strip()
        if side not in {"buy", "sell"}:
            raise ValidationError("side must be buy or sell")
        if order_type not in {"market", "limit"}:
            raise ValidationError("order_type must be market or limit")
        quantity = round_qty(quantity)
        if quantity <= 0:
            raise ValidationError("quantity must be positive")
        if order_type == "limit":
            if limit_price is None or limit_price <= 0:
                raise ValidationError("limit orders require a positive limit_price")
            limit_price = round_price(limit_price)
        else:
            limit_price = None
        self.cash.ensure_active(account_id)

        if client_order_id:
            existing = self.session.scalar(
                select(Order).where(
                    Order.account_id == account_id, Order.client_order_id == client_order_id
                )
            )
            if existing is not None:
                return self._order_view(existing)

        symbol_view = self.prices.symbol(symbol)
        market_price = self.prices.price(symbol)
        reference_price = limit_price or market_price
        position = self._position(account_id, symbol)
        cutoff = utcnow() - timedelta(minutes=1)
        recent = self.session.scalar(
            select(func.count(Order.id)).where(
                Order.account_id == account_id, Order.created_at >= cutoff
            )
        )
        ctx = RiskContext(
            cash=self.cash.cash(account_id),
            position_qty=position.quantity if position else 0.0,
            position_avg_price=position.avg_price if position else 0.0,
            orders_last_minute=int(recent or 0),
        )
        if self.risk is not None:
            # Account-level kill switch runs before per-order limits.
            state = self.risk.state(account_id)
            equity = self._equity(account_id, ctx.cash)
            ctx = RiskContext(
                cash=ctx.cash,
                position_qty=ctx.position_qty,
                position_avg_price=ctx.position_avg_price,
                orders_last_minute=ctx.orders_last_minute,
                equity=equity,
                day_start_equity=state.day_start_equity,
                peak_equity=state.peak_equity,
            )
            if state.breaker_active:
                raise ConflictError(f"trading halted: {state.breaker_reason}")
            breaker = self.policy.breaker(ctx)
            if not breaker.allowed:
                self.risk.trip(account_id, breaker.reason)
                raise ConflictError(f"trading halted: {breaker.reason}")
        self.policy.validate(
            OrderIntent(
                side=side,
                quantity=quantity,
                price=reference_price,
                fee_bps=self.fee_bps,
            ),
            ctx,
            symbol_view,
        )

        account_kind = self.cash.kind(account_id)
        if account_kind == "live":
            return self._place_live_order(
                account_id=account_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit_price,
                source=source,
                client_order_id=client_order_id,
                market_price=market_price,
            )

        order = Order(
            id=new_id(),
            account_id=account_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            status="new",
            source=source,
            client_order_id=client_order_id,
        )
        self.session.add(order)
        self.session.flush()

        if order_type == "market":
            fill_price = self._market_fill_price(side, market_price)
            self._execute(order, quantity, fill_price)
        elif self._is_marketable(order, market_price):
            self._execute(order, quantity, self._limit_fill_price(order, market_price))
        else:
            order.status = "open"

        record_audit(
            self.session,
            action="order.placed",
            entity="order",
            entity_id=order.id,
            detail=json.dumps(
                {
                    "account_id": account_id,
                    "symbol": symbol,
                    "side": side,
                    "type": order_type,
                    "quantity": quantity,
                    "status": order.status,
                }
            ),
        )
        self.session.commit()
        self.session.refresh(order)
        return self._order_view(order)

    def _place_live_order(
        self,
        *,
        account_id: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        limit_price: float | None,
        source: str,
        client_order_id: str | None,
        market_price: float,
    ) -> OrderView:
        """Route to the exchange, then mirror the result locally.

        The exchange is the source of truth for the fill. Local position and cash
        ledgers are updated from the *actual* fill so equity math stays consistent;
        a live account's cash must be reconciled against the exchange balance
        (see /api/brokers/binance/verify) before relying on it for sizing.
        """
        if self.broker is None:
            raise ForbiddenError(
                "live account requires broker credentials; set LQ_BINANCE_API_KEY/SECRET "
                "and enable live trading"
            )
        result = self.broker.place_order(
            BrokerOrderRequest(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit_price,
                client_order_id=client_order_id,
            )
        )
        order = Order(
            id=new_id(),
            account_id=account_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            status=result.status,
            source=f"live:{source}",
            client_order_id=client_order_id,
            filled_quantity=result.filled_quantity,
            avg_fill_price=result.avg_price,
        )
        self.session.add(order)
        self.session.flush()

        fill_price = result.avg_price or market_price
        if result.filled_quantity > 0 and fill_price > 0:
            notional = round_money(result.filled_quantity * fill_price)
            fee = round_money(notional * self.fee_bps / 10_000.0)
            order.fee = fee
            position = self._position(account_id, symbol)
            if position is None:
                position = Position(
                    id=new_id(),
                    account_id=account_id,
                    symbol=symbol,
                    quantity=0.0,
                    avg_price=0.0,
                    realized_pnl=0.0,
                )
                self.session.add(position)
                self.session.flush()
            if side == "buy":
                new_qty = round_qty(position.quantity + result.filled_quantity)
                weighted = position.quantity * position.avg_price + notional + fee
                position.avg_price = round_price(weighted / new_qty) if new_qty else 0.0
                position.quantity = new_qty
                self.cash.post_cash(
                    account_id,
                    amount=-round_money(notional + fee),
                    kind="trade",
                    ref=order.id,
                    note=f"live buy {result.filled_quantity} {symbol} @ {fill_price}",
                )
            else:
                realized = round_money((fill_price - position.avg_price) * result.filled_quantity - fee)
                position.realized_pnl = round_money(position.realized_pnl + realized)
                position.quantity = round_qty(position.quantity - result.filled_quantity)
                if position.quantity <= 1e-9:
                    position.quantity = 0.0
                    position.avg_price = 0.0
                self.cash.post_cash(
                    account_id,
                    amount=round_money(notional - fee),
                    kind="trade",
                    ref=order.id,
                    note=f"live sell {result.filled_quantity} {symbol} @ {fill_price}",
                )
            self.session.add(
                Fill(
                    id=new_id(),
                    order_id=order.id,
                    account_id=account_id,
                    symbol=symbol,
                    side=side,
                    quantity=result.filled_quantity,
                    price=fill_price,
                    fee=fee,
                )
            )
            self.session.flush()

        record_audit(
            self.session,
            action="order.placed_live",
            entity="order",
            entity_id=order.id,
            detail=json.dumps(
                {
                    "broker_order_id": result.broker_order_id,
                    "status": result.raw_status,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                }
            ),
        )
        self.session.commit()
        self.session.refresh(order)
        return self._order_view(order)

    def _equity(self, account_id: str, cash: float) -> float:
        positions_value = sum(p.market_value for p in self.positions(account_id))
        return round_money(cash + positions_value)

    def _market_fill_price(self, side: str, price: float) -> float:
        adjustment = self.slippage_bps / 10_000.0
        return round_price(price * (1.0 + adjustment if side == "buy" else 1.0 - adjustment))

    def _is_marketable(self, order: Order, price: float) -> bool:
        assert order.limit_price is not None
        if order.side == "buy":
            return price <= order.limit_price
        return price >= order.limit_price

    def _limit_fill_price(self, order: Order, price: float) -> float:
        assert order.limit_price is not None
        # Never fill a buy above its limit nor a sell below it.
        return round_price(min(price, order.limit_price) if order.side == "buy" else max(price, order.limit_price))

    # -------------------------------------------------------------- execute
    def _execute(self, order: Order, quantity: float, price: float) -> None:
        notional = round_money(quantity * price)
        fee = round_money(notional * self.fee_bps / 10_000.0)
        position = self._position(order.account_id, order.symbol)
        if position is None:
            position = Position(
                id=new_id(),
                account_id=order.account_id,
                symbol=order.symbol,
                quantity=0.0,
                avg_price=0.0,
                realized_pnl=0.0,
            )
            self.session.add(position)
            self.session.flush()

        if order.side == "buy":
            total_cost = round_money(notional + fee)
            available = self.cash.cash(order.account_id)
            if total_cost > available + 1e-9:
                raise ConflictError("insufficient cash at execution time")
            new_qty = round_qty(position.quantity + quantity)
            weighted = position.quantity * position.avg_price + notional + fee
            position.avg_price = round_price(weighted / new_qty)
            position.quantity = new_qty
            self.cash.post_cash(
                order.account_id,
                amount=-total_cost,
                kind="trade",
                ref=order.id,
                note=f"buy {quantity} {order.symbol} @ {price}",
            )
        else:
            if quantity > position.quantity + 1e-9:
                raise ConflictError("insufficient position at execution time")
            proceeds = round_money(notional - fee)
            realized = round_money((price - position.avg_price) * quantity - fee)
            position.realized_pnl = round_money(position.realized_pnl + realized)
            position.quantity = round_qty(position.quantity - quantity)
            if position.quantity <= 1e-9:
                position.quantity = 0.0
                position.avg_price = 0.0
            self.cash.post_cash(
                order.account_id,
                amount=proceeds,
                kind="trade",
                ref=order.id,
                note=f"sell {quantity} {order.symbol} @ {price}",
            )

        fill = Fill(
            id=new_id(),
            order_id=order.id,
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=fee,
        )
        self.session.add(fill)
        order.filled_quantity = round_qty(order.filled_quantity + quantity)
        prior_notional = order.avg_fill_price * (order.filled_quantity - quantity)
        order.avg_fill_price = round_price(
            (prior_notional + notional) / order.filled_quantity
        )
        order.fee = round_money(order.fee + fee)
        order.status = "filled"
        self.session.flush()

        if self.bus is not None:
            self.bus.publish(
                OrderFilled(
                    order_id=order.id,
                    account_id=order.account_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=quantity,
                    price=price,
                    fee=fee,
                )
            )

    # --------------------------------------------------------------- cancel
    def cancel_order(self, order_id: str, *, account_id: str | None = None) -> OrderView:
        order = self.session.get(Order, order_id)
        if order is None or (account_id and order.account_id != account_id):
            raise NotFoundError(f"order {order_id} not found")
        if order.status not in OPEN_STATUSES:
            raise ConflictError(f"order is {order.status} and cannot be cancelled")
        order.status = "cancelled"
        record_audit(
            self.session,
            action="order.cancelled",
            entity="order",
            entity_id=order.id,
            detail=json.dumps({"account_id": order.account_id}),
        )
        self.session.commit()
        self.session.refresh(order)
        return self._order_view(order)

    def close_position(self, account_id: str, symbol: str) -> OrderView | None:
        position = self._position(account_id, symbol)
        if position is None or position.quantity <= 0:
            raise ConflictError(f"no open position for {symbol.upper()}")
        return self.place_order(
            account_id=account_id,
            symbol=symbol,
            side="sell",
            order_type="market",
            quantity=position.quantity,
            source="manual-close",
        )

    # --------------------------------------------------------------- match
    def match_open_orders(self, *, account_id: str | None = None) -> int:
        """Fill resting limit orders whose price has been crossed."""
        orders = self.open_orders(account_id=account_id)
        filled = 0
        for order in orders:
            try:
                price = self.prices.price(order.symbol)
            except Exception:
                continue
            if not self._is_marketable(order, price):
                continue
            remaining = round_qty(order.quantity - order.filled_quantity)
            if remaining <= 0:
                order.status = "filled"
                continue
            try:
                self._execute(order, remaining, self._limit_fill_price(order, price))
                record_audit(
                    self.session,
                    action="order.filled",
                    entity="order",
                    entity_id=order.id,
                    detail=json.dumps({"account_id": order.account_id, "price": price}),
                )
                self.session.commit()
                filled += 1
            except ConflictError:
                self.session.rollback()
                order = self.session.get(Order, order.id)
                if order is not None:
                    order.status = "rejected"
                    order.reject_reason = "execution failed: funds or position changed"
                    self.session.commit()
        return filled
