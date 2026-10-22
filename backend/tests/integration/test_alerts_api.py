"""Alerts are for decisions, not forensics."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'alerts.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=60,
        market_seed_bars=400,
        market_tick_seconds=3600,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,
        risk_max_orders_per_minute=1000,
        risk_max_daily_loss_pct=0.01,
        risk_max_drawdown_pct=0.05,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as test_client:
        yield test_client
    db.dispose()


def test_breaker_trip_creates_a_critical_alert(client):
    account = client.post(
        "/api/accounts", json={"name": "熔断账户", "initial_balance": 100_000}
    ).json()
    # Force the daily-loss breaker: observe a baseline, then trip it.
    client.post(
        "/api/risk/observe",
        params={"account_id": account["id"], "equity": 100_000},
    )
    response = client.post(
        "/api/risk/observe",
        params={"account_id": account["id"], "equity": 90_000},
    )
    assert response.status_code == 200
    assert response.json()["breaker_active"] is True

    alerts = client.get("/api/alerts", params={"account_id": account["id"]}).json()
    breaker = [item for item in alerts if item["category"] == "breaker"]
    assert breaker, alerts
    assert breaker[0]["level"] == "critical"
    assert breaker[0]["acknowledged"] is False


def test_alert_can_be_acknowledged(client):
    account = client.post(
        "/api/accounts", json={"name": "确认账户", "initial_balance": 100_000}
    ).json()
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 100_000})
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 90_000})

    alert = client.get(
        "/api/alerts", params={"account_id": account["id"]}
    ).json()[0]
    acked = client.post(f"/api/alerts/{alert['id']}/ack").json()
    assert acked["acknowledged"] is True
    assert acked["acknowledged_at"]

    unacked = client.get(
        "/api/alerts",
        params={"account_id": account["id"], "unacknowledged_only": True},
    ).json()
    assert all(item["id"] != alert["id"] for item in unacked)


def test_acknowledge_all_and_summary(client):
    account = client.post(
        "/api/accounts", json={"name": "汇总账户", "initial_balance": 100_000}
    ).json()
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 100_000})
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 90_000})

    summary = client.get("/api/alerts/summary", params={"account_id": account["id"]}).json()
    assert summary["unacknowledged"] >= 1
    assert summary["by_level"]["critical"] >= 1

    cleared = client.post("/api/alerts/ack-all", params={"account_id": account["id"]}).json()
    assert cleared["acknowledged"] >= 1
    after = client.get("/api/alerts/summary", params={"account_id": account["id"]}).json()
    assert after["unacknowledged"] == 0


def test_repeated_breaker_trips_do_not_flood(client):
    """The scheduler re-evaluates constantly; one open alert is enough."""
    account = client.post(
        "/api/accounts", json={"name": "去重账户", "initial_balance": 100_000}
    ).json()
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 100_000})
    for _ in range(4):
        client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 90_000})
    alerts = client.get(
        "/api/alerts", params={"account_id": account["id"], "unacknowledged_only": True}
    ).json()
    assert len([item for item in alerts if item["category"] == "breaker"]) == 1


def test_stop_loss_creates_an_alert(client):
    account = client.post(
        "/api/accounts",
        json={"name": "止损告警", "initial_balance": 500_000, "market": "cn_stock"},
    ).json()
    code = client.get("/api/cn/symbols").json()[0]["code"]
    instance = client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "buy_and_hold",
            "symbol": code,
            "timeframe": "1d",
            "params": {},
            "position_pct": 0.2,
            "max_order_notional": 50_000,
            "stop_loss_pct": 3,
            "start": True,
        },
    ).json()
    client.post("/api/runtime/tick")

    # Mark the position entry far above the market so the stop is already breached;
    # the synthetic feed's price is not something the test can push down directly.
    from sqlalchemy import select

    from app.modules.trading.models import Position

    settings = client.app.state.settings
    db = Database(settings.database_url)
    with db.session() as session:
        position = session.scalar(
            select(Position).where(Position.account_id == account["id"])
        )
        assert position is not None, "instance did not open a position"
        position.avg_price = position.avg_price * 2
        session.commit()
    db.dispose()

    client.post("/api/runtime/tick")
    alerts = client.get("/api/alerts", params={"account_id": account["id"]}).json()
    categories = [a["category"] for a in alerts]
    # A same-day A-share stop cannot actually sell (T+1), so the operator gets an
    # `exit_failed` alert instead of a `stop_loss` one — that is the more urgent
    # message: the position is unprotected and still open. Either alert proves the
    # protective path reached the alert layer.
    assert set(categories) & {"stop_loss", "trailing_stop", "take_profit", "exit_failed"}, categories
    assert any(a["ref_id"] == instance["id"] for a in alerts)


def test_unknown_alert_is_404(client):
    assert client.post("/api/alerts/nope/ack").status_code == 404


def test_category_filter(client):
    account = client.post(
        "/api/accounts", json={"name": "分类账户", "initial_balance": 100_000}
    ).json()
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 100_000})
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 90_000})

    breaker = client.get(
        "/api/alerts", params={"account_id": account["id"], "category": "breaker"}
    ).json()
    assert breaker and all(item["category"] == "breaker" for item in breaker)

    other = client.get(
        "/api/alerts", params={"account_id": account["id"], "category": "gateway_failure"}
    ).json()
    assert other == []

    categories = client.get(
        "/api/alerts/categories", params={"account_id": account["id"]}
    ).json()
    assert any(item["category"] == "breaker" and item["count"] >= 1 for item in categories)


def test_prune_removes_only_old_acknowledged_alerts(client):
    """Unacknowledged alerts are open questions and must survive any pruning."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.core.timeutil import utcnow
    from app.modules.alerts.models import Alert

    settings = client.app.state.settings
    db = Database(settings.database_url)
    with db.session() as session:
        old_acked = Alert(
            account_id="acc",
            level="warning",
            category="breaker",
            title="旧告警",
            acknowledged=True,
            acknowledged_at=utcnow() - timedelta(days=60),
            created_at=utcnow() - timedelta(days=60),
        )
        old_open = Alert(
            account_id="acc",
            level="critical",
            category="breaker",
            title="未处理的旧告警",
            acknowledged=False,
            created_at=utcnow() - timedelta(days=60),
        )
        fresh_acked = Alert(
            account_id="acc",
            level="info",
            category="breaker",
            title="新告警",
            acknowledged=True,
            acknowledged_at=utcnow(),
            created_at=utcnow(),
        )
        session.add_all([old_acked, old_open, fresh_acked])
        session.commit()
    db.dispose()

    removed = client.post(
        "/api/alerts/prune", params={"keep_days": 30, "account_id": "acc"}
    ).json()
    assert removed["deleted"] == 1

    titles = {
        item["title"]
        for item in client.get("/api/alerts", params={"account_id": "acc"}).json()
    }
    assert "旧告警" not in titles
    assert "未处理的旧告警" in titles
    assert "新告警" in titles


