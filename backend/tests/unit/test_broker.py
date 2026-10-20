from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.errors import AppError
from app.modules.brokers.base import BrokerOrderRequest
from app.modules.brokers.binance import BinanceBroker, BrokerError


def make_broker(handler):
    transport = httpx.MockTransport(handler)
    return BinanceBroker(
        api_key="key-123",
        api_secret="secret-456",
        base_url="https://api.binance.test",
        transport=transport,
    )


def test_place_market_order_parses_fill():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("X-MBX-APIKEY")
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "orderId": 998877,
                "status": "FILLED",
                "executedQty": "0.50000000",
                "cummulativeQuoteQty": "15000.00",
            },
        )

    broker = make_broker(handler)
    result = broker.place_order(
        BrokerOrderRequest(symbol="BTCUSDT", side="buy", order_type="market", quantity=0.5)
    )
    assert result.broker_order_id == "998877"
    assert result.status == "filled"
    assert result.filled_quantity == 0.5
    assert result.avg_price == 30000.0

    query = parse_qs(urlparse(captured["url"]).query)
    assert captured["key"] == "key-123"
    assert query["symbol"] == ["BTCUSDT"]
    assert query["side"] == ["BUY"]
    assert query["type"] == ["MARKET"]
    assert query["quantity"] == ["0.5"]
    assert query["signature"]


def test_signature_matches_hmac():
    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        signature = query.pop("signature")[0]
        rebuilt = "&".join(f"{k}={v[0]}" for k, v in query.items())
        expected = hmac.new(b"secret-456", rebuilt.encode(), hashlib.sha256).hexdigest()
        assert signature == expected
        return httpx.Response(200, json={"canTrade": True, "balances": []})

    broker = make_broker(handler)
    account = broker.verify_credentials()
    assert account.can_trade is True


def test_limit_order_requires_price():
    broker = make_broker(lambda request: httpx.Response(200, json={}))
    with pytest.raises(AppError):
        broker.place_order(
            BrokerOrderRequest(symbol="BTCUSDT", side="buy", order_type="limit", quantity=1)
        )


def test_auth_error_is_mapped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key"})

    broker = make_broker(handler)
    with pytest.raises(BrokerError) as exc:
        broker.verify_credentials()
    assert "credentials" in str(exc.value)


def test_invalid_credentials_rejected_at_construction():
    with pytest.raises(AppError):
        BinanceBroker(api_key="", api_secret="")
