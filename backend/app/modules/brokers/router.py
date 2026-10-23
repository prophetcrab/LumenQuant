"""Broker channel status and account reconciliation."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request

from app.api.wiring import build_accounts, build_market
from app.core.errors import ConflictError, ForbiddenError
from app.modules.brokers.binance import build_broker
from app.modules.brokers.ths import build_ths_gateway
from fastapi import Depends
from sqlalchemy.orm import Session
from app.api.deps import get_session


router = APIRouter(prefix="/brokers", tags=["brokers"])


def _ths(request: Request, session: Session):
    settings = request.app.state.settings
    if not settings.live_trading_enabled:
        raise ForbiddenError("live trading is disabled by server configuration")
    path = getattr(request.app.state, "cn_gateway", None)
    if path is not None:
        return path
    market = build_market(request, session)
    gateway = build_ths_gateway(settings, price_feed=market.last_price)
    if gateway is None:
        raise ConflictError(
            "同花顺网关未配置：设置 LQ_THS_GATEWAY_URL（或 LQ_THS_MOCK_ENABLED=true 演练）"
        )
    return gateway


@router.get("/status")
def broker_status(request: Request):
    settings = request.app.state.settings
    binance_ready = bool(settings.binance_api_key and settings.binance_api_secret)
    ths_ready = bool(settings.ths_gateway_url) or settings.ths_mock_enabled
    return {
        "live_trading_enabled": settings.live_trading_enabled,
        "channels": {
            "binance": {
                "market": "crypto",
                "credentials_configured": binance_ready,
                "ready": settings.live_trading_enabled and binance_ready,
            },
            "ths": {
                "market": "cn_stock",
                "gateway_configured": bool(settings.ths_gateway_url),
                "mock_enabled": settings.ths_mock_enabled,
                "ready": settings.live_trading_enabled and ths_ready,
            },
        },
    }


@router.get("/binance/verify")
def verify_binance(request: Request):
    settings = request.app.state.settings
    if not settings.live_trading_enabled:
        raise ForbiddenError("live trading is disabled by server configuration")
    broker = build_broker(settings)
    if broker is None:
        raise ConflictError("binance credentials are not configured")
    return asdict(broker.verify_credentials())


@router.get("/ths/health")
def ths_health(request: Request, session: Session = Depends(get_session)):
    gateway = _ths(request, session)
    account = gateway.verify_credentials()
    return {
        "ok": True,
        "gateway": gateway.name,
        "can_trade": account.can_trade,
        "balances": account.balances,
    }


@router.get("/ths/positions")
def ths_positions(request: Request, session: Session = Depends(get_session)):
    gateway = _ths(request, session)
    return [asdict(p) for p in gateway.positions()]


@router.get("/ths/reconcile")
def ths_reconcile(request: Request, account_id: str, session: Session = Depends(get_session)):
    """Compare gateway state with the platform's ledger.

    Cash is part of the comparison, not just share counts. A live balance can drift
    from the broker's without any position changing: an external transfer, a manual
    trade in the broker app, or a fee the gateway charged and the platform never
    saw. Reporting `all_match: true` while the two balances disagree told the
    operator there was nothing to look at.
    """
    gateway = _ths(request, session)
    accounts = build_accounts(request, session)
    account = accounts.require(account_id)
    from app.api.wiring import build_trading

    local = {p.symbol: p for p in build_trading(request, session).positions(account_id)}
    remote = {p.symbol: p for p in gateway.positions()}
    rows = []
    for symbol in sorted(set(local) | set(remote)):
        local_qty = local[symbol].quantity if symbol in local else 0.0
        remote_qty = remote[symbol].quantity if symbol in remote else 0.0
        rows.append(
            {
                "symbol": symbol,
                "local_quantity": local_qty,
                "gateway_quantity": remote_qty,
                "local_available": local[symbol].available_quantity if symbol in local else 0.0,
                "gateway_available": remote[symbol].available_quantity if symbol in remote else 0.0,
                "match": abs(local_qty - remote_qty) < 1e-6,
            }
        )

    currency = account.base_currency
    local_cash = accounts.cash(account_id)
    cash_note = ""
    try:
        balances = gateway.verify_credentials().balances
    except Exception as exc:  # gateway down: reconciliation is unavailable, not clean
        balances = {}
        cash_note = f"网关余额不可用：{exc}"[:200]
    gateway_cash = balances.get(currency)
    cash_row = {
        "currency": currency,
        "local_cash": round(local_cash, 2),
        "gateway_cash": None if gateway_cash is None else round(float(gateway_cash), 2),
        "difference": None
        if gateway_cash is None
        else round(float(gateway_cash) - local_cash, 2),
        # One cent of tolerance: gateways round to the cent, and a float compare
        # would otherwise flag that rounding as a discrepancy.
        "match": None
        if gateway_cash is None
        else abs(float(gateway_cash) - local_cash) <= 0.01,
        "note": cash_note,
    }
    positions_match = all(row["match"] for row in rows)
    cash_match = cash_row["match"]
    return {
        "account_id": account_id,
        "gateway": gateway.name,
        "positions_match": positions_match,
        "cash_match": cash_match,
        # Unknown cash is not agreement: `all_match` requires both sides compared.
        "all_match": positions_match and cash_match is True,
        "positions": rows,
        "cash": cash_row,
    }
