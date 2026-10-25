from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.router import api_router
from app.core.cache import TTLCache
from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.errors import AppError
from app.core.events import EventBus
from app.core.observability import Metrics, RequestContextMiddleware
from app.core.security import AuthMiddleware, RateLimitMiddleware


def _startup_sync(db: Database, settings: Settings) -> None:
    """Best-effort real-data import; never blocks startup on network failure.

    Bounded on purpose. With 500 registered A-shares this ran every symbol through
    a full daily + intraday + quote sync, which took ~23 minutes during which the
    API was not serving at all. A startup import is a convenience, not a
    correctness requirement: each symbol's stored series is already valid, and
    `POST /api/cn/data/sync` refreshes on demand.

    So only the first `cn_sync_startup_symbols` names are refreshed here — the
    ones a fresh deployment actually plots — and the rest are left to the explicit
    sync. The limit is reported in the log so a short import is never mistaken for
    a complete one.
    """
    from app.modules.cn.sources.tencent import TencentSource
    from app.modules.cn.sync import CnDataSyncService

    limit = max(0, int(getattr(settings, "cn_sync_startup_symbols", 20)))
    logger = logging.getLogger(__name__)
    if limit == 0:
        logger.info("startup data sync skipped (LQ_CN_SYNC_STARTUP_SYMBOLS=0)")
        return
    try:
        with db.session() as session:
            source = TencentSource(timeout=settings.request_timeout_seconds)
            from sqlalchemy import select

            from app.modules.market.models import Symbol

            codes = list(
                session.scalars(
                    select(Symbol.code)
                    .where(Symbol.market == "cn_stock", Symbol.enabled.is_(True))
                    .order_by(Symbol.code)
                    .limit(limit)
                ).all()
            )
            report = CnDataSyncService(session, source).sync_symbols(
                codes,
                daily_limit=settings.cn_sync_daily_limit,
                intraday_limit=settings.cn_sync_intraday_limit,
            )
            logger.info(
                "startup data sync: %s of %s symbols (capped by LQ_CN_SYNC_STARTUP_SYMBOLS), ok=%s",
                len(report.symbols),
                limit,
                report.ok,
            )
    except Exception:
        logger.exception("startup data sync failed; keeping local data")


