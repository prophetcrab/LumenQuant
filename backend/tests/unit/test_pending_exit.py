"""Protective exits against a live gateway that fills asynchronously.

The gateway acknowledges an order and fills it later, so the position stays open
on every scheduler tick. That is the normal live case — and the one where a naive
"close the position" loop resubmits forever.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.alerts.models import Alert
from app.modules.alerts.service import AlertService
from app.modules.brokers.base import BrokerAccount, BrokerOrderResult
from app.modules.market.service_types import SymbolView
from app.modules.runtime.service import RuntimeService
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.models import Position
from app.modules.trading.service import TradingService


class PendingGateway:
    """Accepts orders and fills nothing, like a real broker during the session."""

    name = "pending-mock"

    def __init__(self) -> None:
        self.placed = 0
        self.cancelled = 0

    def verify_credentials(self) -> BrokerAccount:
        return BrokerAccount(can_trade=True, balances={"USDT": 1_000_000.0})

    def place_order(self, request) -> BrokerOrderResult:
        self.placed += 1
        return BrokerOrderResult(
            broker_order_id=f"P{self.placed}",
            status="open",
            filled_quantity=0.0,
            avg_price=0.0,
            raw_status="accepted",
        )

    def cancel_order(self, symbol: str, broker_order_id: str) -> None:
        self.cancelled += 1

    def order_status(self, broker_order_id: str) -> BrokerOrderResult:
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status="open",
            filled_quantity=0.0,
            avg_price=0.0,
            raw_status="accepted",
        )

    def open_orders(self, symbol: str) -> list[dict]:
        return []

    def positions(self) -> list:
        return []

    def trades_since(self, since_ms: int) -> list[dict]:
        return []


class CryptoMarket:
    def __init__(self) -> None:
        self.prices: dict[str, float] = {}

    def last_price(self, symbol: str) -> float:
        return self.prices.get(symbol, 100.0)

    def supported_timeframes(self, market: str = "crypto"):
        return ("15m", "30m", "1h", "4h", "1d")

    def require_symbol(self, code: str) -> SymbolView:
        return SymbolView(
            code=code, name=code, asset_class="crypto", base=code, quote="USDT",
            tick_size=0.01, lot_size=0.01, initial_price=100.0, enabled=True,
            market="crypto",
        )


def _stack(
    tmp_path,
    gateway,
    *,
    alerts: bool = False,
    initial_price: float = 90.0,
    stop_loss_pct: float = 0.05,
    trailing_stop_pct: float = 0.0,
):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'pending.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
        live_trading_enabled=True,
        live_auto_trading_enabled=True,
    )
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    market = CryptoMarket()
    market.prices["BTCUSDT"] = initial_price
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(name="live", initial_balance=100_000, kind="live", market="crypto")
    session.add(
        Position(
            account_id=account.id, symbol="BTCUSDT",
            quantity=1.0, available_quantity=1.0, avg_price=100.0,
        )
    )
    session.commit()
    trading = TradingService(
        session,
        cash=AccountCashAdapter(accounts),
        prices=MarketPriceAdapter(market),
        fee_bps=0,
        slippage_bps=0,
        broker=gateway,
    )
    service = RuntimeService(
        session,
        accounts_service=accounts,
        market=market,
        trading=trading,
        live_auto_trading_enabled=True,
        alerts=AlertService(session) if alerts else None,
    )
    view = service.create(
        account_id=account.id, strategy_key="sma_cross", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.5, max_order_notional=100_000,
        stop_loss_pct=stop_loss_pct, trailing_stop_pct=trailing_stop_pct, start=True,
    )
    return db, session, service, view


def test_a_pending_exit_is_not_resubmitted_every_tick(tmp_path):
    """A stop must produce one order, not one per scheduler tick.

    `close_position` cancels working sells before submitting, which is right for a
    stale limit order but wrong for the exit it just placed itself: the gateway
    keeps the order open until it fills, so each tick cancelled the previous exit
    and sent a new one. Measured before the fix: 4 submits, 3 cancels in 8 ticks,
    and no order lived long enough to fill.
    """
    gateway = PendingGateway()
    db, session, service, view = _stack(tmp_path, gateway)

    outcomes = [service.protect_positions() for _ in range(5)]

    assert gateway.placed == 1, "a pending exit must not be resubmitted"
    assert gateway.cancelled == 0, "the working exit must not be cancelled"
    assert outcomes[0][view.id] == "stop_loss"
    assert all(item[view.id] == "stop_loss_pending" for item in outcomes[1:])
    assert service._require(view.id).executions == 1
    session.close()
    db.dispose()


def test_a_pending_exit_does_not_alert_every_tick(tmp_path):
    """Repeated critical alerts for one stop would bury the real event."""
    gateway = PendingGateway()
    db, session, service, _view = _stack(tmp_path, gateway, alerts=True)

    for _ in range(5):
        service.protect_positions()

    rows = session.scalars(select(Alert)).all()
    assert len(rows) == 1, f"expected one alert, got {len(rows)}"
    session.close()
    db.dispose()


def test_the_pending_exit_is_marked_so_it_can_be_told_apart(tmp_path):
    """The marker must survive the `live:` prefix `place_order` adds.

    Live order rows are stored as `live:<source>`, so matching the marker at the
    start of the string found nothing and the reuse check silently never fired.
    """
    from app.modules.trading.models import Order

    gateway = PendingGateway()
    db, session, service, _view = _stack(tmp_path, gateway)
    service.protect_positions()

    order = session.scalar(select(Order).where(Order.side == "sell"))
    assert order is not None
    assert order.source.startswith("live:exit:")
    session.close()
    db.dispose()


def test_restarting_does_not_reuse_a_stale_trailing_peak(tmp_path):
    """A peak from before the stop must not fire the trailing stop on restart.

    `set_running(False)` only flipped a flag, so the recorded peak survived the
    pause. Restarting with peak 200 while the price sat at 120 made the very first
    tick compute a −40% drawdown and exit a healthy position.
    """
    # Start flat-ish and set the price explicitly below: the trailing stop only
    # fires on a drop from a peak observed *after* the restart.
    # Only the trailing rule is armed here: a 5% fixed stop would fire first and
    # the test would prove nothing about the peak.
    db, session, service, view = _stack(
        tmp_path, PendingGateway(), initial_price=100.0,
        stop_loss_pct=0.0, trailing_stop_pct=0.10,
    )
    row = service._require(view.id)
    row.peak_price = 200.0
    session.commit()

    service.set_running(view.id, False)
    # Price moved while the instance was off; protection must resume from here.
    service.market.prices["BTCUSDT"] = 120.0
    service.set_running(view.id, True)

    assert service._require(view.id).peak_price == 0.0
    assert service.protect_positions() == {}

    # The trailing stop still works from the restart price (120 → 107 is −10.8%).
    # The peak is established by the tick above, so the drop needs a second tick.
    service.market.prices["BTCUSDT"] = 107.0
    outcomes = service.protect_positions()
    assert outcomes == {view.id: "trailing_stop"}
    session.close()
    db.dispose()
