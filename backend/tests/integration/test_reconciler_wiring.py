"""The reconciler must run whenever live trading is on, not only with the runtime.

Mirroring live fills is a money-safety duty: an order that is never polled keeps
its cash reserved for ever and its position never appears in the book. Tying that
to `LQ_RUNTIME_ENABLED` meant the supported "manual live orders only" mode — and
any deployment without a market engine — silently had no reconciliation at all.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


def _build(tmp_path, name: str, **overrides):
    base = dict(
        seed_market=False,
        cn_symbols_enabled=False,
        market_tick_seconds=3600,
    )
    base.update(overrides)
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / name).as_posix()}", **base
    )
    db = Database(settings.database_url)
    db.create_schema()
    return settings, db, create_app(settings, db)


def test_reconciler_runs_with_the_runtime_disabled(tmp_path):
    """Manual-only live trading still needs fills mirrored."""
    _settings, db, app = _build(
        tmp_path,
        "manual_live.db",
        runtime_enabled=False,
        live_trading_enabled=True,
        binance_api_key="key",
        binance_api_secret="secret",
        binance_base_url="https://api.binance.test",
    )
    assert app.state.reconcile_engine is not None
    assert app.state.runtime_engine is None
    db.dispose()


def test_reconciler_is_skipped_when_live_trading_is_off(tmp_path):
    _settings, db, app = _build(tmp_path, "off.db", runtime_enabled=False)
    assert app.state.reconcile_engine is None
    db.dispose()


def test_status_exposes_whether_fills_are_being_polled(tmp_path):
    """Credentials alone do not mean a live order can reach `filled`."""
    settings, db, app = _build(
        tmp_path,
        "status.db",
        runtime_enabled=False,
        live_trading_enabled=True,
        binance_api_key="key",
        binance_api_secret="secret",
        binance_base_url="https://api.binance.test",
    )
    with TestClient(app) as client:
        body = client.get("/api/brokers/status").json()
    # Inside the lifespan the loop really is running — that is the point of the
    # field: credentials configured alone would not tell the operator whether
    # fills are arriving.
    assert body["reconciler_running"] is True
    assert body["channels"]["binance"]["ready"] is True
    assert body["live_auto_trading_enabled"] is False
    db.dispose()


def test_gateway_price_feed_is_wired_for_rehearsal_fills(tmp_path):
    """The mock gateway needs a price, or every rehearsal order sits at accepted.

    `build_ths_gateway` was called without `price_feed`, so market orders never
    filled and the rehearsal path looked exactly like a dead gateway.
    """
    settings, db, app = _build(
        tmp_path,
        "feed.db",
        seed_market=True,
        market_seed_bars=120,
        runtime_enabled=False,
        live_trading_enabled=True,
        ths_mock_enabled=True,
        cn_symbols_enabled=True,
        cn_seed_days=30,
        cn_session_enforced=False,
        cn_live_session_gate=False,
    )
    with TestClient(app):
        gateway = app.state.cn_gateway
        assert gateway is not None
        assert gateway._price_feed is not None
        price = gateway._price_feed("600036.SH")
        assert price > 0, "the mock gateway must be able to price an A-share"
    db.dispose()
