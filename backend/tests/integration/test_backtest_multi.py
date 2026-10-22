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


def test_rebalancing_changes_the_curve(client):
    """Periodic rebalancing must actually change risk and return.

    Without it the sleeves compound independently, so whichever asset ran furthest
    dominates the basket by the end. Snapping back to target weights every N bars
    trims the winner and tops up the laggard — lower return, lower drawdown.
    """
    base = run(client).json()
    assert base["rebalance_bars"] == 0
    assert base["rebalance_count"] == 0

    rebalanced = run(client, rebalance_bars=20).json()
    assert rebalanced["rebalance_bars"] == 20
    assert rebalanced["rebalance_count"] == (rebalanced["aligned_bars"] - 1) // 20
    assert rebalanced["equity"][-1]["equity"] != pytest.approx(
        base["equity"][-1]["equity"], rel=1e-6
    )
    # Same starting capital and same sleeves: only the mix differs.
    assert rebalanced["equity"][0]["equity"] == pytest.approx(
        base["equity"][0]["equity"], abs=1
    )


def test_rebalance_schedule_is_reported(client):
    body = run(client, rebalance_bars=50).json()
    expected = (body["aligned_bars"] - 1) // 50
    assert body["rebalance_count"] == expected


def test_negative_rebalance_is_rejected(client):
    response = run(client, rebalance_bars=-1)
    assert response.status_code == 422


def test_rebalancing_preserves_sleeve_attribution(client):
    """Component returns describe the sleeves, not the rebalanced mix."""
    plain = run(client).json()
    rebalanced = run(client, rebalance_bars=20).json()
    # Per-symbol performance is a property of the strategy on that symbol, so it
    # must be identical whether or not the portfolio rebalances.
    assert [c["return_pct"] for c in plain["components"]] == pytest.approx(
        [c["return_pct"] for c in rebalanced["components"]]
    )


