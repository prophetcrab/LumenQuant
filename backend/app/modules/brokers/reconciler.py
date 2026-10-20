"""Live fill reconciler.

A-share gateways accept orders asynchronously, so the local order row starts as
`open` and must be advanced from gateway state. This loop is the single writer
of fills for live orders: it polls non-final gateway orders, mirrors new fills
into position/cash/ledger exactly once, and records rejects.

Idempotency is by `(order_id, filled_quantity)`: only the *delta* beyond what
has already been booked locally is applied.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.core.money import round_money, round_qty
from app.core.timeutil import utcnow
from app.modules.accounts.service import AccountService
from app.modules.brokers.base import BrokerOrderResult
from app.modules.brokers.ths import ThsGatewayPort, build_ths_gateway
from app.modules.cn.fees import CnFeeSchedule
from app.modules.trading.models import Fill, Order, Position, new_id

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("new", "open", "partially_filled")


class LiveReconciler:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        gateway: ThsGatewayPort | None = None,
    ) -> None:
        self.db = database
        self.settings = settings
        self._gateway = gateway
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.fees = CnFeeSchedule(
            commission_bps=getattr(settings, "cn_commission_bps", 2.5),
            min_commission=getattr(settings, "cn_min_commission", 5.0),
            stamp_duty_bps=getattr(settings, "cn_stamp_duty_bps", 5.0),
            transfer_fee_bps=getattr(settings, "cn_transfer_fee_bps", 0.1),
        )

    @property
    def gateway(self) -> ThsGatewayPort | None:
        if self._gateway is None:
            self._gateway = build_ths_gateway(self.settings)
        return self._gateway

    async def start(self) -> None:
        if self.gateway is None or not getattr(self.settings, "live_trading_enabled", False):
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="live-reconciler")
        logger.info(
            "live reconciler started (interval=%ss)",
            getattr(self.settings, "live_reconcile_seconds", 3.0),
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                logger.exception("live reconcile tick failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=max(1.0, getattr(self.settings, "live_reconcile_seconds", 3.0)),
                )
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ tick
    def tick(self) -> dict[str, int]:
        gateway = self.gateway
        if gateway is None:
            return {"polled": 0, "filled": 0, "rejected": 0}
        polled = filled = rejected = 0
        with self.db.session() as session:
            orders = session.scalars(
                select(Order)
                .where(
                    Order.status.in_(OPEN_STATUSES),
                    Order.broker_order_id != "",
                    Order.source.like("live:%"),
                )
                .order_by(Order.created_at.asc())
            ).all()
            for order in orders:
                polled += 1
                try:
                    result = gateway.order_status(order.broker_order_id)
                except Exception as exc:
                    logger.warning("status poll failed for %s: %s", order.broker_order_id, exc)
                    continue
                if self._apply(session, order, result):
                    filled += 1
                if result.status == "rejected":
                    order.status = "rejected"
                    order.reject_reason = (result.raw_status or "gateway rejected")[:240]
                    rejected += 1
                elif result.status == "cancelled":
                    order.status = "cancelled"
            session.commit()
        return {"polled": polled, "filled": filled, "rejected": rejected}

    def _apply(self, session, order: Order, result: BrokerOrderResult) -> bool:
        already = round_qty(order.filled_quantity)
        cumulative = round_qty(result.filled_quantity)
        delta = round_qty(cumulative - already)
        if delta <= 1e-9:
            if result.status == "filled" and order.status != "filled":
                order.status = "filled"
            return False

        price = result.avg_price or 0.0
        if price <= 0:
            return False
        notional = round_money(delta * price)
        fee = self.fees.total(order.side, notional)
        position = session.scalar(
            select(Position).where(
                Position.account_id == order.account_id, Position.symbol == order.symbol
            )
        )
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
            session.add(position)
            session.flush()

        accounts = AccountService(
            session, live_trading_enabled=self.settings.live_trading_enabled
        )
        if order.side == "buy":
            new_qty = round_qty(position.quantity + delta)
            weighted = position.quantity * position.avg_price + notional + fee
            position.avg_price = round(weighted / new_qty, 8) if new_qty else 0.0
            position.quantity = new_qty
            # A-share T+1: today's buys are not sellable until the next session.
            accounts.post_cash(
                order.account_id,
                amount=-round_money(notional + fee),
                kind="trade",
                ref=order.id,
                note=f"live buy {delta} {order.symbol} @ {price}",
            )
        else:
            realized = round_money((price - position.avg_price) * delta - fee)
            position.realized_pnl = round_money(position.realized_pnl + realized)
            position.quantity = round_qty(position.quantity - delta)
            position.available_quantity = max(
                0.0, round_qty(position.available_quantity - delta)
            )
            if position.quantity <= 1e-9:
                position.quantity = 0.0
                position.available_quantity = 0.0
                position.avg_price = 0.0
            accounts.post_cash(
                order.account_id,
                amount=round_money(notional - fee),
                kind="trade",
                ref=order.id,
                note=f"live sell {delta} {order.symbol} @ {price}",
            )

        session.add(
            Fill(
                id=new_id(),
                order_id=order.id,
                account_id=order.account_id,
                symbol=order.symbol,
                side=order.side,
                quantity=delta,
                price=price,
                fee=fee,
            )
        )
        order.filled_quantity = cumulative
        order.avg_fill_price = round(price, 8)
        order.fee = round_money(order.fee + fee)
        order.status = result.status if result.status in {"filled", "partially_filled"} else order.status
        if cumulative >= order.quantity - 1e-9:
            order.status = "filled"
        session.flush()
        return True

    # ------------------------------------------------------------- settlement
    def unlock_t1(self) -> int:
        """Release T+1 holds. Called once per session open by the scheduler."""
        unlocked = 0
        with self.db.session() as session:
            rows = session.scalars(
                select(Position).where(Position.quantity > Position.available_quantity)
            ).all()
            for position in rows:
                position.available_quantity = position.quantity
                unlocked += 1
            session.commit()
        if unlocked:
            logger.info("T+1 settlement released %s position(s)", unlocked)
        return unlocked
