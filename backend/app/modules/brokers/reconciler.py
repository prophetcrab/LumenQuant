"""Live fill reconciler.

Brokers accept orders asynchronously (an A-share gateway always does; Binance
resting limit orders do too), so a local order row starts as `open` and must be
advanced from broker state. This loop is the single writer of fills for live
orders: it polls non-final broker orders, mirrors new fills into
position/cash/ledger exactly once, and records rejects.

Idempotency is by `(order_id, filled_quantity)`: only the *delta* beyond what
has already been booked locally is applied.

Both markets are handled. The A-share gateway and Binance differ only in how the
status is fetched and how the fee is computed, so the booking logic is shared —
a crypto limit order used to stay `open` for ever, reserving its cash, because
only the A-share gateway was polled.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.alerts.service import AlertService
from app.modules.brokers.base import BrokerOrderResult
from app.modules.brokers import settlement
from app.modules.brokers.booking import book_broker_fill, fee_for
from app.modules.brokers.binance import BinanceBroker, build_broker
from app.modules.brokers.ths import ThsGatewayPort, build_ths_gateway
from app.modules.cn.fees import CnFeeSchedule
from app.modules.trading.models import Order

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("new", "open", "partially_filled")

# How many consecutive poll failures before the channel is reported. A single
# timeout is noise; a run of them means the operator's live orders are no longer
# being mirrored and nobody would otherwise know.
FAILURES_BEFORE_ALERT = 3


class LiveReconciler:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        gateway: ThsGatewayPort | None = None,
        broker: BinanceBroker | None = None,
    ) -> None:
        self.db = database
        self.settings = settings
        self._gateway = gateway
        self._broker = broker
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # Per channel: consecutive failures, so one bad poll does not spam the
        # alert centre while a persistent outage is not missed either.
        self._failures: dict[str, int] = {}
        # Session marker for the A-share T+1 duty, so it runs once per trading day
        # rather than on every tick.
        self._settled_session: str | None = None
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

    @property
    def broker(self) -> BinanceBroker | None:
        if self._broker is None:
            self._broker = build_broker(self.settings)
        return self._broker

    async def start(self) -> None:
        if not getattr(self.settings, "live_trading_enabled", False):
            return
        # Start whenever *either* channel exists. Requiring the A-share gateway
        # meant a Binance-only deployment never reconciled anything, so every
        # resting crypto order kept its cash reserved until the process restarted.
        if self.gateway is None and self.broker is None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="live-reconciler")
        logger.info(
            "live reconciler started (interval=%ss, channels=%s)",
            getattr(self.settings, "live_reconcile_seconds", 3.0),
            ",".join(
                name
                for name, channel in (("ths", self.gateway), ("binance", self.broker))
                if channel is not None
            ),
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
        for channel in (self._broker, self._gateway):
            closer = getattr(channel, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:
                    logger.exception("could not close a broker channel")

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
        polled = filled = rejected = 0
        with self.db.session() as session:
            # T+1 release is a money-safety duty like fill mirroring, so it lives
            # here as well as in the trading engine. With the runtime disabled
            # (manual live orders only) the trading engine is never constructed,
            # and A-share buys stayed unsellable for ever.
            self._settle_session_duties(session)
            orders = session.scalars(
                select(Order)
                .where(
                    Order.status.in_(OPEN_STATUSES),
                    Order.broker_order_id != "",
                    Order.source.like("live:%"),
                )
                .order_by(Order.created_at.asc())
            ).all()
            channel_failures: dict[str, int] = {}
            last_errors: dict[str, str] = {}
            for order in orders:
                channel, channel_client = self._channel_for(session, order)
                if channel_client is None:
                    # The channel that accepted this order is no longer
                    # configured (an operator unset the gateway URL, or a mock
                    # was switched off). Skipping silently left the order and
                    # its reserved cash in limbo with nothing to look at.
                    self._warn_orphan(channel, order)
                    continue
                polled += 1
                try:
                    result = channel_client.order_status(
                        order.broker_order_id, symbol=order.symbol
                    )
                except Exception as exc:
                    logger.warning(
                        "status poll failed for %s/%s: %s",
                        channel,
                        order.broker_order_id,
                        exc,
                    )
                    channel_failures[channel] = channel_failures.get(channel, 0) + 1
                    last_errors[channel] = str(exc)
                    continue
                # Per-order isolation: one unbookable order (an archived
                # account, a corrupt row) must not abort the sweep and leave
                # every later order unmirrored. The failure is surfaced rather
                # than swallowed.
                try:
                    applied = self._apply(session, order, result)
                except Exception as exc:
                    logger.exception(
                        "could not book fill for %s/%s", channel, order.broker_order_id
                    )
                    session.rollback()
                    order = session.get(Order, order.id)
                    if order is None:
                        continue
                    order.reject_reason = f"本地入账失败，等待重试：{exc}"[:240]
                    channel_failures[channel] = channel_failures.get(channel, 0) + 1
                    last_errors[channel] = str(exc)
                    session.commit()
                    continue
                if applied:
                    filled += 1
                # A fill that could not be priced leaves the order open for the
                # next poll; writing a terminal status here would drop it.
                if result.filled_quantity > order.filled_quantity + 1e-9:
                    continue
                if result.status == "rejected":
                    order.status = "rejected"
                    order.reject_reason = (result.raw_status or "broker rejected")[:240]
                    rejected += 1
                elif result.status == "cancelled":
                    order.status = "cancelled"
                elif (
                    result.status == "filled"
                    and order.filled_quantity < order.quantity - 1e-9
                ):
                    # `filled` with less than the ordered quantity would release
                    # the reserve while silently dropping the remainder.
                    order.reject_reason = (
                        f"券商报告已全部成交，但累计成交量 {order.filled_quantity:g} "
                        f"少于委托量 {order.quantity:g}，等待核对"
                    )[:240]
            session.commit()
        self._report_channel_health(channel_failures, last_errors)
        return {"polled": polled, "filled": filled, "rejected": rejected}

    def _warn_orphan(self, channel: str, order: Order) -> None:
        """Report an open live order whose broker channel disappeared."""
        key = f"orphan:{channel}"
        self._failures[key] = self._failures.get(key, 0) + 1
        if self._failures[key] != FAILURES_BEFORE_ALERT:
            return
        try:
            with self.db.session() as session:
                AlertService(session).raise_alert(
                    category="gateway_failure",
                    level="critical",
                    account_id=order.account_id,
                    title=f"{order.symbol} 委托的对账通道已不可用",
                    message=(
                        f"委托 {order.broker_order_id} 属于 {channel} 通道，但该通道已不再配置。"
                        "这笔委托的成交不会再被回写，预留资金也不会释放。"
                        "请恢复通道配置，或撤单后再处理。"
                    ),
                    ref_type="broker_channel",
                    ref_id=channel,
                    commit=True,
                )
        except Exception:
            logger.exception("could not raise orphan-order alert")

    def _channel_for(self, session, order: Order):
        """Which client should poll this order, and its name.

        The channel comes from the account's market, not from the order source:
        both channels write `live:` prefixed sources, so the source cannot tell
        them apart.
        """
        market = AccountService(
            session, live_trading_enabled=self.settings.live_trading_enabled
        ).require(order.account_id).market
        if market == "cn_stock":
            return "ths", self.gateway
        return "binance", self.broker

    def _report_channel_health(
        self, failures: dict[str, int], errors: dict[str, str]
    ) -> None:
        """Alert once when a channel has been failing for several ticks.

        Without this a broker outage looks like quiet bookkeeping: every order
        stays `open`, cash stays reserved, and no page shows anything wrong.
        """
        for channel, count in failures.items():
            self._failures[channel] = self._failures.get(channel, 0) + 1
        for channel in list(self._failures):
            # Only reset real channel counters. Orphan counters (`orphan:<ch>`)
            # are driven by `_warn_orphan` and never appear in `failures`, so
            # resetting them here would keep the streak pinned at 1 and the
            # alert would never fire.
            if channel.startswith("orphan:"):
                continue
            if channel not in failures:
                self._failures[channel] = 0
        for channel, streak in self._failures.items():
            if channel.startswith("orphan:"):
                continue  # reported by `_warn_orphan`, which owns that counter
            if streak < FAILURES_BEFORE_ALERT:
                continue
            if streak != FAILURES_BEFORE_ALERT and streak % 20 != 0:
                continue  # one alert per outage, then a reminder every 20 ticks
            try:
                with self.db.session() as session:
                    AlertService(session).raise_alert(
                        category="gateway_failure",
                        level="critical",
                        account_id="",
                        title=f"{channel} 成交回报中断",
                        message=(
                            f"连续 {streak} 次轮询失败：{errors.get(channel, '')[:200]}。"
                            "实盘委托的成交不会再回写到本地账本，持仓与可用资金会停在旧状态。"
                        ),
                        ref_type="broker_channel",
                        ref_id=channel,
                        commit=True,
                    )
            except Exception:
                logger.exception("could not raise reconciler health alert")

    def _settle_session_duties(self, session) -> None:
        """Release A-share T+1 state once per trading day, at the session open.

        This is the reconciler's copy of the trading engine's duty, and it is not
        redundant: with `LQ_RUNTIME_ENABLED=false` (manual live orders only) the
        trading engine is never constructed, and an A-share buy would stay
        unsellable for ever because nothing else clears `available_quantity`.
        """
        cn_engine = getattr(self.settings, "cn_market_engine", None)
        cn_clock = getattr(cn_engine, "clock", None)
        now = (
            int(cn_clock.now_ms())
            if cn_clock is not None
            else int(time.time() * 1000)
        )
        if not settlement.session_is_open(now):
            return
        key = settlement.session_key(now)
        if self._settled_session == key:
            return
        try:
            positions, holds = settlement.release_t1(session, now_ms=now)
            settlement.refresh_prev_close(session, cn_engine)
        except Exception:
            logger.exception("A-share T+1 settlement failed")
            return
        self._settled_session = key
        if positions or holds:
            logger.info(
                "T+1 settlement for %s: %s position(s), %s hold(s)",
                key,
                positions,
                holds,
            )

    def _fee(self, market: str, side: str, notional: float) -> float:
        return fee_for(
            market, side, notional,
            fees=self.fees, fee_bps=getattr(self.settings, "fee_bps", 10.0),
        )

    def _apply(self, session, order: Order, result: BrokerOrderResult) -> bool:
        return book_broker_fill(
            session,
            order,
            result,
            fees=self.fees,
            fee_bps=getattr(self.settings, "fee_bps", 10.0),
            live_trading_enabled=getattr(self.settings, "live_trading_enabled", True),
        )

    # ------------------------------------------------------------- settlement
    def unlock_t1(self) -> int:
        """Release T+1 state immediately, ignoring the session guard.

        Kept as the manual/testing entry point; the scheduled path is
        `_settle_session_duties`, which runs once per trading day. Scoped to
        A-share accounts so a crypto bookkeeping mismatch is not silently
        "fixed" by making an unsellable quantity sellable.
        """
        with self.db.session() as session:
            positions, holds = settlement.release_t1(session)
        if positions or holds:
            logger.info(
                "T+1 release (manual): %s position(s), %s hold(s)", positions, holds
            )
        return positions + holds
