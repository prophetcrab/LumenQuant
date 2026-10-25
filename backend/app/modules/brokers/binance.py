"""Binance spot REST adapter.

Credentials come from settings/env only and are never persisted. All calls are
signed with HMAC-SHA256 over `timestamp&query` as Binance requires (v3 API).

Two things here exist because the exchange, not this platform, decides them:

* **Quantity precision** — every symbol has a `LOT_SIZE` step, and a quantity
  that is not a multiple of it is rejected outright (`-1111`). `normalize_quantity`
  floors to the step before the order row is written, so the amount stored is the
  amount submitted and the order can still reach `filled`.
* **Order state** — a limit order usually rests after submission. `order_status`
  lets the reconciler advance it; without it the local row stayed `open` forever
  and the cash reserved for it was never released.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from decimal import Decimal
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
    "PENDING_CANCEL": "open",
    "REJECTED": "rejected",
    "EXPIRED": "cancelled",
    "EXPIRED_IN_MATCH": "cancelled",
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
        self._http: httpx.Client | None = None
        # Symbol filters are immutable for the life of a process in practice.
        self._rules: dict[str, dict] = {}

    # ------------------------------------------------------------- internals
    def _client(self) -> httpx.Client:
        """One pooled client per process.

        The reconciler polls every few seconds; a fresh client per call meant a
        TLS handshake per poll, which is pure latency in the live path.
        """
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout, transport=self._transport)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

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
            response = self._client().request(method, url, headers=headers)
        else:
            # Unsigned calls are GETs whose parameters belong in the query
            # string. Sending them as a request body meant the exchange ignored
            # them entirely: `/exchangeInfo` answered with every symbol, and
            # callers that took the first entry read another pair's lot step —
            # then submitted a quantity Binance rejects.
            response = self._client().request(
                method, f"{self.base_url}{path}", headers=headers, params=params or None
            )
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

    # ---------------------------------------------------------- filter rules
    def _symbol_rules(self, symbol: str) -> dict:
        key = symbol.upper()
        cached = self._rules.get(key)
        if cached is not None:
            return cached
        data = self._request("GET", "/api/v3/exchangeInfo", {"symbol": key}, signed=False)
        step = min_qty = min_notional = 0.0
        # Match by symbol rather than trusting the filter. A server that ignores
        # the query parameter answers with the whole catalog, and taking the
        # first entry silently applies another pair's lot step.
        entries = [
            item
            for item in data.get("symbols", [])
            if str(item.get("symbol", "")).upper() == key
        ]
        for entry in entries:
            for item in entry.get("filters", []):
                kind = item.get("filterType")
                if kind == "LOT_SIZE":
                    step = float(item.get("stepSize") or 0)
                    min_qty = float(item.get("minQty") or 0)
                elif kind in {"NOTIONAL", "MIN_NOTIONAL"}:
                    # Spot changed NOTIONAL -> MIN_NOTIONAL; accept either name.
                    min_notional = float(item.get("minNotional") or 0)
        rules = {"step": step, "min_qty": min_qty, "min_notional": min_notional}
        self._rules[key] = rules
        return rules

    def normalize_quantity(self, symbol: str, quantity: float) -> float:
        """Floor a quantity to the symbol's lot step.

        A metadata call must never block a live order, so any failure falls back
        to the caller's value and lets the exchange decide.
        """
        try:
            rules = self._symbol_rules(symbol)
        except Exception:
            return quantity
        step = rules["step"]
        if step <= 0 or quantity <= 0:
            return quantity
        steps = math.floor(quantity / step + 1e-9)
        return round(steps * step, 8)

    def _format_quantity(self, symbol: str, quantity: float) -> str:
        step = 0.0
        try:
            step = self._symbol_rules(symbol)["step"]
        except Exception:
            step = 0.0
        if step > 0:
            # Render at the step's own precision: 0.00001 -> 5 decimals, not 8.
            places = max(0, -Decimal(str(step)).as_tuple().exponent)
        else:
            places = 8
        text = f"{quantity:.{places}f}"
        return text.rstrip("0").rstrip(".") or "0"

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

    def balances(self) -> dict[str, float]:
        """Free and locked balances keyed by asset.

        `verify_credentials` reports free balances only, which is what a human
        wants to see. Reconciliation needs the total: an asset locked by a
        resting order is still owned and must not read as a missing position.
        """
        data = self._request("GET", "/api/v3/account")
        totals: dict[str, float] = {}
        for item in data.get("balances", []):
            total = float(item.get("free", 0)) + float(item.get("locked", 0))
            if total > 0:
                totals[item["asset"]] = total
        return totals

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        params = {
            "symbol": request.symbol,
            "side": request.side.upper(),
            "type": request.order_type.upper(),
            "quantity": self._format_quantity(request.symbol, request.quantity),
        }
        if request.order_type == "limit":
            if request.limit_price is None:
                raise ConflictError("limit order requires a price")
            params["price"] = f"{request.limit_price:.8f}".rstrip("0").rstrip(".")
            params["timeInForce"] = "GTC"
        if request.client_order_id:
            params["newClientOrderId"] = request.client_order_id

        data = self._request("POST", "/api/v3/order", params)
        return self._parse_order(data)

    def cancel_order(self, symbol: str, broker_order_id: str) -> None:
        self._request("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": broker_order_id})

    def order_status(
        self, broker_order_id: str, *, symbol: str = ""
    ) -> BrokerOrderResult:
        """Current state of one order.

        Binance scopes the query by symbol, so a caller that has the local order
        row must pass it. Omitting it is an error rather than a silent lookup
        that would always fail.
        """
        if not symbol:
            raise ConflictError("binance order status requires the symbol")
        data = self._request(
            "GET", "/api/v3/order", {"symbol": symbol, "orderId": broker_order_id}
        )
        return self._parse_order(data)

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

    def _parse_order(self, data: dict) -> BrokerOrderResult:
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
            quote_quantity=quote if executed else None,
        )


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
