"""Background scheduler for running strategy instances."""

from __future__ import annotations

import asyncio
import logging
import time

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.cn.fees import CnFeeSchedule
from app.modules.market.service import MarketService
from app.modules.runtime.service import RuntimeService
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.service import TradingService

logger = logging.getLogger(__name__)

# Logs are written by a background loop nobody watches; check the cap hourly.
LOG_PRUNE_INTERVAL_SECONDS = 3600.0


class StrategyRuntimeEngine:
    def __init__(self, database: Database, settings: Settings, *, market_engine=None, bus=None) -> None:
        self.db = database
        self.settings = settings
        self.market_engine = market_engine
        self.bus = bus
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_prune = 0.0

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="strategy-runtime")

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
                logger.exception("strategy runtime tick failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=max(1.0, self.settings.engine_interval_seconds * 3),
                )
            except asyncio.TimeoutError:
                pass

    def tick(self) -> dict[str, str]:
        with self.db.session() as session:
            accounts = AccountService(
                session, live_trading_enabled=self.settings.live_trading_enabled
            )
            market = MarketService(
                session,
                clock=getattr(self.market_engine, "clock", None),
                live=self.market_engine,
            )
            trading = TradingService(
                session,
                cash=AccountCashAdapter(accounts),
                prices=MarketPriceAdapter(market),
                bus=self.bus,
                fee_bps=self.settings.fee_bps,
                slippage_bps=self.settings.slippage_bps,
                cn_fees=CnFeeSchedule(
                    commission_bps=self.settings.cn_commission_bps,
                    min_commission=self.settings.cn_min_commission,
                    stamp_duty_bps=self.settings.cn_stamp_duty_bps,
                    transfer_fee_bps=self.settings.cn_transfer_fee_bps,
                ),
                cn_session_enforced=self.settings.cn_session_enforced,
                live_market=self.market_engine,
            )
            service = RuntimeService(
                session, accounts_service=accounts, market=market, trading=trading
            )
            results: dict[str, str] = {}
            # Protective exits run first: a stop must fire even when no new bar
            # has closed, and even if the strategy loop below throws.
            try:
                results.update(service.protect_positions())
            except Exception:
                logger.exception("protective exit sweep failed")
            for row in service.running_instances():
                try:
                    results[row.id] = service.evaluate_instance(row)
                except Exception as exc:  # one bad instance must not stop the rest
                    logger.exception("instance %s evaluation failed", row.id)
                    row.last_error = str(exc)[:400]
                    session.commit()
                    results[row.id] = "error"
            # Logs grow unattended; trim once an hour rather than on every tick.
            now = time.monotonic()
            if now - self._last_prune >= LOG_PRUNE_INTERVAL_SECONDS:
                self._last_prune = now
                try:
                    removed = service.prune_all_logs()
                    session.commit()
                    if removed:
                        logger.info("pruned %s runtime log rows", removed)
                except Exception:
                    logger.exception("runtime log pruning failed")
            return results
