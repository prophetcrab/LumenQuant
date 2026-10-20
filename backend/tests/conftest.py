from __future__ import annotations

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


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        seed_market=False,
        runtime_enabled=False,
        live_trading_enabled=False,
        auth_enabled=False,
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
