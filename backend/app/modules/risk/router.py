from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.modules.risk.policy import RiskContext, RiskLimits, RiskPolicy
from app.modules.alerts.service import AlertService
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
    service = RiskService(session, alerts=AlertService(session))
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
    """Everything the risk centre needs in one call."""
    from app.api.wiring import build_accounts, build_market, build_portfolio, build_trading
    from app.modules.risk.overview import build_overview
    from app.modules.runtime.service import RuntimeService

    accounts = build_accounts(request, session)
    market = build_market(request, session)
    portfolio = build_portfolio(request, session, accounts=accounts)
    trading = build_trading(request, session, accounts=accounts, market=market)
    runtime = RuntimeService(
        session, accounts_service=accounts, market=market, trading=trading
    )
    return build_overview(
        account_id=account_id,
        accounts=accounts,
        market=market,
        portfolio=portfolio,
        trading=trading,
        runtime=runtime,
        limits=_limits(request),
        service=RiskService(session, alerts=AlertService(session)),
    )
