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

    # Background runtime (strategy runners, matching loop, equity snapshots).
    runtime_enabled: bool = True
    engine_interval_seconds: float = 2.0

    # Safety switches.
    live_trading_enabled: bool = False
    auth_enabled: bool = False
    auth_token: str = ""

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