def test_prune_rejects_negative_days(client):
    assert client.post("/api/alerts/prune", params={"keep_days": -1}).status_code == 422


def test_platform_wide_alert_is_visible_to_every_account(client):
    """A dead price feed has no account, yet every account must see it.

    Filtering strictly by account_id made the most severe alert category
    invisible: it never appeared in the list, the category filter or the badge.
    """
    account = client.post(
        "/api/accounts",
        json={"name": "告警可见性", "initial_balance": 100000, "market": "cn_stock"},
    ).json()

    # Simulate what the quote engines raise on their own session: no account_id.
    from app.modules.alerts.service import AlertService

    with client.app.state.db.session() as session:
        AlertService(session).raise_alert(
            category="data_feed",
            level="critical",
            title="A 股实时行情中断",
            message="启动后从未成功获取报价。",
            ref_type="data_feed",
            ref_id="cn",
            commit=True,
        )

    listed = client.get("/api/alerts", params={"account_id": account["id"]}).json()
    assert any(row["category"] == "data_feed" for row in listed)

    summary = client.get("/api/alerts/summary", params={"account_id": account["id"]}).json()
    assert summary["by_level"]["critical"] == 1

    categories = client.get(
        "/api/alerts/categories", params={"account_id": account["id"]}
    ).json()
    assert any(row["category"] == "data_feed" for row in categories)


def test_alerts_can_be_filtered_by_time_window(client):
    """A time window is how an operator asks "what happened overnight?"."""
    from datetime import timedelta

    from app.core.timeutil import utcnow
    from app.modules.alerts.service import AlertService

    account = client.post(
        "/api/accounts", json={"name": "时间窗", "initial_balance": 100_000}
    ).json()
    with client.app.state.db.session() as session:
        svc = AlertService(session)
        old = svc.raise_alert(
            category="breaker", title="旧告警", account_id=account["id"], ref_id="old"
        )
        old.created_at = utcnow() - timedelta(days=3)
        session.commit()
        svc.raise_alert(
            category="breaker", title="新告警", account_id=account["id"], ref_id="new"
        )

    cutoff = (utcnow() - timedelta(days=1)).isoformat()
    recent = client.get(
        "/api/alerts", params={"account_id": account["id"], "since": cutoff}
    ).json()
    titles = [row["title"] for row in recent]
    assert "新告警" in titles
    assert "旧告警" not in titles

    # An `until` bound excludes the newer row instead.
    older = client.get(
        "/api/alerts",
        params={"account_id": account["id"], "until": cutoff},
    ).json()
    titles = [row["title"] for row in older]
    assert "旧告警" in titles
    assert "新告警" not in titles


def test_summary_can_total_across_all_accounts(client):
    """The sidebar badge must surface a problem in a *different* account."""
    from app.modules.alerts.service import AlertService

    first = client.post(
        "/api/accounts", json={"name": "账户一", "initial_balance": 100_000}
    ).json()
    second = client.post(
        "/api/accounts", json={"name": "账户二", "initial_balance": 100_000}
    ).json()
    with client.app.state.db.session() as session:
        svc = AlertService(session)
        svc.raise_alert(
            category="breaker", level="critical", title="一的告警",
            account_id=first["id"], ref_id="a", commit=True,
        )
        svc.raise_alert(
            category="stop_loss", level="warning", title="二的告警",
            account_id=second["id"], ref_id="b", commit=True,
        )
        # Platform-wide alert has no account at all.
        svc.raise_alert(
            category="data_feed", level="critical", title="行情中断",
            ref_type="data_feed", ref_id="cn", commit=True,
        )

    scoped = client.get(
        "/api/alerts/summary", params={"account_id": first["id"]}
    ).json()
    assert scoped["unacknowledged"] == 2  # own breaker + platform-wide
    assert "accounts" not in scoped

    total = client.get(
        "/api/alerts/summary", params={"all_accounts": "true"}
    ).json()
    # Every open alert, including the account-less one.
    assert total["unacknowledged"] == 3
    assert total["by_level"]["critical"] == 2
    per_account = {item["account_id"]: item for item in total["accounts"]}
    assert per_account[first["id"]]["unacknowledged"] == 1
    assert per_account[second["id"]]["unacknowledged"] == 1
    # Account-less alerts are reported under the empty key, not attributed to an
    # arbitrary account.
    assert per_account[""]["unacknowledged"] == 1
