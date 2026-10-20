from __future__ import annotations


def test_account_lifecycle_via_api(client):
    created = client.post(
        "/api/accounts",
        json={"name": "Desk Alpha", "initial_balance": 250000, "leverage": 2},
    )
    assert created.status_code == 201, created.text
    account = created.json()
    assert account["cash_balance"] == 250000
    assert account["status"] == "active"

    listed = client.get("/api/accounts")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    deposit = client.post(f"/api/accounts/{account['id']}/deposit", json={"amount": 5000})
    assert deposit.status_code == 200
    assert deposit.json()["cash_balance"] == 255000

    withdraw = client.post(
        f"/api/accounts/{account['id']}/withdraw", json={"amount": 3000, "note": "test"}
    )
    assert withdraw.status_code == 200
    assert withdraw.json()["cash_balance"] == 252000

    ledger = client.get(f"/api/accounts/{account['id']}/ledger")
    assert ledger.status_code == 200
    assert len(ledger.json()) == 3

    patched = client.patch(f"/api/accounts/{account['id']}", json={"status": "archived"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "archived"


def test_error_contract(client):
    missing = client.get("/api/accounts/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"

    invalid = client.post("/api/accounts", json={"name": "x", "initial_balance": -1})
    assert invalid.status_code == 422

    live = client.post("/api/accounts", json={"name": "live", "kind": "live"})
    assert live.status_code == 403
    assert live.json()["error"]["code"] == "forbidden"


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
