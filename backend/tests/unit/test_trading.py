from __future__ import annotations

import pytest

from app.core.errors import ConflictError, ValidationError
from app.modules.accounts.service import AccountService
from app.modules.market.service_types import SymbolView
from app.modules.risk.policy import RiskLimits, RiskPolicy
from app.modules.trading.adapters import AccountCashAdapter
from app.modules.trading.service import TradingService


class FakePrices:
    def __init__(self, price: float = 100.0) -> None:
        self.current = price

    def price(self, _symbol: str) -> float:
        return self.current

    def symbol(self, code: str) -> SymbolView:
        return SymbolView(
            code=code,
            name=code,
            asset_class="test",
            base=code,
            quote="USDT",
            tick_size=0.01,
            lot_size=0.01,
            initial_price=self.current,
            enabled=True,
        )


@pytest.fixture()
def env(database):
    prices = FakePrices()
    with database.session() as session:
        accounts = AccountService(session)
        account = accounts.create(name="Trader", initial_balance=10_000)
        service = TradingService(
            session,
            cash=AccountCashAdapter(accounts),
            prices=prices,
            policy=RiskPolicy(RiskLimits(max_order_notional=100_000, max_position_notional=100_000)),
            fee_bps=10,
            slippage_bps=0,
        )
        yield service, accounts, account, prices


def test_market_buy_books_fill_position_and_cash(env):
    service, accounts, account, _ = env
    order = service.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy", order_type="market", quantity=2
    )
    assert order.status == "filled"
    assert order.filled_quantity == 2
    # Notional 200 + 0.1% fee = 200.2
    assert accounts.cash(account.id) == pytest.approx(9_799.8)
    positions = service.positions(account.id)
    assert len(positions) == 1
    assert positions[0].quantity == 2
    assert positions[0].avg_price == pytest.approx(100.1)
    assert len(service.fills(account.id)) == 1


def test_sell_realizes_profit(env):
    service, accounts, account, prices = env
    service.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy", order_type="market", quantity=1
    )
    prices.current = 110.0
    order = service.place_order(
        account_id=account.id, symbol="BTCUSDT", side="sell", order_type="market", quantity=1
    )
    assert order.status == "filled"
    positions = service.positions(account.id)
    assert positions == []
    # (110 - 100.1) - 0.11 fee = 9.79
    cash = accounts.cash(account.id)
    assert cash == pytest.approx(10_000 + 9.79, abs=1e-6)


def test_rejects_oversized_order_and_short_sell(env):
    service, _, account, _ = env
    with pytest.raises(ConflictError):
        service.place_order(
            account_id=account.id, symbol="BTCUSDT", side="buy", order_type="market", quantity=500
        )
    with pytest.raises(ConflictError):
        service.place_order(
            account_id=account.id, symbol="BTCUSDT", side="sell", order_type="market", quantity=1
        )
    with pytest.raises(ValidationError):
        service.place_order(
            account_id=account.id, symbol="BTCUSDT", side="buy", order_type="limit", quantity=1
        )


def test_limit_order_rests_then_matches(env):
    service, accounts, account, prices = env
    order = service.place_order(
        account_id=account.id,
        symbol="BTCUSDT",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=95.0,
    )
    assert order.status == "open"
    assert service.match_open_orders() == 0
    prices.current = 94.0
    assert service.match_open_orders() == 1
    filled = service.list_orders(account.id)[0]
    assert filled.status == "filled"
    assert filled.avg_fill_price == pytest.approx(94.0)
    assert accounts.cash(account.id) == pytest.approx(10_000 - 94.094)


def test_cancel_open_order(env):
    service, _, account, _ = env
    order = service.place_order(
        account_id=account.id,
        symbol="BTCUSDT",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=50,
    )
    cancelled = service.cancel_order(order.id)
    assert cancelled.status == "cancelled"
    with pytest.raises(ConflictError):
        service.cancel_order(order.id)


def test_client_order_id_is_idempotent(env):
    service, _, account, _ = env
    first = service.place_order(
        account_id=account.id,
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        quantity=1,
        client_order_id="abc-1",
    )
    second = service.place_order(
        account_id=account.id,
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        quantity=1,
        client_order_id="abc-1",
    )
    assert first.id == second.id
    assert len(service.list_orders(account.id)) == 1


def test_close_position(env):
    service, accounts, account, prices = env
    service.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy", order_type="market", quantity=3
    )
    prices.current = 105
    service.close_position(account.id, "BTCUSDT")
    assert service.positions(account.id) == []
    # (105 - 100.1) * 3 - 0.315 sell fee = 14.385
    assert accounts.cash(account.id) == pytest.approx(10_014.385, abs=1e-6)
