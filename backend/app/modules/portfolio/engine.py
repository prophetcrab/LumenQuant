"""Periodic equity snapshotter + risk breaker feed.

Without this loop the equity curve only grows when someone opens the dashboard.
Running it on a timer makes the curve honest and gives the breakers a baseline.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.market.service import MarketService
from app.modules.portfolio.service import PortfolioService
from app.modules.risk.policy import RiskContext, RiskLimits, RiskPolicy
from app.modules.risk.service import RiskService
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.service import TradingService

logger = logging.getLogger(__name__)


class PortfolioEngine:
    def __init__(self, database: Database, settings: Settings, *, market_engine=None) -> None:
        self.db = database
        self.settings = settings
        self.market_engine = market_engine
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="portfolio-engine")

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
                logger.exception("portfolio tick failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass

    def tick(self) -> int:
        settings = self.settings
        limits = RiskLimits(
            max_order_notional=settings.risk_max_order_notional,
            max_position_notional=settings.risk_max_position_notional,
            min_order_notional=settings.risk_min_order_notional,
            max_orders_per_minute=settings.risk_max_orders_per_minute,
            max_daily_loss_pct=settings.risk_max_daily_loss_pct,
            max_drawdown_pct=settings.risk_max_drawdown_pct,
        )
        policy = RiskPolicy(limits)
        recorded = 0
        with self.db.session() as session:
            accounts_service = AccountService(
                session, live_trading_enabled=settings.live_trading_enabled
            )
            market = MarketService(
                session,
                clock=getattr(self.market_engine, "clock", None),
                live=self.market_engine,
            )
            trading = TradingService(
                session,
                cash=AccountCashAdapter(accounts_service),
                prices=MarketPriceAdapter(market),
                policy=policy,
                fee_bps=settings.fee_bps,
                slippage_bps=settings.slippage_bps,
            )
            portfolio = PortfolioService(session, accounts_service, trading)
            risk = RiskService(session)
            for account in accounts_service.list(include_archived=False):
                try:
                    overview = portfolio.overview(account.id, record=True)
                    state = risk.state(account.id)
                    if not state.breaker_active:
                        risk.observe(account.id, overview.equity)
                        decision = policy.breaker(
                            RiskContext(
                                cash=overview.cash,
                                position_qty=0.0,
                                position_avg_price=0.0,
                                orders_last_minute=0,
                                equity=overview.equity,
                                day_start_equity=state.day_start_equity or overview.equity,
                                peak_equity=max(state.peak_equity, overview.equity),
                            )
                        )
                        if not decision.allowed:
                            risk.trip(account.id, decision.reason)
                            logger.warning("account %s breaker tripped: %s", account.id, decision.reason)
                    recorded += 1
                except Exception:
                    logger.exception("portfolio snapshot failed for %s", account.id)
        return recorded
