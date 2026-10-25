"""Broker channel status, health probes and ledger reconciliation."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_accounts, build_market, build_trading
from app.core.errors import ConflictError, ForbiddenError
from app.modules.brokers.binance import build_broker
from app.modules.brokers.ths import build_ths_gateway

router = APIRouter(prefix="/brokers", tags=["brokers"])


def _ths(request: Request, session: Session):
    settings = request.app.state.settings
    if not settings.live_trading_enabled:
        raise ForbiddenError("live trading is disabled by server configuration")
    gateway = getattr(request.app.state, "cn_gateway", None)
    if gateway is not None:
        return gateway
    market = build_market(request, session)
    gateway = build_ths_gateway(settings, price_feed=market.last_price)
    if gateway is None:
        raise ConflictError(
            "同花顺网关未配置：设置 LQ_THS_GATEWAY_URL（或 LQ_THS_MOCK_ENABLED=true 演练）"
        )
    return gateway


def _binance(request: Request):
    settings = request.app.state.settings
    if not settings.live_trading_enabled:
        raise ForbiddenError("live trading is disabled by server configuration")
    broker = getattr(request.app.state, "crypto_broker", None) or build_broker(settings)
    if broker is None:
        raise ConflictError(
            "binance 实盘未配置：设置 LQ_BINANCE_API_KEY / LQ_BINANCE_API_SECRET"
        )
    return broker


@router.get("/status")
def broker_status(request: Request):
    settings = request.app.state.settings
    binance_ready = bool(settings.binance_api_key and settings.binance_api_secret)
    ths_ready = bool(settings.ths_gateway_url) or settings.ths_mock_enabled
    reconciler = getattr(request.app.state, "reconcile_engine", None)
    return {
        "live_trading_enabled": settings.live_trading_enabled,
        "live_auto_trading_enabled": settings.live_auto_trading_enabled,
        # Polling is what actually moves a live order to `filled`; reported
        # separately because "credentials configured" alone does not mean fills
        # are arriving.
        "reconciler_running": reconciler is not None and reconciler._task is not None,
        "reconcile_seconds": getattr(settings, "live_reconcile_seconds", 3.0),
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
    broker = _binance(request)
    account = broker.verify_credentials()
    return asdict(account)


@router.get("/binance/positions")
def binance_positions(request: Request):
    """Balances held at the exchange, as the broker sees them.

    Spot has no short positions, and an asset is the position: this is the
    remote side of the reconciliation comparison.
    """
    broker = _binance(request)
    return broker.balances()


@router.post("/binance/align-cash")
def binance_align_cash(
    request: Request,
    account_id: str,
    confirm: bool = False,
    session: Session = Depends(get_session),
):
    """Point the local cash book at the balance the exchange actually reports.

    A live account starts from a locally configured `initial_balance`, which is
    not the amount available for trading. Sizing is computed from the local book,
    so until the two agree every order is sized against a fictional number.

    `confirm=false` returns a preview and changes nothing: balancing an account
    is a funding event and must never happen as a side effect of opening a page.
    The confirmation also requires the same `X-Live-Confirm` header as a live
    order. Without it this was a plain form-submittable request that rewrote the
    account's cash basis — no CORS preflight, no header, one click.
    """
    broker = _binance(request)
    accounts = build_accounts(request, session)
    account = accounts.require(account_id)
    if account.market != "crypto":
        raise ConflictError("该账户不是加密账户，请使用 A 股网关对账")
    if account.kind != "live":
        raise ConflictError("只有实盘账户需要对账券商余额")
    if account.broker and account.broker != broker.name:
        raise ConflictError(f"该账户绑定 {account.broker}，与币安通道不匹配")
    if confirm and request.headers.get("X-Live-Confirm", "").lower() != "true":
        raise ForbiddenError("对齐余额会改写资金基准，需携带 X-Live-Confirm: true")

    balances = broker.balances()
    currency = (account.base_currency or "USDT").upper()
    if currency not in balances:
        # "No USDT in the balance list" and "zero USDT" are not the same thing:
        # `balances()` omits zero positions, so an all-in-BTC account would look
        # like zero cash here and this call would wipe the local book — while the
        # platform still has no record of the BTC that replaced it.
        raise ConflictError(
            f"交易所未报告 {currency} 余额（可能已全部换成其他资产），拒绝将本地现金清零；"
            "请先对齐持仓或人工核对"
        )
    broker_cash = float(balances[currency])
    local_cash = accounts.cash(account_id)
    # Reserved cash is what resting live orders have already committed; showing
    # it makes the preview answer "how much can I actually trade afterwards".
    reserved = build_trading(request, session, accounts=accounts).reserved_cash(account_id)
    preview = {
        "account_id": account_id,
        "currency": currency,
        "local_cash": round(local_cash, 8),
        "broker_cash": round(broker_cash, 8),
        "difference": round(broker_cash - local_cash, 8),
        "reserved_cash": round(reserved, 8),
        "applied": False,
    }
    if not confirm:
        return preview
    updated = accounts.align_to_broker_cash(
        account_id, broker_cash, note="对齐币安账户余额（POST /brokers/binance/align-cash）"
    )
    preview["applied"] = True
    preview["cash_after"] = updated.cash_balance
    return preview


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


@router.get("/binance/reconcile")
def binance_reconcile(
    request: Request, account_id: str, session: Session = Depends(get_session)
):
    """Compare the platform's crypto book with the exchange's balances.

    Spot holds assets, not "positions with a symbol", so the comparison is per
    asset: the local quantity for every symbol whose base asset belongs to the
    account is summed against the exchange's total balance. Differences almost
    always mean a manual trade in the Binance app or a deposit/withdrawal the
    platform never saw.
    """
    broker = _binance(request)
    accounts = build_accounts(request, session)
    account = accounts.require(account_id)
    market = build_market(request, session)
    trading = build_trading(request, session, accounts=accounts, market=market)

    # Local quantity per base asset.
    local_assets: dict[str, float] = {}
    for position in trading.positions(account_id):
        try:
            view = market.require_symbol(position.symbol)
        except Exception:
            continue
        base = (view.base or view.code).upper()
        local_assets[base] = local_assets.get(base, 0.0) + position.quantity

    note = ""
    all_balances: dict[str, float] = {}
    try:
        all_balances = broker.balances()
        remote_assets = all_balances
    except Exception as exc:  # exchange down: unavailable, not clean
        remote_assets = {}
        note = f"交易所余额不可用：{exc}"[:200]

    currency = account.base_currency.upper() if account.base_currency else ""
    # The quote currency is cash, compared on its own line below. Including it
    # here compared "local 0" (positions never hold cash) against the exchange's
    # whole balance, so `all_match` was false for every correctly balanced
    # account.
    if currency:
        local_assets.pop(currency, None)
        remote_assets = {
            asset: qty for asset, qty in remote_assets.items() if asset != currency
        }

    rows = []
    for asset in sorted(set(local_assets) | set(remote_assets)):
        local_qty = local_assets.get(asset, 0.0)
        remote_qty = remote_assets.get(asset, 0.0)
        # Dust below one satoshi is rounding, not a discrepancy.
        match = abs(local_qty - remote_qty) <= 1e-8
        rows.append(
            {
                "asset": asset,
                "local_quantity": local_qty,
                "exchange_quantity": remote_qty,
                "difference": round(remote_qty - local_qty, 8),
                "match": match,
            }
        )

    local_cash = accounts.cash(account_id)
    exchange_cash = all_balances.get(currency) if currency else None
    cash_row = {
        "currency": currency,
        "local_cash": round(local_cash, 8),
        "exchange_cash": None if exchange_cash is None else round(float(exchange_cash), 8),
        "difference": None
        if exchange_cash is None
        else round(float(exchange_cash) - local_cash, 8),
        "match": None
        if exchange_cash is None
        else abs(float(exchange_cash) - local_cash) <= 0.01,
        "note": note,
    }
    assets_match = all(row["match"] for row in rows)
    return {
        "account_id": account_id,
        "gateway": broker.name,
        "assets_match": assets_match,
        "cash_match": cash_row["match"],
        "all_match": assets_match and cash_row["match"] is True,
        "assets": rows,
        "cash": cash_row,
    }
