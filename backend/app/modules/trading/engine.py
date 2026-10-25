"""Background loop that matches resting limit orders against live prices.

Also performs once-per-session A-share duties: T+1 release and previous-close
refresh, so the 09:30 open starts with correct limits and sellable quantities.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.brokers import settlement
from app.modules.cn.calendar import day_key
from app.modules.market.service import MarketService
from app.modules.risk.policy import RiskLimits, RiskPolicy
from app.modules.risk.service import RiskService
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.service import TradingService

logger = logging.getLogger(__name__)


class TradingEngine:
    def __init__(self, database: Database, settings: Settings, *, market_engine=None, bus=None) -> None:
        self.db = database
        self.settings = settings
        self.market_engine = market_engine
        self.bus = bus
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_settlement_day: date | None = None

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="trading-engine")

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
                logger.exception("trading tick failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=max(0.5, self.settings.engine_interval_seconds),
                )
            except asyncio.TimeoutError:
                pass

    def tick(self) -> int:
        with self.db.session() as session:
            self._session_duties(session)
            accounts = AccountService(
                session, live_trading_enabled=self.settings.live_trading_enabled
            )
            market = MarketService(
                session,
                clock=getattr(self.market_engine, "clock", None),
                live=self.market_engine,
            )
            service = TradingService(
                session,
                cash=AccountCashAdapter(accounts),
                prices=MarketPriceAdapter(market),
                policy=RiskPolicy(
                    RiskLimits(
                        max_order_notional=self.settings.risk_max_order_notional,
                        max_position_notional=self.settings.risk_max_position_notional,
                        min_order_notional=self.settings.risk_min_order_notional,
                        max_orders_per_minute=self.settings.risk_max_orders_per_minute,
                        max_daily_loss_pct=self.settings.risk_max_daily_loss_pct,
                        max_drawdown_pct=self.settings.risk_max_drawdown_pct,
                    )
                ),
                bus=self.bus,
                fee_bps=self.settings.fee_bps,
                slippage_bps=self.settings.slippage_bps,
                risk=RiskService(session),
                live_market=self.market_engine,
            )
            return service.match_open_orders()

    def _session_duties(self, session) -> None:
        """Release T+1 and refresh limits once per trading day at the open.

        The simulated CN engine supplies the session clock when it exists. In a
        real-data-only deployment there is no such engine, and the old early
        return meant T+1 was *never* released: a position bought today stayed
        unsellable forever because nothing else clears `available_quantity`.
        Falling back to the wall clock keeps the duty running in both modes.

        The release itself lives in `brokers.settlement` because the live
        reconciler runs the same duty when this engine is not constructed at all
        (`LQ_RUNTIME_ENABLED=false`).
        """
        cn_engine = getattr(self.market_engine, "cn", None)
        clock = getattr(cn_engine, "clock", None)
        now = clock.now_ms() if clock is not None else int(time.time() * 1000)
        today = day_key(now)
        if self._last_settlement_day == today:
            return
        if not settlement.session_is_open(now):
            return
        positions, holds = settlement.release_t1(session, now_ms=now)
        settlement.refresh_prev_close(session, cn_engine)
        self._last_settlement_day = today
        if positions or holds:
            logger.info(
                "T+1 settlement: released %s position(s), %s hold(s)",
                positions,
                holds,
            )
