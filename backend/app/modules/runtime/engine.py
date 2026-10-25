"""Background scheduler for running strategy instances."""

from __future__ import annotations

import asyncio
import logging
import time

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.alerts.service import AlertService
from app.modules.cn.fees import CnFeeSchedule
from app.modules.market.service import MarketService
from app.modules.risk.service import RiskService
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
        self.alert_retention_days = int(
            getattr(settings, "alert_retention_days", 30)
        )

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
            # One alert service per tick, shared by every collaborator. It holds
            # only the session, so three separate instances were pure overhead.
            alerts = AlertService(session)
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
                risk=RiskService(session, alerts=alerts),
                alerts=alerts,
            )
            service = RuntimeService(
                session,
                accounts_service=accounts,
                market=market,
                trading=trading,
                live_auto_trading_enabled=self.settings.live_auto_trading_enabled,
                alerts=alerts,
            )
            results: dict[str, str] = {}
            # One read of the running set for both phases. The sweep used to
            # query it again on its own, so every tick listed the same rows twice
            # and the two phases could even disagree if the set changed between
            # them.
            running = service.running_instances()
            # Protective exits run first: a stop must fire even when no new bar
            # has closed, and even if the strategy loop below throws.
            try:
                results.update(service.protect_positions(running))
            except Exception:
                logger.exception("protective exit sweep failed")
            for row in running:
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
                try:
                    # Acknowledged alerts are history; unacknowledged ones are
                    # open questions and are never pruned by age.
                    # AlertService is imported at module level; re-importing it
                    # here would bind the name locally for the whole method and
                    # break the TradingService/AlertService construction above.
                    purged = AlertService(session).prune(keep_days=self.alert_retention_days)
                    if purged:
                        logger.info("pruned %s acknowledged alerts", purged)
                except Exception:
                    logger.exception("alert pruning failed")
                try:
                    from app.modules.backtest.multi import MultiSymbolBacktester

                    # MarketService is imported at module level. Re-importing it
                    # here bound the name locally for the *whole* function, which
                    # made every tick fail with UnboundLocalError at the top of
                    # the method — quietly, because the loop logs and continues.
                    multi = MultiSymbolBacktester(
                        MarketService(session), session=session
                    )
                    pruned = multi.prune_orphan_sleeves()
                    if pruned:
                        logger.info("pruned %s orphan backtest sleeves", pruned)
                except Exception:
                    logger.exception("orphan sleeve pruning failed")
                try:
                    # Equity snapshots are written every ~10s per account and were
                    # never pruned. Two-tier downsampling keeps the curve readable
                    # without letting the table grow without bound.
                    from app.modules.portfolio.service import prune_equity_snapshots

                    removed = prune_equity_snapshots(session)
                    if removed:
                        logger.info("downsampled %s old equity snapshots", removed)
                except Exception:
                    logger.exception("equity snapshot pruning failed")
            return results
