"""Typed application configuration.

Every runtime knob is explicit here so modules never read os.environ directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LQ_",
        env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "LumenQuant"
    app_version: str = "0.1.0"
    environment: str = "development"
    api_prefix: str = "/api"
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'lumenquant.db').as_posix()}"
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]

    # Market simulation (demo acceleration: one 15m candle every N seconds).
    seed_market: bool = True
    market_seed_bars: int = 11_520
    market_tick_seconds: float = 5.0
    market_timeframe: str = "15m"
    # Speed of the synthetic clock. The engines read this via getattr, so leaving
    # it undefined here meant LQ_MARKET_CLOCK_SPEED was silently ignored and the
    # clock was always 60x — with charts running days ahead of wall time. Set to
    # 1 for real-time behaviour. Irrelevant when a real data source is active.
    market_clock_speed: float = 60.0

    # Crypto real market data. `synthetic` keeps the deterministic generator;
    # `binance` replaces it with public tickers + klines (no API key, no orders).
    crypto_data_source: str = "synthetic"
    crypto_data_base_url: str = "https://data-api.binance.vision"
    crypto_realtime_enabled: bool = True
    crypto_quote_poll_seconds: float = 5.0
    # Real-history depth per symbol on import. Binance caps one call at 1,000
    # bars, so the source pages with startTime/endTime to reach these totals.
    # 8,000 15m bars ~= 83 days; 2,000 daily ~= 5.5 years. Raising these raises
    # first-start import time roughly linearly (~8 extra requests per symbol per
    # 8,000 intraday bars).
    crypto_sync_intraday_limit: int = 8000
    crypto_sync_daily_limit: int = 2000
    # Hourly is imported from its own endpoint so multi-year analysis does not
    # need 4x the rows at 15m. ~70,000 bars covers BTCUSDT from 2019.
    crypto_sync_hourly_limit: int = 70_000
    # Optional lower bound for the hourly import (epoch ms). Empty means "as far
    # back as the exchange goes", which for BTC on Binance is 2017.
    crypto_sync_start_ms: int | None = None

    # Background runtime (strategy runners, matching loop, equity snapshots).
    runtime_enabled: bool = True
    engine_interval_seconds: float = 2.0

    # Execution costs for paper fills.
    fee_bps: float = 10.0
    slippage_bps: float = 3.0

    # A-share mode.
    cn_symbols_enabled: bool = True
    cn_seed_days: int = 600
    # Paper accounts skip the session gate by default so the demo works at night.
    # Live A-share orders are always gated on the real Beijing session.
    cn_session_enforced: bool = False
    # Live A-share orders are gated on the real Beijing session by default.
    # Only disable for rehearsal environments; never for real money.
    cn_live_session_gate: bool = True
    cn_extra_holidays: list[str] = []
    # synthetic | tencent — real data is opt-in because it needs network access.
    cn_data_source: str = "synthetic"
    cn_sync_on_start: bool = False
    # How much history a real-data import asks for. These are the practical
    # ceilings of the free upstream, measured 2026-10:
    #   Tencent 15m -> 800 bars (~50 trading days); above that it returns 320
    #   Tencent 1d  -> 2,000 bars (~8 years); above that the payload breaks
    # A-share minute history is not available further back from a free source, so
    # a backtest that needs more intraday data must aggregate from 1d or import a
    # CSV. Daily is the series to rely on for long A-share research.
    cn_sync_daily_limit: int = 2000
    cn_sync_intraday_limit: int = 800
    # How many registered symbols the *startup* import refreshes. A full pass over
    # 500 names takes ~23 minutes and the API serves nothing until it finishes;
    # stored series are already valid, so a handful is enough for a fresh
    # deployment and the rest come from POST /api/cn/data/sync. 0 disables it.
    cn_sync_startup_symbols: int = 20
    # Realtime A-share quote polling. Requires cn_data_source=tencent. In that
    # mode the synthetic CN engine is reduced to symbol registration: letting it
    # keep ticking would write synthetic bars over real ones at a different price
    # level, the defect that made change% and limit bands meaningless.
    cn_realtime_enabled: bool = True
    cn_quote_poll_seconds: float = 15.0
    cn_commission_bps: float = 2.5
    cn_min_commission: float = 5.0
    cn_stamp_duty_bps: float = 5.0
    cn_transfer_fee_bps: float = 0.1

    # Tonghuashun (同花顺) order gateway. Empty URL disables the channel.
    # The gateway is an authorized local/broker-side HTTP service; this platform
    # never automates the THS desktop client.
    ths_gateway_url: str = ""
    ths_gateway_token: str = ""
    # Deterministic in-process gateway for demos and tests of the live path.
    ths_mock_enabled: bool = False
    live_reconcile_seconds: float = 3.0

    # Risk limits. Percent fields are fractions (0.05 == 5%); 0 disables a breaker.
    risk_max_order_notional: float = 1_000_000.0
    risk_max_position_notional: float = 5_000_000.0
    risk_min_order_notional: float = 10.0
    risk_max_orders_per_minute: int = 120
    risk_max_daily_loss_pct: float = 0.05
    risk_max_drawdown_pct: float = 0.25

    # Safety switches.
    live_trading_enabled: bool = False
    # Separate opt-in for *automated* live orders. live_trading_enabled alone only
    # permits manual orders (which also require the X-Live-Confirm header). An
    # auto-running strategy places orders with nobody watching, so enabling it is
    # a materially different decision and must be made explicitly.
    live_auto_trading_enabled: bool = False
    # Acknowledged alerts older than this are pruned. Unacknowledged ones are
    # kept forever: nobody has acted on them yet.
    alert_retention_days: int = 30
    auth_enabled: bool = False
    auth_token: str = ""
    rate_limit_per_minute: int = 600

    # Optional OpenAI-compatible assistant.
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # Live broker credentials (never persisted to the database).
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_base_url: str = "https://api.binance.com"

    request_timeout_seconds: float = 10.0
    log_level: str = "INFO"

    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"


@lru_cache
def get_settings() -> Settings:
    return Settings()
