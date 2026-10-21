"""Account risk snapshot shared by the risk centre and the assistant.

The aggregation used to live inside the HTTP router. That made it unreachable
from anything that is not an HTTP request — the assistant could read positions
and orders but could not answer "can I open a position right now?". Moving the
assembly here keeps one definition of "risk overview" for both callers.
"""

from __future__ import annotations

from dataclasses import asdict

from app.modules.accounts.service import AccountService
from app.modules.market.service import MarketService
from app.modules.portfolio.service import PortfolioService
from app.modules.risk.policy import RiskLimits
from app.modules.risk.service import RiskService
from app.modules.runtime.service import RuntimeService
from app.modules.trading.service import TradingService


def build_overview(
    *,
    account_id: str,
    accounts: AccountService,
    market: MarketService,
    portfolio: PortfolioService,
    trading: TradingService,
    runtime: RuntimeService,
    limits: RiskLimits,
    service: RiskService,
) -> dict:
    """One transaction, one consistent set of figures.

    Every number comes from the same session, so the page (or the assistant)
    never shows equity from one instant next to positions from another.
    """
    account = accounts.require(account_id)
    state = service.state(account_id)
    book = portfolio.overview(account_id, record=False)
    positions = trading.positions(account_id)
    reserved = trading.reserved_cash(account_id)

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
    unprotected = [
        f"{item['strategy']}·{item['symbol']}"
        for item in instances
        if item["running"] and not item["protected"]
    ]

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