def test_runs_are_persisted_and_listed(client):
    """A basket run must be retrievable later, like a single-symbol run."""
    created = run(client).json()
    assert created["id"]
    assert created["duration_ms"] > 0

    listing = client.get("/api/backtests/multi").json()
    match = [item for item in listing if item["id"] == created["id"]]
    assert match, "run missing from the history list"
    entry = match[0]
    assert entry["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert entry["weights"] and len(entry["weights"]) == 3
    assert entry["metrics"]["total_return_pct"] == pytest.approx(
        created["metrics"]["total_return_pct"], abs=0.01
    )


def test_persisted_run_can_be_reloaded_in_full(client):
    created = run(client, rebalance_bars=20).json()
    loaded = client.get(f"/api/backtests/multi/{created['id']}").json()
    assert loaded["rebalance_bars"] == 20
    assert loaded["aligned_bars"] == created["aligned_bars"]
    assert loaded["equity"] == created["equity"]
    assert loaded["components"] == created["components"]
    assert loaded["currency"] == created["currency"]


def test_persisted_run_can_be_deleted(client):
    created = run(client).json()
    assert client.delete(f"/api/backtests/multi/{created['id']}").status_code == 204
    assert client.get(f"/api/backtests/multi/{created['id']}").status_code == 404
    remaining = client.get("/api/backtests/multi").json()
    assert all(item["id"] != created["id"] for item in remaining)


def test_unknown_run_is_404(client):
    assert client.get("/api/backtests/multi/nope").status_code == 404
    assert client.delete("/api/backtests/multi/nope").status_code == 404


def test_failed_run_is_not_persisted(client):
    """A rejected basket must not leave a stub in the history."""
    before = len(client.get("/api/backtests/multi").json())
    assert run(client, symbols=["BTCUSDT", "600036.SH"]).status_code == 422
    assert len(client.get("/api/backtests/multi").json()) == before


def test_sleeves_are_stored_as_ordinary_runs(client):
    """Each sleeve must be a normal BacktestRun so portfolio analysis can use it.

    Without this the user had to re-run every symbol individually before the
    correlation view could say anything — the basket run knew the answer but had
    no way to hand it over.
    """
    created = run(client).json()
    children = created.get("child_run_ids") or []
    assert len(children) == 3
    assert all(isinstance(item, str) and item for item in children)

    # They are reachable as ordinary runs.
    for run_id in children:
        detail = client.get(f"/api/backtests/{run_id}")
        assert detail.status_code == 200
        assert detail.json()["strategy"] == "sma_cross"


def test_sleeves_stay_out_of_the_single_symbol_history(client):
    """Basket sleeves belong to their parent run, not the flat history list."""
    created = run(client).json()
    history = client.get("/api/backtests").json()
    ids = {item["id"] for item in history}
    assert all(child not in ids for child in created["child_run_ids"])


def test_sleeves_feed_portfolio_analysis(client):
    """The whole point: a basket run can be studied for diversification at once."""
    created = run(client).json()
    response = client.post(
        "/api/backtests/portfolio", json={"run_ids": created["child_run_ids"][:2]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["components"]) == 2
    assert body["correlation"]["matrix"][0][1] == pytest.approx(
        body["correlation"]["matrix"][1][0]
    )
    labels = body["correlation"]["labels"]
    assert "BTCUSDT" in labels[0] and "ETHUSDT" in labels[1]


def test_rebalanced_sleeves_are_stored_unrebalanced(client):
    """Sleeve rows hold the per-symbol strategy curve, not the basket mix.

    The parent run owns the rebalanced combination; a sleeve is "this strategy on
    this symbol", which is what correlation analysis needs.
    """
    rebalanced = run(client, rebalance_bars=20).json()
    plain = run(client, rebalance_bars=0).json()
    a = client.get(f"/api/backtests/{rebalanced['child_run_ids'][0]}").json()
    b = client.get(f"/api/backtests/{plain['child_run_ids'][0]}").json()
    assert a["equity"] == b["equity"]


def test_deleting_a_basket_removes_its_sleeves(client):
    """Orphaned sleeves would appear in portfolio analysis with no parent.

    The user deletes the basket, then later sees three unexplained runs in the
    portfolio picker — and cannot delete them from the single-symbol history
    because they are hidden there.
    """
    created = run(client).json()
    children = created["child_run_ids"]
    assert client.delete(f"/api/backtests/multi/{created['id']}").status_code == 204
    for child in children:
        assert client.get(f"/api/backtests/{child}").status_code == 404


def test_prune_orphan_sleeves_cleans_up_leftovers(client):
    """A crash between deletes (or an older version) can leave sleeves behind."""
    from sqlalchemy import select

    from app.modules.backtest.models import BacktestRun
    from app.modules.backtest.multi import MultiSymbolBacktester

    created = run(client).json()
    settings = client.app.state.settings
    db = Database(settings.database_url)
    with db.session() as session:
        # Detach the children from their parent without deleting the children.
        from app.modules.backtest.models import MultiBacktestRun

        parent = session.get(MultiBacktestRun, created["id"])
        parent.child_run_ids_json = "[]"
        session.commit()
        orphans = session.scalars(
            select(BacktestRun).where(BacktestRun.origin == "multi")
        ).all()
        assert len(orphans) >= 3
        removed = MultiSymbolBacktester(
            client.app.state.live_market, session=session
        ).prune_orphan_sleeves()
        assert removed >= 3
        remaining = session.scalars(
            select(BacktestRun).where(BacktestRun.origin == "multi")
        ).all()
        assert remaining == []
    db.dispose()


def test_merged_curve_flags_an_implausible_sleeve(client):
    """A bad sleeve must surface even though merging dilutes it.

    One sleeve at 1/N weight turning from 100k to 5k still moves the merged curve
    by a large fraction; if no warning fires, the diluted jump reads as a real
    portfolio move.
    """
    from app.modules.backtest.multi import MultiSymbolBacktester

    common = list(range(30))
    # Second sleeve collapses then recovers, as a broken settlement would.
    curves = [
        {t: 100_000.0 for t in common},
        {t: 100_000.0 if t not in (10, 11) else (5_000.0 if t == 10 else 100_000.0) for t in common},
    ]
    report = MultiSymbolBacktester.__new__(MultiSymbolBacktester)
    combined = report._combine(
        curves=curves, common=common, shares=[0.5, 0.5], rebalance_bars=0
    )
    from app.modules.backtest.engine import extreme_bar_return

    flagged = extreme_bar_return(combined)
    assert flagged is not None
    index, change = flagged
    # The drop (index 10) and the recovery (index 11) are both implausible; the
    # helper reports the first one it meets in the direction of iteration.
    assert index in (10, 11)
    assert abs(change) > 40


def test_apply_weights_reruns_the_basket_with_the_new_mix(client):
    """Saving adjusted weights must produce a run that matches those weights.

    Overwriting `weights_json` in place would leave the stored metrics and equity
    curve describing the previous mix — a record that contradicts itself.
    """
    original = run(client).json()
    ids = original["child_run_ids"]

    response = client.post(
        "/api/backtests/portfolio/apply-weights",
        json={"run_ids": ids, "weights": [6, 3, 1]},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["previous_run_id"] == original["id"]
    assert body["id"] != original["id"]
    assert [c["share"] for c in body["components"]] == pytest.approx(
        [0.6, 0.3, 0.1], abs=1e-6
    )
    # A different mix means a different curve; identical output would mean the
    # weights were accepted and ignored.
    assert body["metrics"]["total_return_pct"] != pytest.approx(
        original["metrics"]["total_return_pct"], abs=1e-9
    )
    # The original basket is untouched and still describes its own mix.
    stored = client.get(f"/api/backtests/multi/{original['id']}").json()
    original_shares = [c["share"] for c in original["components"]]
    assert stored["weights"] == pytest.approx(original_shares, abs=1e-6)


def test_apply_weights_rejects_a_partial_sleeve_set(client):
    """Omitting a sleeve would silently drop a position the user still holds."""
    ids = run(client).json()["child_run_ids"]
    response = client.post(
        "/api/backtests/portfolio/apply-weights",
        json={"run_ids": ids[:2], "weights": [1, 1]},
    )
    assert response.status_code == 422
    assert "全部" in response.json()["error"]["message"]


def test_apply_weights_rejects_a_non_sleeve_run(client):
    """Only basket sleeves have a mix to re-apply."""
    single = client.post(
        "/api/backtests",
        json={"strategy": "sma_cross", "symbol": "BTCUSDT", "timeframe": "1h", "bars": 200},
    ).json()
    sleeves = run(client).json()["child_run_ids"]
    response = client.post(
        "/api/backtests/portfolio/apply-weights",
        json={"run_ids": [single["id"], sleeves[0]], "weights": [1, 1]},
    )
    assert response.status_code == 422
    assert "篮子" in response.json()["error"]["message"]


def test_apply_weights_rejects_sleeves_from_two_baskets(client):
    """Components of different baskets must not be merged into one rerun."""
    first = run(client).json()["child_run_ids"]
    second = run(client, strategy="ema_trend").json()["child_run_ids"]
    response = client.post(
        "/api/backtests/portfolio/apply-weights",
        json={"run_ids": [first[0], second[1]], "weights": [1, 1]},
    )
    assert response.status_code == 404
    assert "篮子" in response.json()["error"]["message"]
