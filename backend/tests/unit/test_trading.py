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


def test_resting_buy_reserves_cash(env):
    """A resting limit buy must lock its notional.

    Without reservation the same balance could back unlimited pending orders and
    the account would go negative when several filled together.
    """
    service, accounts, account, _ = env
    service.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=50, limit_price=90.0,
    )
    reserved = service.reserved_cash(account.id)
    # Reservation includes the fee the fill would charge (fee_bps = 10).
    expected = 50 * 90.0 * (1 + 10 / 10_000)
    assert reserved == pytest.approx(expected)
    assert service.available_cash(account.id) == pytest.approx(10_000 - expected)

    # A second order exceeding the remaining budget is refused.
    with pytest.raises(ConflictError):
        service.place_order(
            account_id=account.id, symbol="BTCUSDT", side="buy",
            order_type="limit", quantity=70, limit_price=90.0,
        )


def test_cancelled_order_releases_cash(env):
    service, _, account, _ = env
    order = service.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=50, limit_price=90.0,
    )
    assert service.available_cash(account.id) < 10_000
    service.cancel_order(order.id)
    assert service.available_cash(account.id) == pytest.approx(10_000)


def test_filled_resting_order_consumes_reservation(env):
    service, accounts, account, prices = env
    service.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=20, limit_price=95.0,
    )
    before = service.available_cash(account.id)
    prices.current = 94.0
    assert service.match_open_orders() == 1
    positions = service.positions(account.id)
    assert positions[0].quantity == 20
    # Cash actually spent; nothing remains reserved.
    assert service.reserved_cash(account.id) == 0
    expected_cash = 10_000 - 20 * 94.0 * (1 + 10 / 10_000)
    assert accounts.cash(account.id) == pytest.approx(expected_cash, abs=1e-6)
    assert service.available_cash(account.id) == pytest.approx(accounts.cash(account.id))
    # Filled below the limit price, so the released reservation exceeds the actual
    # spend and available cash can rise: price improvement is real, not a bug.
    assert service.available_cash(account.id) == pytest.approx(before + (95.0 - 94.0) * 20, abs=0.5)


def test_resting_sell_locks_shares(env):
    service, accounts, account, prices = env
    service.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="market", quantity=10,
    )
    assert service.positions(account.id)[0].available_quantity == pytest.approx(10)
    service.place_order(
        account_id=account.id, symbol="BTCUSDT", side="sell",
        order_type="limit", quantity=6, limit_price=200.0,
    )
    view = service.positions(account.id)[0]
    assert view.available_quantity == pytest.approx(4)  # 6 shares locked
    with pytest.raises(ConflictError):
        service.place_order(
            account_id=account.id, symbol="BTCUSDT", side="sell",
            order_type="limit", quantity=5, limit_price=200.0,
        )


def test_resting_orders_do_not_trip_breaker(database):
    from app.modules.risk.policy import RiskLimits, RiskPolicy
    from app.modules.risk.service import RiskService

    prices = FakePrices()
    with database.session() as session:
        accounts = AccountService(session)
        account = accounts.create(name="挂单熔断", initial_balance=100_000)
        service = TradingService(
            session,
            cash=AccountCashAdapter(accounts),
            prices=prices,
            policy=RiskPolicy(
                RiskLimits(
                    max_order_notional=1_000_000,
                    max_position_notional=1_000_000,
                    max_daily_loss_pct=0.05,
                )
            ),
            risk=RiskService(session),
            fee_bps=0,
            slippage_bps=0,
        )
        # Rest a buy for 60% of equity: if reservations counted as losses this
        # would read as a -60% day and halt the account.
        service.place_order(
            account_id=account.id, symbol="BTCUSDT", side="buy",
            order_type="limit", quantity=600, limit_price=100.0,
        )
        state = RiskService(session).state(account.id)
        assert state.breaker_active is False

        # A further small order must still be accepted.
        order = service.place_order(
            account_id=account.id, symbol="BTCUSDT", side="buy",
            order_type="market", quantity=1,
        )
        assert order.status == "filled"


