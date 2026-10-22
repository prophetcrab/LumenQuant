from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.core.db import Database  # noqa: E402
from app.main import create_app  # noqa: E402

# Tests must never depend on the developer's .env. `Settings` reads the project env
# file, so a machine configured for real data (`LQ_CN_SYNC_ON_START=true`) made
# every create_app() call reach Tencent/Binance inside the suite: a 2.5-minute run
# turned into one that never finished, and results depended on network state.
# Forcing the offline defaults here keeps the suite hermetic however the checkout
# is configured.
_OFFLINE_DEFAULTS = {
    "LQ_CN_DATA_SOURCE": "synthetic",
    "LQ_CRYPTO_DATA_SOURCE": "synthetic",
    "LQ_CN_SYNC_ON_START": "false",
    "LQ_SEED_MARKET": "false",
    "LQ_RUNTIME_ENABLED": "false",
    "LQ_LIVE_TRADING_ENABLED": "false",
    "LQ_LIVE_AUTO_TRADING_ENABLED": "false",
}
for _key, _value in _OFFLINE_DEFAULTS.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
        live_trading_enabled=False,
        live_auto_trading_enabled=False,
        auth_enabled=False,
        # Explicit rather than inherited: see _OFFLINE_DEFAULTS above.
        cn_data_source="synthetic",
        crypto_data_source="synthetic",
        cn_sync_on_start=False,
    )


@pytest.fixture()
def database(settings: Settings) -> Database:
    db = Database(settings.database_url)
    db.create_schema()
    yield db
    db.dispose()


@pytest.fixture()
def app(settings: Settings, database: Database):
    return create_app(settings, database)


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c
