from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.backtest.models import BacktestRun


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'portfolio.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=False,
        market_seed_bars=800,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as test_client:
        yield test_client
    db.dispose()


def _run(client, strategy: str, symbol: str, timeframe: str = "1h", bars: int = 200) -> str:
    response = client.post(
        "/api/backtests",
        json={"strategy": strategy, "symbol": symbol, "timeframe": timeframe, "bars": bars},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_portfolio_combines_two_runs(client):
    ids = [_run(client, "sma_cross", "BTCUSDT"), _run(client, "rsi_reversion", "ETHUSDT")]
    response = client.post("/api/backtests/portfolio", json={"run_ids": ids})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["aligned_bars"] >= 50
    assert len(body["components"]) == 2
    assert sum(item["weight"] for item in body["components"]) == pytest.approx(1.0)
    assert sum(item["allocation"] for item in body["components"]) == pytest.approx(
        body["initial_capital"], rel=1e-6
    )
    assert len(body["equity"]) == body["aligned_bars"]
    # The combined curve starts at the requested capital and ends at the value
    # the reported total return implies — the two must not drift apart.
    assert body["equity"][0]["equity"] == pytest.approx(body["initial_capital"], rel=1e-6)
    expected_final = body["initial_capital"] * (
        1 + body["metrics"]["total_return_pct"] / 100
    )
    assert body["equity"][-1]["equity"] == pytest.approx(expected_final, rel=1e-4)
    assert body["metrics"]["trade_count"] is None  # per-sleeve only
    matrix = body["correlation"]["matrix"]
    assert len(matrix) == 2 and len(matrix[0]) == 2
    assert matrix[0][0] == 1.0 and matrix[1][1] == 1.0
    assert matrix[0][1] == matrix[1][0]


def test_portfolio_is_scale_invariant(client):
    """Doubling the capital must double the curve, not change the shape."""
    ids = [_run(client, "sma_cross", "BTCUSDT"), _run(client, "macd_momentum", "SOLUSDT")]
    base = client.post(
        "/api/backtests/portfolio", json={"run_ids": ids, "initial_capital": 100_000}
    ).json()
    doubled = client.post(
        "/api/backtests/portfolio", json={"run_ids": ids, "initial_capital": 200_000}
    ).json()
    assert doubled["metrics"]["total_return_pct"] == pytest.approx(
        base["metrics"]["total_return_pct"], abs=1e-4
    )
    assert doubled["equity"][-1]["equity"] == pytest.approx(
        base["equity"][-1]["equity"] * 2, rel=1e-4
    )


def test_portfolio_rejects_mismatched_timeframes(client):
    ids = [
        _run(client, "sma_cross", "BTCUSDT", timeframe="1h"),
        _run(client, "sma_cross", "BTCUSDT", timeframe="4h"),
    ]
    response = client.post("/api/backtests/portfolio", json={"run_ids": ids})
    assert response.status_code == 422
    assert "周期" in response.json()["error"]["message"]


def test_portfolio_requires_distinct_known_runs(client):
    single = _run(client, "sma_cross", "BTCUSDT")
    assert (
        client.post("/api/backtests/portfolio", json={"run_ids": [single]}).status_code == 422
    )
    duplicate = client.post("/api/backtests/portfolio", json={"run_ids": [single, single]})
    assert duplicate.status_code == 422
    missing = client.post(
        "/api/backtests/portfolio",
        json={"run_ids": [single, "00000000-0000-0000-0000-000000000000"]},
    )
    assert missing.status_code == 404


def test_portfolio_weights_change_the_outcome(client):
    ids = [_run(client, "sma_cross", "BTCUSDT"), _run(client, "rsi_reversion", "ETHUSDT")]
    equal = client.post("/api/backtests/portfolio", json={"run_ids": ids}).json()
    tilted = client.post(
        "/api/backtests/portfolio", json={"run_ids": ids, "weights": [4, 1]}
    ).json()
    assert tilted["components"][0]["weight"] == pytest.approx(0.8)
    assert tilted["components"][1]["weight"] == pytest.approx(0.2)
    # A different mix must move the curve unless both sleeves are identical.
    assert tilted["metrics"]["total_return_pct"] != pytest.approx(
        equal["metrics"]["total_return_pct"], abs=1e-9
    )
    bad = client.post(
        "/api/backtests/portfolio", json={"run_ids": ids, "weights": [1, 1, 1]}
    )
    assert bad.status_code == 422


def test_portfolio_warns_when_sleeves_are_the_same_bet(client):
    """Two runs of the same strategy on the same symbol are one position."""
    ids = [_run(client, "sma_cross", "BTCUSDT"), _run(client, "sma_cross", "BTCUSDT")]
    body = client.post("/api/backtests/portfolio", json={"run_ids": ids}).json()
    assert body["correlation"]["matrix"][0][1] == pytest.approx(1.0, abs=1e-6)
    # Identical strategy/symbol pairs must still be distinguishable in the UI.
    labels = body["correlation"]["labels"]
    assert labels[0] != labels[1]
    assert labels[0].startswith("sma_cross · BTCUSDT #")
    joined = " ".join(body["warnings"])
    assert "相关性" in joined
    assert "标的相同" in joined
    assert "策略相同" in joined


def test_portfolio_alignment_uses_real_overlap(client, tmp_path):
    """A stored series from another period must not be silently extrapolated."""
    ids = [
        _run(client, "sma_cross", "BTCUSDT"),
        _run(client, "macd_momentum", "ETHUSDT"),
    ]
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    settings = client.app.state.settings
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        row = session.get(BacktestRun, ids[1])
        points = json.loads(row.equity_json)
        # Shift one sleeve a month into the future: overlap collapses.
        shift = 30 * 24 * 60 * 60 * 1000
        row.equity_json = json.dumps(
            [{"time": point["time"] + shift, "equity": point["equity"]} for point in points]
        )
        session.commit()
    engine.dispose()

    response = client.post("/api/backtests/portfolio", json={"run_ids": ids})
    assert response.status_code == 422
    assert "重叠" in response.json()["error"]["message"]


def _inject_jump(client, run_id: str, *, at_fraction: float, magnitude: float) -> None:
    """Rewrite a stored curve to contain one impossible single-bar move."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    settings = client.app.state.settings
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        row = session.get(BacktestRun, run_id)
        points = json.loads(row.equity_json)
        index = max(2, int(len(points) * at_fraction))
        for point in points[index:]:
            point["equity"] = point["equity"] * (1.0 + magnitude)
        row.equity_json = json.dumps(points)
        session.commit()
    engine.dispose()


def test_portfolio_flags_an_implausible_sleeve_curve(client):
    """One bad bar in one sleeve silently distorts every correlation.

    A single -60% return dominates the covariance, so that sleeve reads as
    uncorrelated with everything else even if it moves exactly like the others.
    The analysis must say so instead of publishing a confident-looking matrix.
    """
    ids = [
        _run(client, "sma_cross", "BTCUSDT"),
        _run(client, "rsi_reversion", "ETHUSDT"),
        _run(client, "macd_momentum", "SOLUSDT"),
    ]
    _inject_jump(client, ids[1], at_fraction=0.5, magnitude=-0.60)

    body = client.post("/api/backtests/portfolio", json={"run_ids": ids}).json()
    joined = " ".join(body["warnings"])
    assert "异常跳变" in joined
    # The warning must name which sleeve is compromised, not just "something".
    assert "rsi_reversion · ETHUSDT" in joined
    # A curve that cannot come from price alone invalidates the summary metrics.
    assert "不可直接采信" in joined


def test_robust_matrix_is_reported_when_the_jump_drives_the_correlation(client):
    """Removing the anomaly must visibly move at least one correlation."""
    ids = [
        _run(client, "sma_cross", "BTCUSDT"),
        _run(client, "rsi_reversion", "ETHUSDT"),
    ]
    _inject_jump(client, ids[0], at_fraction=0.4, magnitude=0.8)

    body = client.post("/api/backtests/portfolio", json={"run_ids": ids}).json()
    robust = body["correlation"].get("robust")
    assert robust is not None, body["warnings"]
    assert robust["dropped_bars"] >= 1
    joined = " ".join(body["warnings"])
    # A warning that claims a recomputation happened must be backed by the data:
    # earlier the message was emitted while the matrix itself was never attached,
    # leaving the UI with text pointing at nothing.
    assert "重算相关性" in joined
    assert len(robust["matrix"]) == 2
    # Same shape as the original matrix, so the UI can render either.
    assert len(robust["matrix"][0]) == 2
    # Fewer observations than the raw series.
    assert robust["returns_per_sleeve"] < body["aligned_bars"] - 1


def test_clean_portfolios_report_no_anomaly_and_no_robust_matrix(client):
    """No false positives: a normal pair must not gain a second matrix."""
    ids = [_run(client, "sma_cross", "BTCUSDT"), _run(client, "rsi_reversion", "ETHUSDT")]
    body = client.post("/api/backtests/portfolio", json={"run_ids": ids}).json()
    assert "robust" not in body["correlation"]
    assert not any("异常跳变" in warning for warning in body["warnings"])