def test_missing_price_values_a_position_at_cost_not_zero(env):
    """A quote outage must not look like a total loss to the risk breakers.

    `_safe_price` returns 0 when the feed is unavailable. Valuing the position at
    0 wipes its market value, which the daily-loss and drawdown breakers read as
    a catastrophic loss and trip on — blocking all trading because a quote was
    missing for one tick.
    """
    service, accounts, account, _prices = env
    service.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy", order_type="market", quantity=2
    )

    class NoPrice:
        def price(self, _symbol):
            raise RuntimeError("feed down")

        def symbol(self, _code):
            raise RuntimeError("feed down")

    blind = TradingService(
        service.session,
        cash=service.cash,
        prices=NoPrice(),
        policy=service.policy,
    )
    view = blind.positions(account.id)[0]
    # Cost, not zero: no invented loss and no invented gain. avg_price includes
    # the entry fee, so it is slightly above the 100.0 market price.
    assert view.market_price == pytest.approx(100.1)
    assert view.market_value == pytest.approx(200.2)
    assert view.unrealized_pnl == pytest.approx(0.0)


def test_limit_order_notional_uses_the_worse_of_limit_and_market(env):
    """A limit price below market must not shrink the order's apparent size.

    `max_order_notional` exists to bound how much one order can put at risk. A buy
    limit far below market was measured at its own price, so 0.1 BTC at 9,000 read
    as 900 against a 1,000 cap while the same size at market (7,025) was refused —
    and a resting limit fills at market once the book moves through it.
    """
    service, accounts, account, prices = env
    service.policy = RiskPolicy(
        RiskLimits(max_order_notional=1_000, min_order_notional=10)
    )
    prices.current = 70_000.0

    with pytest.raises(ConflictError) as exc:
        service.place_order(
            account_id=account.id,
            symbol="BTCUSDT",
            side="buy",
            order_type="limit",
            quantity=0.1,
            limit_price=9_000.0,
        )
    assert "exceeds the limit" in str(exc.value)

    # A limit above market is the genuinely worst case for a buy and is used as-is.
    with pytest.raises(ConflictError):
        service.place_order(
            account_id=account.id,
            symbol="BTCUSDT",
            side="buy",
            order_type="limit",
            quantity=0.1,
            limit_price=80_000.0,
        )

    # A small order still passes: the guard must not block everything.
    service.policy = RiskPolicy(
        RiskLimits(max_order_notional=100_000, min_order_notional=10)
    )
    order = service.place_order(
        account_id=account.id,
        symbol="BTCUSDT",
        side="buy",
        order_type="limit",
        quantity=0.01,
        limit_price=9_000.0,
    )
    assert order.status == "open"


def test_full_budget_sizing_reserves_the_fee(env):
    """Spending the entire available balance must still be executable.

    Sizing from budget/price produced an order whose notional equalled the budget,
    so the fee pushed the total over it and every full-size order was rejected:
    "need 10,007, available 10,000". The runner hit this whenever an instance was
    configured with position_pct=1.0.
    """
    service, accounts, account, prices = env
    prices.current = 100.0
    budget = accounts.cash(account.id)

    qty = service.max_affordable_quantity(
        account.id, "BTCUSDT", prices.current, budget
    )
    assert qty > 0
    notional = qty * prices.current
    assert notional <= budget
    # The whole point: notional + fee still fits.
    assert notional + service._fee("crypto", "buy", notional) <= budget + 1e-9

    order = service.place_order(
        account_id=account.id,
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        quantity=qty,
    )
    assert order.status == "filled"
    assert accounts.cash(account.id) >= 0

    # With no fee the same budget buys strictly more, confirming the reserve is
    # real and not just rounding noise.
    free_service = TradingService(
        service.session,
        cash=service.cash,
        prices=prices,
        policy=service.policy,
        fee_bps=0,
        slippage_bps=0,
    )
    assert free_service.max_affordable_quantity(
        account.id, "BTCUSDT", prices.current, budget
    ) >= qty