def _register_live_symbols(db: Database, provider) -> None:
    """Tell the composite provider which market owns each symbol."""
    from sqlalchemy import select as sa_select

    from app.modules.market.models import Symbol

    with db.session() as session:
        for code, market in session.execute(
            sa_select(Symbol.code, Symbol.market)
        ).all():
            provider.register(code, market)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db = database or Database(settings.database_url)
    bus = EventBus()
    if settings.cn_extra_holidays:
        from app.modules.cn.calendar import add_holidays

        add_holidays(settings.cn_extra_holidays)
    market_engine = None
    cn_market_engine = None
    cn_quote_engine = None
    cn_symbol_registrar = None
    live_provider = None
    trading_engine = None
    runtime_engine = None
    portfolio_engine = None
    reconcile_engine = None
    cn_gateway = None
    realtime_crypto = (
        settings.crypto_data_source == "binance" and settings.crypto_realtime_enabled
    )
    if realtime_crypto:
        # Real data owns the crypto series; the synthetic engine is not started at
        # all, because it would resume writing simulated bars between polls.
        from app.modules.market.crypto_quotes import CryptoQuoteEngine

        market_engine = CryptoQuoteEngine(db, settings)
    elif settings.seed_market:
        from app.modules.market.engine import MarketEngine

        market_engine = MarketEngine(db, settings)
    realtime_cn = (
        settings.cn_data_source != "synthetic" and settings.cn_realtime_enabled
    )
    if settings.cn_symbols_enabled:
        from app.modules.cn.engine import CnMarketEngine

        # The A-share symbol catalog is registry data, not synthetic market data.
        # Keeping it tied to seed_market meant a real-data-only deployment
        # (seed_market=false, cn_data_source=tencent) had no symbols at all, and
        # every sync failed with "本地未登记标的" before it could fetch a quote.
        #
        # Registration is deferred to the lifespan: create_app() runs at import
        # time, before create_schema(), so querying the table here crashed a
        # fresh deployment on startup with "no such table: market_symbols".
        if realtime_cn:
            # Real data owns the CN price series. The synthetic engine would keep
            # writing bars at a completely different price level between syncs,
            # so in this mode it only registers the catalog.
            cn_symbol_registrar = CnMarketEngine(db, settings).ensure_symbols
            from app.modules.cn.quotes import CnQuoteEngine

            cn_quote_engine = CnQuoteEngine(db, settings)
            cn_market_engine = cn_quote_engine
        elif settings.seed_market:
            cn_market_engine = CnMarketEngine(db, settings)
        else:
            cn_symbol_registrar = CnMarketEngine(db, settings).ensure_symbols
    from app.modules.cn.provider import CompositeLiveProvider

    live_provider = CompositeLiveProvider(crypto=market_engine, cn=cn_market_engine)
    # One gateway instance per process: the reconciler polls the same object that
    # accepted the order, so in-memory gateway state (mock or session-bound HTTP
    # adapter) is not recreated on every request.
    from app.modules.brokers.ths import build_ths_gateway

    # `price_feed` is what lets the mock gateway fill market orders. Without it
    # every rehearsal order sat at `accepted` for ever, which looks exactly like
    # a broken gateway rather than an unfilled simulation. The provider exposes
    # the in-progress candle per symbol, whose close is the newest price.
    def _gateway_price(symbol: str) -> float:
        candle = live_provider.live_candle(symbol) if live_provider is not None else None
        return float(candle.close) if candle is not None else 0.0

    cn_gateway = build_ths_gateway(settings, price_feed=_gateway_price)
    # Same reasoning for crypto: one signed client, one set of cached symbol
    # filters, shared by the request path and the reconciler.
    from app.modules.brokers.binance import build_broker

    crypto_broker = build_broker(settings)
    if settings.runtime_enabled and (market_engine is not None or cn_market_engine is not None):
        from app.modules.runtime.engine import StrategyRuntimeEngine
        from app.modules.trading.engine import TradingEngine

        trading_engine = TradingEngine(
            db, settings, market_engine=live_provider, bus=bus
        )
        runtime_engine = StrategyRuntimeEngine(
            db, settings, market_engine=live_provider, bus=bus
        )
        from app.modules.portfolio.engine import PortfolioEngine

        portfolio_engine = PortfolioEngine(db, settings, market_engine=live_provider)
    # The reconciler is deliberately outside the block above. Mirroring live
    # fills is a money-safety duty, not a strategy feature: with
    # LQ_RUNTIME_ENABLED=false (the supported "manual live orders only" mode)
    # or no market engine, it was never constructed, so live orders never moved
    # off `open` and their cash stayed reserved for ever.
    if settings.live_trading_enabled and (cn_gateway is not None or crypto_broker is not None):
        from app.modules.brokers.reconciler import LiveReconciler

        reconcile_engine = LiveReconciler(
            db, settings, gateway=cn_gateway, broker=crypto_broker
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db.create_schema()
        if cn_symbol_registrar is not None:
            await asyncio.to_thread(cn_symbol_registrar)
        if market_engine is not None:
            await market_engine.start()
        # Import history before the realtime poller starts: the sync replaces the
        # stored series wholesale, and a live bar written in between would be
        # deleted by that purge and only restored 15 minutes later.
        if settings.cn_sync_on_start and settings.cn_data_source != "synthetic":
            await asyncio.to_thread(_startup_sync, db, settings)
        if cn_market_engine is not None:
            await cn_market_engine.start()
        if live_provider is not None:
            await asyncio.to_thread(_register_live_symbols, db, live_provider)
        if trading_engine is not None:
            await trading_engine.start()
        if runtime_engine is not None:
            await runtime_engine.start()
        if portfolio_engine is not None:
            await portfolio_engine.start()
        if reconcile_engine is not None:
            await reconcile_engine.start()
        try:
            yield
        finally:
            if reconcile_engine is not None:
                await reconcile_engine.stop()
            if portfolio_engine is not None:
                await portfolio_engine.stop()
            if runtime_engine is not None:
                await runtime_engine.stop()
            if trading_engine is not None:
                await trading_engine.stop()
            if cn_market_engine is not None:
                await cn_market_engine.stop()
            if market_engine is not None:
                await market_engine.stop()
            db.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.bus = bus
    app.state.market_cache = TTLCache(ttl_seconds=1.5, max_entries=32)
    app.state.candle_cache = TTLCache(ttl_seconds=2.0, max_entries=256)
    app.state.stats_cache = TTLCache(ttl_seconds=30.0, max_entries=32)
    # Whole-market rows are ~5,400 dicts; a short TTL keeps interactive
    # re-filtering instant without hammering the upstream source.
    app.state.screener_cache = TTLCache(ttl_seconds=60.0, max_entries=4)
    app.state.metrics = Metrics()
    app.state.market_engine = market_engine
    app.state.cn_market_engine = cn_market_engine
    app.state.cn_quote_engine = cn_quote_engine
    app.state.live_market = live_provider
    app.state.trading_engine = trading_engine
    app.state.runtime_engine = runtime_engine
    app.state.portfolio_engine = portfolio_engine
    app.state.reconcile_engine = reconcile_engine
    app.state.cn_gateway = cn_gateway
    app.state.crypto_broker = crypto_broker

    app.add_middleware(RequestContextMiddleware, metrics=app.state.metrics)
    app.add_middleware(
        RateLimitMiddleware, limit=settings.rate_limit_per_minute, window_seconds=60.0
    )
    app.add_middleware(
        AuthMiddleware, token=settings.auth_token, enabled=settings.auth_enabled
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health", tags=["system"])
    def health():
        try:
            with db.session() as session:
                session.execute(text("SELECT 1"))
            database_status = "ok"
        except Exception:
            database_status = "unavailable"
        return {
            "status": "ok" if database_status == "ok" else "degraded",
            "version": settings.app_version,
            "database": database_status,
            "environment": settings.environment,
        }

    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
