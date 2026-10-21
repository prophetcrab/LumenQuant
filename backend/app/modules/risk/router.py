from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.modules.risk.policy import RiskContext, RiskLimits, RiskPolicy
from app.modules.risk.service import RiskService


router = APIRouter(prefix="/risk", tags=["risk"])


def _limits(request: Request) -> RiskLimits:
    settings = request.app.state.settings
    return RiskLimits(
        max_order_notional=settings.risk_max_order_notional,
        max_position_notional=settings.risk_max_position_notional,
        min_order_notional=settings.risk_min_order_notional,
        max_orders_per_minute=settings.risk_max_orders_per_minute,
        max_daily_loss_pct=settings.risk_max_daily_loss_pct,
        max_drawdown_pct=settings.risk_max_drawdown_pct,
    )


@router.get("/limits")
def limits(request: Request):
    return asdict(_limits(request))


@router.get("/state")
def state(account_id: str, session: Session = Depends(get_session)):
    return asdict(RiskService(session).state(account_id))


@router.post("/observe")
def observe(
    account_id: str,
    equity: float,
    request: Request,
    session: Session = Depends(get_session),
):
    """Feed an equity mark into the breakers (called by the portfolio loop)."""
    service = RiskService(session)
    service.observe(account_id, equity)
    view = service.state(account_id)
    decision = RiskPolicy(_limits(request)).breaker(
        RiskContext(
            cash=0.0,
            position_qty=0.0,
            position_avg_price=0.0,
            orders_last_minute=0,
            equity=equity,
            day_start_equity=view.day_start_equity,
            peak_equity=view.peak_equity,
        )
    )
    if not decision.allowed:
        service.trip(account_id, decision.reason)
    return asdict(service.state(account_id))


@router.post("/reset")
def reset(account_id: str, session: Session = Depends(get_session)):
    return asdict(RiskService(session).reset(account_id))


@router.get("/overview")
def overview(
    request: Request,
    account_id: str,
    session: Session = Depends(get_session),
):
    """Everything the risk centre needs in one call.

    Aggregating server-side keeps the page consistent (all figures from one
    transaction) and avoids five parallel requests on every refresh.
    """
    from app.api.wiring import build_accounts, build_market, build_portfolio, build_trading

    accounts = build_accounts(request, session)
    account = accounts.require(account_id)
    market = build_market(request, session)
    portfolio = build_portfolio(request, session, accounts=accounts)
    trading = build_trading(request, session, accounts=accounts, market=market)
    service = RiskService(session)

    state = service.state(account_id)
    limits = _limits(request)
    book = portfolio.overview(account_id, record=False)
    positions = trading.positions(account_id)
    reserved = trading.reserved_cash(account_id)

    # Per-symbol share locks from resting sells.
    from app.modules.runtime.service import RuntimeService

    runtime = RuntimeService(
        session, accounts_service=accounts, market=market, trading=trading
    )
    instances = [
        {
            "id": item.id,
            "strategy": item.strategy_name,
            "symbol": item.symbol,
            "running": item.running,
            "stop_loss_pct": item.stop_loss_pct,
            "take_profit_pct": item.take_profit_pct,
            "trailing_stop_pct": item.trailing_stop_pct,
            "peak_price": item.peak_price,
            "executions": item.executions,
            "last_error": item.last_error,
            "protected": bool(
                item.stop_loss_pct or item.take_profit_pct or item.trailing_stop_pct
            ),
        }
        for item in runtime.list(account_id=account_id)
    ]

    unprotected = [item["strategy"] + "·" + item["symbol"] for item in instances if item["running"] and not item["protected"]]

    return {
        "account": {
            "id": account.id,
            "name": account.name,
            "market": account.market,
            "base_currency": account.base_currency,
            "kind": account.kind,
        },
        "state": asdict(state),
        "limits": asdict(limits),
        "exposure": {
            "equity": book.equity,
            "cash": book.cash,
            "available_cash": book.available_cash,
            "reserved_cash": reserved,
            "positions_value": book.positions_value,
            "exposure_pct": book.exposure_pct,
            "position_count": book.position_count,
            "largest_position_pct": max(
                (item.weight_pct for item in book.allocations), default=0.0
            ),
        },
        "positions": [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "available_quantity": p.available_quantity,
                "avg_price": p.avg_price,
                "market_price": p.market_price,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
                "unrealized_pct": round(
                    (p.unrealized_pnl / (p.avg_price * p.quantity) * 100)
                    if p.avg_price and p.quantity
                    else 0.0,
                    4,
                ),
            }
            for p in positions
        ],
        "instances": instances,
        "warnings": [
            f"自动运行实例未设置任何止损保护：{name}" for name in unprotected
        ],
    }
