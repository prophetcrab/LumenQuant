"""Background loop that matches resting limit orders against live prices."""

from __future__ import annotations

import asyncio
import logging

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.market.service import MarketService
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
                bus=self.bus,
                fee_bps=self.settings.fee_bps,
                slippage_bps=self.settings.slippage_bps,
            )
            return service.match_open_orders()
