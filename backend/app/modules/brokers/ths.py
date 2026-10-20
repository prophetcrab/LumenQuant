"""Tonghuashun (同花顺) order gateway adapter.

Important boundary: this platform talks to an **authorized order gateway** over
HTTP. It does not automate the Tonghuashun desktop client, inject keystrokes or
hooks into its process — that would violate the vendor's terms and is unsafe.

Expected gateway contract (JSON over HTTP):

    GET  /api/health                        -> {"ok": true, "account": "..."}
    GET  /api/account                       -> {"account_id","cash","buying_power","positions":[...]}
    POST /api/orders                        -> {"order_id","status"}
    GET  /api/orders/{order_id}             -> {"order_id","status","filled_quantity",
                                                "avg_price","fee","reject_reason"}
    DELETE /api/orders/{order_id}           -> {"ok": true}
    GET  /api/orders?status=open            -> {"orders":[{...}]}
    GET  /api/trades?since=<ms>             -> {"trades":[{...}]}

When no gateway URL is configured, the platform uses `MockThsGateway`, which
implements the same contract in-process so paper/live code paths are identical.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.core.errors import AppError, ConflictError, ForbiddenError
from app.modules.brokers.base import (
    BrokerAccount,
    BrokerOrderRequest,
    BrokerOrderResult,
)

logger = logging.getLogger(__name__)

THS_STATUS_MAP = {
    "pending": "new",
    "accepted": "open",
    "partial": "partially_filled",
    "filled": "filled",
    "cancelled": "cancelled",
    "rejected": "rejected",
}

FINAL_STATUSES = {"filled", "cancelled", "rejected"}


class ThsGatewayError(AppError):
    status_code = 502
    code = "ths_gateway_error"


@dataclass(frozen=True)
class ThsPosition:
    symbol: str
    quantity: float
    available_quantity: float
    avg_price: float


class ThsGatewayPort:
    name = "ths"

    def verify_credentials(self) -> BrokerAccount: ...
    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult: ...
    def cancel_order(self, symbol: str, broker_order_id: str) -> None: ...
    def open_orders(self, symbol: str) -> list[dict]: ...
    def order_status(self, broker_order_id: str) -> BrokerOrderResult: ...
    def positions(self) -> list[ThsPosition]: ...
    def trades_since(self, since_ms: int) -> list[dict]: ...


class ThsHttpGateway:
    """Adapter for a local/broker-side authorized THS order gateway."""

    name = "ths"

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ForbiddenError(
                "同花顺网关未配置：请设置 LQ_THS_GATEWAY_URL 指向已授权的下单网关"
            )
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._transport = transport

    def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                response = client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ThsGatewayError(f"同花顺网关不可达：{exc}") from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = payload.get("message") or payload.get("error") or response.text[:200]
            except Exception:
                message = response.text[:200]
            raise ThsGatewayError(f"同花顺网关返回 {response.status_code}：{message}")
        try:
            return response.json()
        except ValueError as exc:
            raise ThsGatewayError("同花顺网关返回了非 JSON 响应") from exc

    def verify_credentials(self) -> BrokerAccount:
        health = self._request("GET", "/api/health")
        if not health.get("ok"):
            raise ForbiddenError("同花顺网关未就绪")
        account = self._request("GET", "/api/account")
        balances = {"CNY": float(account.get("cash", 0.0))}
        return BrokerAccount(can_trade=True, balances=balances)

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        payload = {
            "symbol": request.symbol,
            "side": request.side,
            "order_type": request.order_type,
            "quantity": request.quantity,
        }
        if request.limit_price is not None:
            payload["limit_price"] = request.limit_price
        if request.client_order_id:
            payload["client_order_id"] = request.client_order_id
        data = self._request("POST", "/api/orders", json=payload)
        status = THS_STATUS_MAP.get(str(data.get("status", "accepted")).lower(), "open")
        return BrokerOrderResult(
            broker_order_id=str(data.get("order_id", "")),
            status=status,
            filled_quantity=float(data.get("filled_quantity", 0.0)),
            avg_price=float(data.get("avg_price", 0.0)),
            raw_status=str(data.get("status", "")),
        )

    def cancel_order(self, symbol: str, broker_order_id: str) -> None:
        self._request("DELETE", f"/api/orders/{broker_order_id}")

    def open_orders(self, symbol: str) -> list[dict]:
        data = self._request("GET", "/api/orders", params={"status": "open"})
        return list(data.get("orders", []))

    def order_status(self, broker_order_id: str) -> BrokerOrderResult:
        data = self._request("GET", f"/api/orders/{broker_order_id}")
        raw = str(data.get("status", "accepted"))
        return BrokerOrderResult(
            broker_order_id=str(data.get("order_id", broker_order_id)),
            status=THS_STATUS_MAP.get(raw.lower(), "open"),
            filled_quantity=float(data.get("filled_quantity", 0.0)),
            avg_price=float(data.get("avg_price", 0.0)),
            raw_status=data.get("reject_reason") or raw,
        )

    def positions(self) -> list[ThsPosition]:
        data = self._request("GET", "/api/account")
        return [
            ThsPosition(
                symbol=str(item["symbol"]),
                quantity=float(item.get("quantity", 0.0)),
                available_quantity=float(item.get("available_quantity", item.get("quantity", 0.0))),
                avg_price=float(item.get("avg_price", 0.0)),
            )
            for item in data.get("positions", [])
        ]

    def trades_since(self, since_ms: int) -> list[dict]:
        data = self._request("GET", "/api/trades", params={"since": int(since_ms)})
        return list(data.get("trades", []))


class MockThsGateway:
    """In-process gateway used when no real gateway is configured.

    It accepts orders, fills market orders immediately at a supplied reference
    price and leaves limit orders open until `advance()` is called. This exists
    so the live-order code path (submit -> poll -> reconcile) can be tested
    end to end without a broker.
    """

    name = "ths-mock"

    def __init__(self, price_feed=None) -> None:
        self._orders: dict[str, dict] = {}
        self._trades: list[dict] = []
        self._price_feed = price_feed
        self._positions: dict[str, ThsPosition] = {}

    # ---------------------------------------------------------- test helpers
    def set_position(self, symbol: str, quantity: float, avg_price: float, available: float | None = None) -> None:
        self._positions[symbol] = ThsPosition(
            symbol=symbol,
            quantity=quantity,
            available_quantity=quantity if available is None else available,
            avg_price=avg_price,
        )

    def fill_order(self, broker_order_id: str, quantity: float, price: float) -> None:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise KeyError(broker_order_id)
        order["filled_quantity"] = quantity
        order["avg_price"] = price
        order["status"] = "filled"
        self._trades.append(
            {
                "trade_id": uuid.uuid4().hex[:12],
                "order_id": broker_order_id,
                "symbol": order["symbol"],
                "side": order["side"],
                "quantity": quantity,
                "price": price,
                "fee": 0.0,
                "time": int(datetime.now().timestamp() * 1000),
            }
        )
        self._apply_position(order["symbol"], order["side"], quantity, price)

    def _apply_position(self, symbol: str, side: str, quantity: float, price: float) -> None:
        current = self._positions.get(symbol)
        if side == "buy":
            if current is None:
                self._positions[symbol] = ThsPosition(symbol, quantity, 0.0, price)
            else:
                total = current.quantity + quantity
                avg = (current.quantity * current.avg_price + quantity * price) / total
                self._positions[symbol] = ThsPosition(symbol, total, current.available_quantity, avg)
        elif current is not None:
            remaining = max(0.0, current.quantity - quantity)
            self._positions[symbol] = ThsPosition(
                symbol, remaining, max(0.0, current.available_quantity - quantity), current.avg_price
            )

    def _reference_price(self, symbol: str) -> float:
        if self._price_feed is None:
            return 0.0
        try:
            return float(self._price_feed(symbol))
        except Exception:
            return 0.0

    # ------------------------------------------------------------------ port
    def verify_credentials(self) -> BrokerAccount:
        cash = 1_000_000.0
        return BrokerAccount(can_trade=True, balances={"CNY": cash})

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        broker_id = f"MOCK-{uuid.uuid4().hex[:10]}"
        if request.order_type == "market":
            price = self._reference_price(request.symbol) or 0.0
            status = "filled" if price > 0 else "accepted"
        else:
            price = 0.0
            status = "accepted"
        self._orders[broker_id] = {
            "order_id": broker_id,
            "symbol": request.symbol,
            "side": request.side,
            "order_type": request.order_type,
            "quantity": request.quantity,
            "limit_price": request.limit_price,
            "filled_quantity": 0.0,
            "avg_price": 0.0,
            "status": status,
        }
        if status == "filled":
            self.fill_order(broker_id, request.quantity, price)
        return BrokerOrderResult(
            broker_order_id=broker_id,
            status="filled" if status == "filled" else "open",
            filled_quantity=request.quantity if status == "filled" else 0.0,
            avg_price=price if status == "filled" else 0.0,
            raw_status=status,
        )

    def cancel_order(self, symbol: str, broker_order_id: str) -> None:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise ConflictError("gateway order not found")
        if order["status"] in FINAL_STATUSES:
            raise ConflictError(f"order is {order['status']}")
        order["status"] = "cancelled"

    def open_orders(self, symbol: str) -> list[dict]:
        return [
            dict(order)
            for order in self._orders.values()
            if order["symbol"] == symbol and order["status"] not in FINAL_STATUSES
        ]

    def order_status(self, broker_order_id: str) -> BrokerOrderResult:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise ConflictError("gateway order not found")
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=THS_STATUS_MAP.get(order["status"], "open"),
            filled_quantity=order["filled_quantity"],
            avg_price=order["avg_price"],
            raw_status=order["status"],
        )

    def positions(self) -> list[ThsPosition]:
        return list(self._positions.values())

    def trades_since(self, since_ms: int) -> list[dict]:
        return [t for t in self._trades if t["time"] >= since_ms]


def build_ths_gateway(settings, price_feed=None) -> ThsGatewayPort | None:
    if settings.ths_gateway_url:
        return ThsHttpGateway(
            base_url=settings.ths_gateway_url,
            token=settings.ths_gateway_token,
            timeout=settings.request_timeout_seconds,
        )
    if settings.ths_mock_enabled:
        return MockThsGateway(price_feed=price_feed)
    return None
