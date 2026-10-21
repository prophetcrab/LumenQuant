"""Multi-symbol backtest: capital split, alignment, and basket benchmark."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'multi.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=120,
        market_seed_bars=900,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as test_client:
        yield test_client
    db.dispose()


def run(client: TestClient, **overrides):
    body = {
        "strategy": "sma_cross",
        "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "timeframe": "1h",
        "bars": 200,
        "config": {"initial_capital": 300_000},
    }
    body.update(overrides)
    return client.post("/api/backtests/multi", json=body)


def test_equal_weight_splits_capital_evenly(client):
    response = run(client)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["aligned_bars"] >= 50
    assert len(body["components"]) == 3
    for component in body["components"]:
        assert component["share"] == pytest.approx(1 / 3, abs=1e-6)
        assert component["allocation"] == pytest.approx(100_000, abs=1)
    # Combined curve starts at the full capital, not a per-sleeve slice.
    assert body["equity"][0]["equity"] == pytest.approx(300_000, abs=1)
    assert len(body["equity"]) == body["aligned_bars"]


def test_weights_shift_the_allocation(client):
    response = run(client, weights=[2, 1, 1])
    assert response.status_code == 200, response.text
    components = response.json()["components"]
    assert components[0]["share"] == pytest.approx(0.5, abs=1e-6)
    assert components[1]["share"] == pytest.approx(0.25, abs=1e-6)
    assert components[0]["allocation"] == pytest.approx(150_000, abs=1)


def test_returns_are_internally_consistent(client):
    body = run(client).json()
    metrics = body["metrics"]
    first = body["equity"][0]["equity"]
    last = body["equity"][-1]["equity"]
    assert metrics["total_return_pct"] == pytest.approx(
        (last / first - 1) * 100, abs=0.02
    )
    # Trade counts are summed across sleeves, not averaged.
    assert metrics["trade_count"] == sum(c["trades"] for c in body["components"])


def test_benchmark_is_the_equal_weight_basket(client):
    body = run(client).json()
    metrics = body["metrics"]
    benchmark = metrics["benchmark"]
    assert benchmark["benchmark"] == "等权买入持有"
    # The buy-and-hold benchmark must be measured on the same aligned window.
    assert body["comparison"][0]["strategy"] == pytest.approx(100.0, abs=0.01)
    assert body["comparison"][0]["benchmark"] == pytest.approx(100.0, abs=0.01)
    assert metrics["buy_hold_pct"] == pytest.approx(
        benchmark["benchmark_return_pct"], abs=0.01
    )


def test_rejects_mixed_markets(client):
    response = run(client, symbols=["BTCUSDT", "600036.SH"])
    assert response.status_code == 422
    assert "同一市场" in response.json()["error"]["message"]


def test_rejects_duplicates_and_bad_weights(client):
    assert run(client, symbols=["BTCUSDT", "BTCUSDT"]).status_code == 422
    assert run(client, symbols=["BTCUSDT"]).status_code == 422  # schema min_length
    too_many = run(client, weights=[1, 1])
    assert too_many.status_code == 422
    negative = run(client, weights=[1, -1, 1])
    assert negative.status_code == 422
    assert "正数" in negative.json()["error"]["message"]


def test_unknown_symbol_is_rejected(client):
    response = run(client, symbols=["BTCUSDT", "NOPEUSDT"])
    assert response.status_code == 404


def test_a_share_basket_is_supported(tmp_path):
    """The same endpoint must work on A-shares, with CNY and T+1 rules."""
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'multi-cn.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=600,
        market_seed_bars=300,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        response = client.post(
            "/api/backtests/multi",
            json={
                "strategy": "sma_cross",
                "symbols": ["600036.SH", "601318.SH"],
                "timeframe": "1d",
                "bars": 300,
                "config": {"initial_capital": 500_000},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["currency"] == "CNY"
        assert body["market"] == "cn_stock"
        # 4h is not a valid A-share timeframe.
        bad = client.post(
            "/api/backtests/multi",
            json={
                "strategy": "sma_cross",
                "symbols": ["600036.SH", "601318.SH"],
                "timeframe": "4h",
                "bars": 300,
            },
        )
        assert bad.status_code == 422
    db.dispose()
