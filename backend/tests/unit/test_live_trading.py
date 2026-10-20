from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.modules.accounts.service import AccountService
from app.modules.brokers.base import BrokerAccount, BrokerOrderResult
from app.modules.market.service_types import SymbolView
from app.modules.trading.adapters import AccountCashAdapter
from app.modules.trading.service import TradingService


class FakePrices:
    current = 100.0

    def price(self, _symbol):
        return self.current

    def symbol(self, code):
        return SymbolView(
            code=code, name=code, asset_class="test", base=code, quote="USDT",
            tick_size=0.01, lot_size=0.001, initial_price=100.0, enabled=True,
        )


class FakeBroker:
    name = "fake"

    def __init__(self, status="filled", filled=2.0, price=101.0):
        self.calls = []
        self.status = status
        self.filled = filled
        self.price = price

    def verify_credentials(self):
        return BrokerAccount(can_trade=True, balances={"USDT": 1_000_000.0})

    def place_order(self, request):
        self.calls.append(request)
        return BrokerOrderResult(
            broker_order_id="B-1",
            status=self.status,
            filled_quantity=self.filled,
            avg_price=self.price,
            raw_status=self.status.upper(),
        )

    def cancel_order(self, symbol, broker_order_id):
        return None

    def open_orders(self, symbol):
        return []


def make_env(database, *, broker=None):
    prices = FakePrices()
    session = database.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="Live Book", kind="live", initial_balance=100_000, broker="binance"
    )
    service = TradingService(
        session,
        cash=AccountCashAdapter(accounts),
        prices=prices,
        fee_bps=0,
        slippage_bps=0,
        broker=broker,
    )
    return session, service, accounts, account


def test_live_order_without_broker_is_forbidden(database):
    session, service, _, account = make_env(database)
    try:
        with pytest.raises(AppError):
            service.place_order(
                account_id=account.id, symbol="BTCUSDT", side="buy",
                order_type="market", quantity=1,
            )
    finally:
        session.close()


def test_live_market_fill_mirrors_broker(database):
    broker = FakeBroker()
    session, service, accounts, account = make_env(database, broker=broker)
    try:
        order = service.place_order(
            account_id=account.id, symbol="BTCUSDT", side="buy",
            order_type="market", quantity=2,
        )
        assert broker.calls and broker.calls[0].quantity == 2
        assert order.status == "filled"
        assert order.avg_fill_price == pytest.approx(101.0)
        assert order.source.startswith("live:")
        positions = service.positions(account.id)
        assert positions[0].quantity == 2
        assert positions[0].avg_price == pytest.approx(101.0)
        assert accounts.cash(account.id) == pytest.approx(99_798.0)
        assert len(service.fills(account.id)) == 1
    finally:
        session.close()


def test_live_limit_order_rests_without_position(database):
    broker = FakeBroker(status="open", filled=0.0, price=95.0)
    session, service, _, account = make_env(database, broker=broker)
    try:
        order = service.place_order(
            account_id=account.id, symbol="BTCUSDT", side="buy",
            order_type="limit", quantity=1, limit_price=95,
        )
        assert order.status == "open"
        assert service.positions(account.id) == []
        assert service.fills(account.id) == []
    finally:
        session.close()
