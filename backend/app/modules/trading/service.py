"""Order lifecycle, matching and positions.

All writes for one order action happen in a single transaction: order row, fill,
position, cash ledger and audit. The service never imports another module's ORM
models; it talks to `CashPort` and `PricePort`.

Market rules are resolved per account:
* crypto — 24/7, fractional lots, flat bps fee, instantly sellable;
* cn_stock — 100-share lots, daily price band, T+1 availability, asymmetric fees,
  and (optionally) a session gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.events import EventBus, OrderFilled
from app.core.money import round_money, round_price, round_qty
from app.core.timeutil import iso, utcnow
from app.modules.brokers.base import BrokerOrderRequest, BrokerPort
from app.modules.cn.calendar import is_session_open
from app.modules.cn.fees import CnFeeSchedule
from app.modules.cn.rules import (
    PriceBand,
    price_band,
    validate_limit_price,
    validate_quantity,
)
from app.modules.risk.policy import OrderIntent, RiskContext, RiskPolicy
from app.modules.risk.service import RiskService
from app.modules.trading.models import CashHold, Fill, Order, Position, new_id
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
    broker_order_id: str
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
    available_quantity: float
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
        cn_broker: BrokerPort | None = None,
        cn_fees: CnFeeSchedule | None = None,
        cn_session_enforced: bool = False,
        cn_live_session_gate: bool = True,
        live_market=None,
        alerts=None,
    ) -> None:
        self.session = session
        self.cash = cash
        self.prices = prices
        self.policy = policy or RiskPolicy()
        self.bus = bus
        self.alerts = alerts
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.risk = risk
        self.broker = broker
        self.cn_broker = cn_broker
        self.cn_fees = cn_fees or CnFeeSchedule()
        self.cn_session_enforced = cn_session_enforced
        self.cn_live_session_gate = cn_live_session_gate
        self.live_market = live_market

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
            broker_order_id=order.broker_order_id,
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
        # Pending sells lock shares; report what the user can actually commit.
        sellable = round_qty(
            max(0.0, position.available_quantity - self.reserved_quantity(position.account_id, position.symbol))
        )
        return PositionView(
            account_id=position.account_id,
            symbol=position.symbol,
            quantity=position.quantity,
            available_quantity=sellable,
            avg_price=position.avg_price,
            realized_pnl=round_money(position.realized_pnl),
            market_price=price,
            market_value=market_value,
            unrealized_pnl=unrealized,
            updated_at=iso(position.updated_at),
        )

    def reserved_cash(self, account_id: str, *, exclude_order_id: str | None = None) -> float:
        """Cash committed by resting buy orders.

        A resting limit buy must reserve its notional (plus fee), otherwise the
        same balance can back unlimited pending orders and the account goes
        negative when several fill at once. Sells reserve shares instead, which is
        handled by `available_quantity`.
        """
        orders = self.session.scalars(
            select(Order).where(
                Order.account_id == account_id,
                Order.side == "buy",
                Order.status.in_(OPEN_STATUSES),
            )
        ).all()
        reserved = 0.0
        for order in orders:
            if exclude_order_id and order.id == exclude_order_id:
                continue
            remaining = round_qty(order.quantity - order.filled_quantity)
            if remaining <= 0:
                continue
            price = order.limit_price or self._safe_price(order.symbol)
            if price <= 0:
                continue
            notional = round_money(remaining * price)
            reserved += notional + self._fee(self._account_market(account_id), "buy", notional)
        return round_money(reserved)

    def available_cash(self, account_id: str) -> float:
        """Book cash minus pending-order reservations and unsettled proceeds."""
        return round_money(
            self.cash.cash(account_id)
            - self.reserved_cash(account_id)
            - self.held_cash(account_id)
        )

    def held_cash(self, account_id: str) -> float:
        """Proceeds booked but not yet spendable (A-share T+1 settlement)."""
        market = self._account_market(account_id)
        if market != "cn_stock":
            return 0.0
        from app.modules.risk.daykey import risk_day_key

        today = risk_day_key(market)
        total = self.session.scalar(
            select(func.coalesce(func.sum(CashHold.amount), 0.0)).where(
                CashHold.account_id == account_id,
                CashHold.release_day >= today,
            )
        )
        return round_money(float(total or 0.0))

    def release_held_cash(self, account_id: str | None = None) -> int:
        """Delete holds whose release day has arrived.

        Called by the daily settlement sweep alongside `settle_t1`. Holds are
        removed rather than flipped to a flag: the ledger already carries the
        cash, so a released hold simply stops subtracting from availability.
        """
        from datetime import date

        from app.modules.risk.daykey import risk_day_key

        today = date.fromisoformat(risk_day_key("cn_stock"))
        stmt = delete(CashHold).where(CashHold.release_day <= today.isoformat())
        if account_id:
            stmt = stmt.where(CashHold.account_id == account_id)
        result = self.session.execute(stmt)
        self.session.commit()
        return int(result.rowcount or 0)

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

    def reserved_quantity(self, account_id: str, symbol: str) -> float:
        """Shares locked by pending sell orders."""
        orders = self.session.scalars(
            select(Order).where(
                Order.account_id == account_id,
                Order.symbol == symbol.upper(),
                Order.side == "sell",
                Order.status.in_(OPEN_STATUSES),
            )
        ).all()
        return round_qty(
            sum(max(0.0, order.quantity - order.filled_quantity) for order in orders)
        )

    def fills(self, account_id: str, *, limit: int = 100) -> list[FillView]:
        rows = self.session.scalars(
            select(Fill)
            .where(Fill.account_id == account_id)
            .order_by(Fill.created_at.desc())
            .limit(max(1, min(limit, 1000)))
        ).all()
        return [self._fill_view(f) for f in rows]

    # --------------------------------------------------------------- market
    def _account_market(self, account_id: str) -> str:
        return getattr(self.cash, "market", lambda _id: "crypto")(account_id)

    def _fee(self, market: str, side: str, notional: float) -> float:
        if market == "cn_stock":
            return self.cn_fees.total(side, notional)
        return round_money(notional * self.fee_bps / 10_000.0)

    def _band(self, symbol_view, market: str) -> PriceBand | None:
        if market != "cn_stock":
            return None
        return price_band(
            symbol_view.prev_close,
            symbol_view.board,
            symbol_view.is_st,
            symbol_view.tick_size or 0.01,
        )

    def _assert_session(self, market: str, *, live: bool) -> None:
        if market != "cn_stock":
            return
        if not (live and self.cn_live_session_gate) and not self.cn_session_enforced:
            return
        if not is_session_open():
            raise ConflictError("A股非交易时段，无法下单（09:30-11:30 / 13:00-15:00）")

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

        market = self._account_market(account_id)
        account_kind = self.cash.kind(account_id)
        symbol_view = self.prices.symbol(symbol)
        if symbol_view.market != market:
            raise ValidationError(
                f"{symbol} 属于 {symbol_view.market} 市场，与账户市场 {market} 不一致"
            )
        if symbol_view.suspended:
            raise ConflictError(f"{symbol} 停牌中，无法交易")

        market_price = self.prices.price(symbol)
        reference_price = limit_price or market_price
        position = self._position(account_id, symbol)

        if market == "cn_stock":
            self._assert_session(market, live=account_kind == "live")
            sellable = (
                round_qty(
                    position.available_quantity
                    - self.reserved_quantity(account_id, symbol)
                )
                if position
                else 0.0
            )
            validate_quantity(
                side,
                quantity,
                position_quantity=position.quantity if position else 0.0,
                available_quantity=sellable,
            )
            band = self._band(symbol_view, market)
            if limit_price is not None:
                validate_limit_price(limit_price, band, symbol_view.tick_size or 0.01)
            if band is not None:
                locked = getattr(self.live_market, "is_locked", lambda _s: "")(symbol)
                if side == "buy" and (
                    locked == "up" or at_or_above(market_price, band.up, symbol_view.tick_size)
                ):
                    raise ConflictError(f"{symbol} 涨停，无法买入")
                if side == "sell" and (
                    locked == "down" or at_or_below(market_price, band.down, symbol_view.tick_size)
                ):
                    raise ConflictError(f"{symbol} 跌停，无法卖出")

        cutoff = utcnow() - timedelta(minutes=1)
        recent = self.session.scalar(
            select(func.count(Order.id)).where(
                Order.account_id == account_id, Order.created_at >= cutoff
            )
        )
        position_commit = (
            round_qty(
                (position.available_quantity if market == "cn_stock" else position.quantity)
                - self.reserved_quantity(account_id, symbol)
            )
            if position
            else 0.0
        )
        ctx = RiskContext(
            cash=self.available_cash(account_id),
            position_qty=max(0.0, position_commit),
            position_avg_price=position.avg_price if position else 0.0,
            orders_last_minute=int(recent or 0),
        )
        if self.risk is not None:
            # Account-level kill switch runs before per-order limits.
            state = self.risk.state(account_id)
            # ctx.cash is *available* cash (reservations deducted). Equity must use
            # book cash, otherwise pending orders look like realised losses and
            # immediately trip the daily-loss breaker.
            equity = self._equity(account_id, self.cash.cash(account_id))
            if state.day_start_equity <= 0 or state.peak_equity <= 0:
                # First order of the session: seed the baseline from current
                # equity so the breaker is armed from the start.
                state = self.risk.ensure_baseline(account_id, equity)
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

        estimated_fee = self._fee(market, side, reference_price * quantity)
        self.policy.validate(
            OrderIntent(
                side=side,
                quantity=quantity,
                price=reference_price,
                fee_bps=(estimated_fee / (reference_price * quantity) * 10_000.0)
                if reference_price * quantity
                else 0.0,
            ),
            ctx,
            symbol_view,
        )

        if account_kind == "live":
            gateway = self.cn_broker if market == "cn_stock" else self.broker
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
                market=market,
                gateway=gateway,
                band=self._band(symbol_view, market),
                tick=symbol_view.tick_size or 0.01,
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
            fill_price = self._market_fill_price(
                side, market_price, band=self._band(symbol_view, market),
                tick=symbol_view.tick_size or 0.01,
            )
            self._execute(order, quantity, fill_price, market=market)
        elif self._is_marketable(order, market_price):
            self._execute(
                order,
                quantity,
                self._limit_fill_price(order, market_price),
                market=market,
            )
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
                    "market": market,
                }
            ),
        )
        self.session.commit()
        self.session.refresh(order)
        return self._order_view(order)

    def _record_gateway_failure(
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
        reason: str,
    ) -> None:
        """Persist a rejected order so the attempt is auditable.

        The outcome is genuinely ambiguous — the request may or may not have
        reached the broker — so the row says `rejected` with a reason that
        admits it, rather than implying nothing happened.
        """
        order = Order(
            id=new_id(),
            account_id=account_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            status="rejected",
            source=f"live:{source}",
            client_order_id=client_order_id,
            reject_reason=f"gateway error before ack: {reason}"[:240],
        )
        self.session.add(order)
        record_audit(
            self.session,
            action="order.gateway_failed",
            entity="order",
            entity_id=order.id,
            detail=json.dumps(
                {"account_id": account_id, "symbol": symbol, "reason": reason[:240]}
            ),
        )
        self.session.commit()

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
        market: str,
        gateway: BrokerPort | None,
        band: PriceBand | None,
        tick: float,
    ) -> OrderView:
        """Submit to the gateway and book whatever it reports as filled.

        A-share gateways are asynchronous: the usual result is `open`, and the
        LiveReconciler books fills later. Crypto (Binance) answers immediately
        with the fill, so the same code handles both.
        """
        if gateway is None:
            if market == "cn_stock":
                raise ForbiddenError(
                    "A股实盘需要同花顺下单网关：设置 LQ_THS_GATEWAY_URL（或启用 LQ_THS_MOCK_ENABLED 做演练）"
                )
            raise ForbiddenError(
                "live account requires broker credentials; set LQ_BINANCE_API_KEY/SECRET "
                "and enable live trading"
            )
        try:
            result = gateway.place_order(
                BrokerOrderRequest(
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=quantity,
                    limit_price=limit_price,
                    client_order_id=client_order_id,
                )
            )
        except Exception as exc:
            # The gateway is an external system: unreachable, unauthorised, or
            # returning garbage. An unhandled exception became a 500 with no
            # trace in the order table, so the user could not tell whether the
            # broker had received anything. Record the failure as a rejected
            # order and surface a message that says nothing was known to be sent.
            self._record_gateway_failure(
                account_id=account_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit_price,
                source=source,
                client_order_id=client_order_id,
                reason=str(exc),
            )
            if self.alerts is not None:
                self.alerts.raise_alert(
                    category="gateway_failure",
                    level="critical",
                    account_id=account_id,
                    title=f"{symbol} 下单网关调用失败",
                    message=f"{side} {quantity} {symbol}：{exc}",
                    ref_type="account",
                    ref_id=account_id,
                    commit=True,
                )
            raise ConflictError(
                f"下单网关调用失败，订单未确认提交：{exc}"
            ) from exc
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
            broker_order_id=result.broker_order_id,
            filled_quantity=0.0,
            avg_fill_price=0.0,
        )
        self.session.add(order)
        self.session.flush()

        fill_price = result.avg_price or market_price
        if result.filled_quantity > 0 and fill_price > 0:
            if band is not None:
                fill_price = min(max(fill_price, band.down), band.up)
            self._book_fill(
                order,
                result.filled_quantity,
                fill_price,
                market=market,
                status=result.status,
            )

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
                    "market": market,
                }
            ),
        )
        self.session.commit()
        self.session.refresh(order)
        return self._order_view(order)

    def _equity(self, account_id: str, cash: float) -> float:
        positions_value = sum(p.market_value for p in self.positions(account_id))
        return round_money(cash + positions_value)

    def _market_fill_price(
        self,
        side: str,
        price: float,
        *,
        band: PriceBand | None = None,
        tick: float = 0.01,
    ) -> float:
        if band is not None:
            # A-share market orders cannot transact outside the daily band.
            return round_price(min(max(price, band.down), band.up))
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
    def _execute(
        self, order: Order, quantity: float, price: float, *, market: str = "crypto"
    ) -> None:
        self._book_fill(order, quantity, price, market=market, status="filled")

    def _book_fill(
        self,
        order: Order,
        quantity: float,
        price: float,
        *,
        market: str,
        status: str = "filled",
    ) -> None:
        notional = round_money(quantity * price)
        fee = self._fee(market, order.side, notional)
        position = self._position(order.account_id, order.symbol)
        if position is None:
            position = Position(
                id=new_id(),
                account_id=order.account_id,
                symbol=order.symbol,
                quantity=0.0,
                available_quantity=0.0,
                avg_price=0.0,
                realized_pnl=0.0,
            )
            self.session.add(position)
            self.session.flush()

        if order.side == "buy":
            total_cost = round_money(notional + fee)
            # Exclude this order's own reservation: at fill time the reservation
            # is about to be consumed by this very trade.
            available = round_money(
                self.cash.cash(order.account_id)
                - self.reserved_cash(order.account_id, exclude_order_id=order.id)
            )
            if total_cost > available + 1e-9:
                raise ConflictError("insufficient cash at execution time")
            new_qty = round_qty(position.quantity + quantity)
            weighted = position.quantity * position.avg_price + notional + fee
            position.avg_price = round_price(weighted / new_qty)
            position.quantity = new_qty
            if market != "cn_stock":
                # Crypto settles instantly; A-shares unlock at next settlement.
                position.available_quantity = new_qty
            self.cash.post_cash(
                order.account_id,
                amount=-total_cost,
                kind="trade",
                ref=order.id,
                note=f"buy {quantity} {order.symbol} @ {price}",
            )
        else:
            sellable = (
                position.available_quantity if market == "cn_stock" else position.quantity
            )
            if quantity > sellable + 1e-9:
                raise ConflictError(
                    f"可卖数量不足：可卖 {sellable:g}"
                    + ("（T+1 当日买入次日可用）" if market == "cn_stock" else "")
                )
            proceeds = round_money(notional - fee)
            realized = round_money((price - position.avg_price) * quantity - fee)
            position.realized_pnl = round_money(position.realized_pnl + realized)
            position.quantity = round_qty(position.quantity - quantity)
            if market == "cn_stock":
                position.available_quantity = max(
                    0.0, round_qty(position.available_quantity - quantity)
                )
            else:
                position.available_quantity = position.quantity
            if position.quantity <= 1e-9:
                position.quantity = 0.0
                position.available_quantity = 0.0
                position.avg_price = 0.0
            self.cash.post_cash(
                order.account_id,
                amount=proceeds,
                kind="trade",
                ref=order.id,
                note=f"sell {quantity} {order.symbol} @ {price}",
            )
            if market == "cn_stock" and proceeds > 0:
                # A-share sale proceeds settle T+1: the ledger is credited now,
                # but the money cannot back a new purchase until the next trading
                # day. Holding it here keeps `available_cash` honest without
                # touching the ledger (which must show the trade when it happened).
                from datetime import date

                from app.modules.cn.calendar import next_trading_day
                from app.modules.risk.daykey import risk_day_key

                release = next_trading_day(date.fromisoformat(risk_day_key(market)))
                self.session.add(
                    CashHold(
                        account_id=order.account_id,
                        amount=proceeds,
                        release_day=release.isoformat(),
                        ref=order.id,
                    )
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
        order.status = status if status in {"filled", "partially_filled"} else order.status
        if order.filled_quantity >= order.quantity - 1e-9:
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
        if order.broker_order_id:
            gateway = (
                self.cn_broker
                if self._account_market(order.account_id) == "cn_stock"
                else self.broker
            )
            if gateway is None:
                raise ConflictError("gateway unavailable; cannot cancel live order")
            try:
                gateway.cancel_order(order.symbol, order.broker_order_id)
            except Exception as exc:
                # The local row must stay `open`: the broker may still have the
                # order live. Reporting success here would hide a position the
                # user believes is cancelled.
                raise ConflictError(
                    f"撤单请求失败，委托可能仍在券商侧有效：{exc}"
                ) from exc
        order.status = "cancelled"
        record_audit(
            self.session,
            action="order.cancelled",
            entity="order",
            entity_id=order.id,
            detail=json.dumps(
                {"account_id": order.account_id, "broker_order_id": order.broker_order_id}
            ),
        )
        self.session.commit()
        self.session.refresh(order)
        return self._order_view(order)

    def close_position(self, account_id: str, symbol: str) -> OrderView | None:
        position = self._position(account_id, symbol)
        if position is None or position.quantity <= 0:
            raise ConflictError(f"no open position for {symbol.upper()}")
        market = self._account_market(account_id)
        quantity = (
            position.available_quantity if market == "cn_stock" else position.quantity
        )
        if quantity <= 0:
            raise ConflictError("无可卖数量（T+1：当日买入次日可用）")
        return self.place_order(
            account_id=account_id,
            symbol=symbol,
            side="sell",
            order_type="market",
            quantity=quantity,
            source="manual-close",
        )

    # --------------------------------------------------------------- match
    def match_open_orders(self, *, account_id: str | None = None) -> int:
        """Fill resting limit orders whose price has been crossed."""
        orders = self.open_orders(account_id=account_id)
        filled = 0
        for order in orders:
            if order.broker_order_id:
                continue  # live orders are advanced by the reconciler
            try:
                symbol_view = self.prices.symbol(order.symbol)
                price = self.prices.price(order.symbol)
            except Exception:
                continue
            if symbol_view.suspended:
                continue
            market = symbol_view.market
            band = self._band(symbol_view, market)
            if band is not None:
                tick = symbol_view.tick_size or 0.01
                if order.side == "buy" and at_or_above(price, band.up, tick):
                    continue  # limit-up: nothing to buy
                if order.side == "sell" and at_or_below(price, band.down, tick):
                    continue
            if not self._is_marketable(order, price):
                continue
            remaining = round_qty(order.quantity - order.filled_quantity)
            if remaining <= 0:
                order.status = "filled"
                continue
            try:
                self._execute(
                    order,
                    remaining,
                    self._limit_fill_price(order, price),
                    market=market,
                )
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

    # ----------------------------------------------------------- settlement
    def settle_t1(self) -> int:
        """Release T+1 holds for A-share positions. Run once per session open.

        Scoped to A-share accounts: crypto settles instantly, so touching its
        positions here would mask a genuine quantity/availability mismatch instead
        of fixing it.
        """
        from app.modules.accounts.models import Account

        cn_account_ids = self.session.scalars(
            select(Account.id).where(Account.market == "cn_stock")
        ).all()
        if not cn_account_ids:
            return 0
        rows = self.session.scalars(
            select(Position).where(
                Position.quantity > Position.available_quantity,
                Position.account_id.in_(cn_account_ids),
            )
        ).all()
        for position in rows:
            position.available_quantity = position.quantity
        # Release every outstanding sale-proceeds hold for these accounts. This
        # method *is* the settlement event ("a new trading day has opened"), so
        # holds created yesterday are due now regardless of the wall-clock date —
        # comparing against `release_day <= today` would leave them stuck whenever
        # the settlement runs before the calendar has rolled.
        held = self.session.execute(
            delete(CashHold).where(CashHold.account_id.in_(cn_account_ids))
        )
        self.session.commit()
        return len(rows) + int(held.rowcount or 0)


def at_or_above(price: float, level: float, tick: float) -> bool:
    return level > 0 and price >= level - tick / 2


def at_or_below(price: float, level: float, tick: float) -> bool:
    return level > 0 and price <= level + tick / 2
