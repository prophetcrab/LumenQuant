"""Binance spot REST adapter.

Credentials come from settings/env only and are never persisted. All calls are
signed with HMAC-SHA256 over `timestamp&query` as Binance requires (v3 API).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx

from app.core.errors import AppError, ConflictError, ForbiddenError
from app.modules.brokers.base import (
    BrokerAccount,
    BrokerOrderRequest,
    BrokerOrderResult,
)

STATUS_MAP = {
    "NEW": "open",
    "PARTIALLY_FILLED": "partially_filled",
    "FILLED": "filled",
    "CANCELED": "cancelled",
    "REJECTED": "rejected",
    "EXPIRED": "cancelled",
}


class BrokerError(AppError):
    status_code = 502
    code = "broker_error"


class BinanceBroker:
    name = "binance"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.binance.com",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not api_secret:
            raise ForbiddenError(
                "live broker credentials are not configured (set LQ_BINANCE_API_KEY / LQ_BINANCE_API_SECRET)"
            )
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    # ------------------------------------------------------------- internals
    def _sign(self, query: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _request(
        self, method: str, path: str, params: dict | None = None, *, signed: bool = True
    ) -> dict:
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.api_key}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            query = urlencode(params, doseq=True)
            query += f"&signature={self._sign(query)}"
            url = f"{self.base_url}{path}?{query}"
            body = None
        else:
            url = f"{self.base_url}{path}"
            body = params or None

        with httpx.Client(
            timeout=self.timeout, transport=self._transport
        ) as client:
            response = client.request(method, url, headers=headers, data=body)
        if response.status_code >= 400:
            try:
                detail = response.json()
                message = f"{detail.get('code')}: {detail.get('msg')}"
            except Exception:
                message = response.text[:300]
            if response.status_code in (401, 403):
                raise BrokerError(f"binance rejected credentials ({message})")
            raise BrokerError(f"binance error {response.status_code} ({message})")
        return response.json()

    # ------------------------------------------------------------------ port
    def verify_credentials(self) -> BrokerAccount:
        data = self._request("GET", "/api/v3/account")
        if data.get("canTrade") is False:
            raise ForbiddenError("binance account is not allowed to trade")
        balances = {
            item["asset"]: float(item["free"])
            for item in data.get("balances", [])
            if float(item.get("free", 0)) > 0
        }
        return BrokerAccount(can_trade=bool(data.get("canTrade")), balances=balances)

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        params = {
            "symbol": request.symbol,
            "side": request.side.upper(),
            "type": request.order_type.upper(),
            "quantity": f"{request.quantity:.8f}".rstrip("0").rstrip("."),
        }
        if request.order_type == "limit":
            if request.limit_price is None:
                raise ConflictError("limit order requires a price")
            params["price"] = f"{request.limit_price:.8f}".rstrip("0").rstrip(".")
            params["timeInForce"] = "GTC"
        if request.client_order_id:
            params["newClientOrderId"] = request.client_order_id

        data = self._request("POST", "/api/v3/order", params)
        executed = float(data.get("executedQty") or 0)
        quote = float(data.get("cummulativeQuoteQty") or 0)
        avg = quote / executed if executed else 0.0
        raw_status = str(data.get("status", "NEW")).upper()
        return BrokerOrderResult(
            broker_order_id=str(data.get("orderId", "")),
            status=STATUS_MAP.get(raw_status, "open"),
            filled_quantity=executed,
            avg_price=avg,
            raw_status=raw_status,
        )

    def cancel_order(self, symbol: str, broker_order_id: str) -> None:
        self._request("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": broker_order_id})

    def open_orders(self, symbol: str) -> list[dict]:
        data = self._request("GET", "/api/v3/openOrders", {"symbol": symbol})
        return [
            {
                "id": str(item.get("orderId")),
                "symbol": item.get("symbol"),
                "side": str(item.get("side", "")).lower(),
                "type": str(item.get("type", "")).lower(),
                "quantity": float(item.get("origQty") or 0),
                "price": float(item.get("price") or 0),
                "status": STATUS_MAP.get(str(item.get("status", "")).upper(), "open"),
            }
            for item in data
        ]


def build_broker(settings) -> BinanceBroker | None:
    """Factory used by the composition root; returns None when disabled."""
    if not settings.live_trading_enabled:
        return None
    if not settings.binance_api_key or not settings.binance_api_secret:
        return None
    return BinanceBroker(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        base_url=settings.binance_base_url,
        timeout=settings.request_timeout_seconds,
    )
