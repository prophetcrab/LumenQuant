"""Live gateway failures must be visible, auditable, and must not fake success."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def live_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'gw.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=60,
        market_seed_bars=300,
        market_tick_seconds=3600,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,
        live_trading_enabled=True,
        ths_mock_enabled=True,
        risk_max_orders_per_minute=1000,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client, app
    db.dispose()


def _live_account(client: TestClient) -> dict:
    return client.post(
        "/api/accounts",
        json={
            "name": "网关账户",
            "kind": "live",
            "market": "cn_stock",
            "initial_balance": 500_000,
        },
    ).json()


def _break_gateway(app, message: str = "gateway unreachable") -> None:
    gateway = app.state.cn_gateway

    def boom(_request):
        raise RuntimeError(message)

    gateway.place_order = boom


def test_gateway_failure_records_a_rejected_order(live_client):
    """An unhandled gateway exception used to surface as a 500 with no order row.

    The user could not tell whether the broker had received the order at all, and
    there was no audit trail of the attempt.
    """
    client, app = live_client
    account = _live_account(client)
    _break_gateway(app)

    response = client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "600036.SH",
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
        },
        headers={"X-Live-Confirm": "true"},
    )
    assert response.status_code == 409, response.text
    message = response.json()["error"]["message"]
    assert "网关" in message and "gateway unreachable" in message

    orders = client.get("/api/trading/orders", params={"account_id": account["id"]}).json()
    assert len(orders) == 1
    assert orders[0]["status"] == "rejected"
    assert "gateway error before ack" in orders[0]["reject_reason"]
    assert orders[0]["source"].startswith("live:")


def test_gateway_failure_does_not_charge_the_account(live_client):
    client, app = live_client
    account = _live_account(client)
    before = client.get(f"/api/accounts/{account['id']}").json()["cash_balance"]
    _break_gateway(app)

    client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "600036.SH",
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
        },
        headers={"X-Live-Confirm": "true"},
    )
    after = client.get(f"/api/accounts/{account['id']}").json()["cash_balance"]
    assert after == pytest.approx(before)
    assert client.get("/api/trading/positions", params={"account_id": account["id"]}).json() == []


def test_cancel_failure_leaves_the_order_open(live_client):
    """A failed cancel must not mark the order cancelled.

    The broker may still hold it live; claiming success would leave the user with
    a position they believe is gone.
    """
    client, app = live_client
    account = _live_account(client)

    # Place a real (mock-gateway) order so it has a broker id. The price must sit
    # inside the daily band or the validation layer (correctly) rejects it first.
    snapshot = client.get("/api/market/snapshot", params={"symbol": "600036.SH"}).json()
    limit_price = round(snapshot["limit_down"] + 0.01, 2)
    placed = client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "600036.SH",
            "side": "buy",
            "order_type": "limit",
            "quantity": 100,
            "limit_price": limit_price,
        },
        headers={"X-Live-Confirm": "true"},
    )
    assert placed.status_code == 201, placed.text
    order_id = placed.json()["id"]

    gateway = app.state.cn_gateway

    def boom(*_args, **_kwargs):
        raise RuntimeError("cancel rejected")

    gateway.cancel_order = boom

    response = client.delete(f"/api/trading/orders/{order_id}")
    assert response.status_code == 409, response.text
    assert "cancel rejected" in response.json()["error"]["message"]

    orders = client.get("/api/trading/orders", params={"account_id": account["id"]}).json()
    by_id = {item["id"]: item for item in orders}
    assert by_id[order_id]["status"] != "cancelled"


def test_reconciler_keeps_polling_after_a_status_failure(live_client):
    """One failing status poll must not stop the sweep.

    The reconciler is the only writer of live fills. If a gateway hiccup on one
    order ended the pass, every later order would stall until the next tick, and
    a persistently bad order would stall them forever.
    """
    from app.modules.accounts.service import AccountService
    from app.modules.brokers.base import BrokerOrderResult
    from app.modules.brokers.reconciler import LiveReconciler
    from app.modules.trading.models import Order

    _client, app = live_client
    settings = app.state.settings
    db = Database(settings.database_url)
    with db.session() as session:
        account = AccountService(session, live_trading_enabled=True).create(
            name="对账账户", initial_balance=500_000, market="cn_stock", kind="live"
        )
        for index in range(3):
            session.add(
                Order(
                    id=f"rec-{index}",
                    account_id=account.id,
                    symbol="600036.SH",
                    side="buy",
                    order_type="limit",
                    quantity=100,
                    status="open",
                    broker_order_id=f"b{index}",
                    source="live:test",
                )
            )
        session.commit()

    reconciler = LiveReconciler(db, settings, gateway=app.state.cn_gateway)
    polled: list[str] = []

    def flaky(broker_order_id: str, *, symbol: str = ""):
        polled.append(broker_order_id)
        if broker_order_id == "b1":
            raise RuntimeError("gateway hiccup")
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status="open",
            filled_quantity=0.0,
            avg_price=0.0,
            raw_status="open",
        )

    reconciler._gateway.order_status = flaky
    result = reconciler.tick()
    db.dispose()

    assert polled == ["b0", "b1", "b2"]
    assert result["polled"] == 3


def test_reconcile_reports_a_cash_difference(live_client):
    """A cash drift with matching positions must not read as "all consistent".

    The gateway balance can move without any share changing hands: an external
    transfer, a manual trade in the broker app, or a fee the platform never saw.
    The old response only compared positions, so `all_match` was true while the two
    balances disagreed by the entire initial deposit.
    """
    client, _app = live_client
    account = _live_account(client)

    body = client.get(
        "/api/brokers/ths/reconcile", params={"account_id": account["id"]}
    ).json()

    assert body["positions_match"] is True
    assert body["cash_match"] is False
    assert body["all_match"] is False
    cash_row = body["cash"]
    assert cash_row["currency"] == "CNY"
    assert cash_row["local_cash"] == pytest.approx(500_000)
    # The mock gateway starts with 1,000,000, which is exactly the kind of drift
    # this check exists to surface.
    assert cash_row["gateway_cash"] == pytest.approx(1_000_000)
    assert cash_row["difference"] == pytest.approx(500_000)
