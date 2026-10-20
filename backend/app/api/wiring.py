"""Single composition root for module wiring.

Routers ask for fully-wired services here instead of assembling dependencies
themselves. This keeps cross-module wiring in one place (the only module allowed
to know about all modules) while business modules stay unaware of each other.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.accounts.service import AccountService
from app.modules.backtest.service import BacktestService
from app.modules.brokers.binance import build_broker
from app.modules.brokers.ths import build_ths_gateway
from app.modules.cn.fees import CnFeeSchedule
from app.modules.market.service import MarketService
from app.modules.portfolio.service import PortfolioService
from app.modules.risk.policy import RiskLimits, RiskPolicy
from app.modules.risk.service import RiskService
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.service import TradingService


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


def live_market(request: Request):
    return getattr(request.app.state, "live_market", None)


def build_accounts(request: Request, session: Session) -> AccountService:
    settings = settings_of(request)
    return AccountService(session, live_trading_enabled=settings.live_trading_enabled)


def build_market(
    request: Request, session: Session, *, live: bool = True
) -> MarketService:
    engine = live_market(request) if live else None
    return MarketService(
        session,
        clock=getattr(engine, "clock", None),
        live=engine,
    )


def cn_fee_schedule(settings: Settings) -> CnFeeSchedule:
    return CnFeeSchedule(
        commission_bps=settings.cn_commission_bps,
        min_commission=settings.cn_min_commission,
        stamp_duty_bps=settings.cn_stamp_duty_bps,
        transfer_fee_bps=settings.cn_transfer_fee_bps,
    )


def build_risk_policy(settings: Settings) -> RiskPolicy:
    return RiskPolicy(
        RiskLimits(
            max_order_notional=settings.risk_max_order_notional,
            max_position_notional=settings.risk_max_position_notional,
            min_order_notional=settings.risk_min_order_notional,
            max_orders_per_minute=settings.risk_max_orders_per_minute,
            max_daily_loss_pct=settings.risk_max_daily_loss_pct,
            max_drawdown_pct=settings.risk_max_drawdown_pct,
        )
    )


def build_trading(
    request: Request,
    session: Session,
    *,
    accounts: AccountService | None = None,
    market: MarketService | None = None,
    with_brokers: bool = False,
) -> TradingService:
    settings = settings_of(request)
    accounts = accounts or build_accounts(request, session)
    market = market or build_market(request, session)
    broker = cn_broker = None
    if with_brokers:
        broker = build_broker(settings)
        cn_broker = getattr(request.app.state, "cn_gateway", None) or build_ths_gateway(
            settings, price_feed=market.last_price
        )
    return TradingService(
        session,
        cash=AccountCashAdapter(accounts),
        prices=MarketPriceAdapter(market),
        policy=build_risk_policy(settings),
        bus=getattr(request.app.state, "bus", None),
        fee_bps=settings.fee_bps,
        slippage_bps=settings.slippage_bps,
        risk=RiskService(session),
        broker=broker,
        cn_broker=cn_broker,
        cn_fees=cn_fee_schedule(settings),
        cn_session_enforced=settings.cn_session_enforced,
        cn_live_session_gate=getattr(settings, "cn_live_session_gate", True),
        live_market=live_market(request),
    )


def build_portfolio(
    request: Request,
    session: Session,
    *,
    accounts: AccountService | None = None,
    trading: TradingService | None = None,
) -> PortfolioService:
    accounts = accounts or build_accounts(request, session)
    trading = trading or build_trading(request, session, accounts=accounts)
    return PortfolioService(session, accounts, trading)


def build_backtests(
    request: Request, session: Session, *, market: MarketService | None = None
) -> BacktestService:
    settings = settings_of(request)
    market = market or build_market(request, session, live=False)
    return BacktestService(
        session,
        market,
        fee_bps=settings.fee_bps,
        slippage_bps=settings.slippage_bps,
        cn_fee_schedule=cn_fee_schedule(settings),
    )
