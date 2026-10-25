"""Two duties the live path used to skip.

1. **Cancel race** — a broker can fill part of an order between the last poll and
   the cancel. Writing `cancelled` took the order out of the polled set, dropping
   that fill from the ledger for ever while the broker kept the money and the
   asset.

2. **A-share T+1 with no strategy runtime** — the release used to live only in
   `TradingEngine`, which is not constructed when `LQ_RUNTIME_ENABLED=false`
   (manual live orders only). An A-share buy then stayed unsellable for ever.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.brokers.base import BrokerAccount, BrokerOrderResult
from app.modules.brokers.reconciler import LiveReconciler
from app.modules.market.service_types import SymbolView
from app.modules.trading.adapters import AccountCashAdapter
from app.modules.trading.models import CashHold, Fill, Order, Position
from app.modules.trading.service import TradingService


class Prices:
    def price(self, _symbol):
        return 100.0

    def symbol(self, code):
        return SymbolView(
            code=code, name=code, asset_class="crypto", base=code, quote="USDT",
            tick_size=0.01, lot_size=0.001, initial_price=100.0, enabled=True,
            market="crypto",
        )


class Broker:
    """Reports a configurable order state, and records cancels."""

    name = "binance"

    def __init__(self) -> None:
        self.filled = 0.0
        self.fill_price = 0.0
        self.quote_quantity: float | None = None
        self.status = "open"
        self.cancelled = 0
        self.status_error: Exception | None = None

    def verify_credentials(self):
        return BrokerAccount(can_trade=True, balances={"USDT": 1_000_000.0})

    def place_order(self, request):
        return BrokerOrderResult("B-1", "open", 0.0, 0.0, "NEW")

    def cancel_order(self, symbol, broker_order_id):
        self.cancelled += 1

    def open_orders(self, symbol):
        return []

    def order_status(self, broker_order_id, *, symbol=""):
        if self.status_error is not None:
            raise self.status_error
        return BrokerOrderResult(
            broker_order_id,
            self.status,
            self.filled,
            self.fill_price,
            "FILLED" if self.status == "filled" else "NEW",
            quote_quantity=self.quote_quantity,
        )


def _settings(tmp_path, name="cancel.db", **overrides) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / name).as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
        live_trading_enabled=True,
        fee_bps=0.0,
        **overrides,
    )


def _crypto_stack(tmp_path, broker=None):
    settings = _settings(tmp_path)
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="Live", kind="live", market="crypto",
        initial_balance=100_000, broker="binance",
    )
    trading = TradingService(
        session, cash=AccountCashAdapter(accounts), prices=Prices(),
        fee_bps=0.0, slippage_bps=0.0, broker=broker,
    )
    return db, session, accounts, account, trading


# --------------------------------------------------------------- cancel race
def test_cancel_books_a_fill_that_happened_before_it(tmp_path):
    """The broker traded 0.4 during the poll gap; cancelling must record it.

    This is the default path, not an edge case: the reconciler polls every three
    seconds, and a partially filled limit order is exactly what a user cancels.
    """
    broker = Broker()
    db, session, accounts, account, trading = _crypto_stack(tmp_path, broker)
    order = trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )
    start_cash = accounts.cash(account.id)

    # The broker filled 0.4 at 95 before the cancel arrived.
    broker.filled, broker.fill_price, broker.quote_quantity = 0.4, 95.0, 38.0
    broker.status = "cancelled"

    cancelled = trading.cancel_order(order.id)

    assert broker.cancelled == 1
    assert cancelled.status == "cancelled"
    assert cancelled.filled_quantity == pytest.approx(0.4)
    assert cancelled.avg_fill_price == pytest.approx(95.0)
    # The money and the position are both in the book.
    assert accounts.cash(account.id) == pytest.approx(start_cash - 38.0)
    session.expire_all()
    position = session.scalar(select(Position))
    assert position is not None and position.quantity == pytest.approx(0.4)
    fills = session.scalars(select(Fill)).all()
    assert len(fills) == 1 and fills[0].quantity == pytest.approx(0.4)
    session.close()
    db.dispose()


def test_cancel_with_no_prior_fill_changes_nothing(tmp_path):
    broker = Broker()
    db, session, accounts, account, trading = _crypto_stack(tmp_path, broker)
    order = trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )
    start_cash = accounts.cash(account.id)

    cancelled = trading.cancel_order(order.id)

    assert cancelled.status == "cancelled"
    assert cancelled.filled_quantity == 0.0
    assert accounts.cash(account.id) == pytest.approx(start_cash)
    assert session.scalar(select(Position)) is None
    session.close()
    db.dispose()


def test_cancel_does_not_double_book_an_already_mirrored_fill(tmp_path):
    """If the reconciler already booked the fill, the cancel must not repeat it."""
    broker = Broker()
    db, session, accounts, account, trading = _crypto_stack(tmp_path, broker)
    order = trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )
    broker.filled, broker.fill_price, broker.quote_quantity = 0.4, 95.0, 38.0
    broker.status = "partially_filled"
    LiveReconciler(db, _settings(tmp_path), broker=broker).tick()

    start_cash = accounts.cash(account.id)
    broker.status = "cancelled"
    trading.cancel_order(order.id)

    assert accounts.cash(account.id) == pytest.approx(start_cash)
    session.expire_all()
    assert session.scalar(select(Position)).quantity == pytest.approx(0.4)
    assert len(session.scalars(select(Fill)).all()) == 1
    session.close()
    db.dispose()


def test_cancel_keeps_the_order_open_when_the_final_state_is_unreadable(tmp_path):
    """A cancel we cannot confirm must not be presented as a finished order.

    The broker may have traded it; leaving the row open keeps the reconciler
    polling, so a late fill is still mirrored.
    """
    broker = Broker()
    db, session, _accounts, account, trading = _crypto_stack(tmp_path, broker)
    order = trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )
    broker.status_error = RuntimeError("exchange timeout")

    result = trading.cancel_order(order.id)

    assert broker.cancelled == 1
    assert result.status == "open"
    assert "无法读取券商终态" in result.reject_reason
    # Recoverable: the reconciler still sees this order.
    broker.status_error = None
    broker.filled, broker.fill_price, broker.quote_quantity = 0.4, 95.0, 38.0
    broker.status = "filled"
    assert LiveReconciler(db, _settings(tmp_path), broker=broker).tick()["filled"] == 1
    session.expire_all()
    assert session.get(Order, order.id).filled_quantity == pytest.approx(0.4)
    session.close()
    db.dispose()


def test_cancel_keeps_the_order_open_when_a_fill_cannot_be_priced(tmp_path):
    """A quantity with no price is a fill we must not discard."""
    broker = Broker()
    db, session, _accounts, account, trading = _crypto_stack(tmp_path, broker)
    order = trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )
    broker.filled, broker.fill_price, broker.status = 0.4, 0.0, "cancelled"

    result = trading.cancel_order(order.id)

    assert result.status == "open"
    assert "未提供成交价" in result.reject_reason
    session.expire_all()
    assert session.get(Order, order.id).filled_quantity == pytest.approx(0.0)
    session.close()
    db.dispose()


# ------------------------------------------------------------------ A-share T+1
class _NoCnEngine:
    """Real-data-only deployment: no simulated A-share engine exists."""

    cn = None


def test_reconciler_releases_t1_without_the_trading_engine(tmp_path, monkeypatch):
    """Manual-only live trading must still unlock tomorrow's sellable shares.

    `TradingEngine` — the usual owner of this duty — is never constructed when the
    strategy runtime is off, so the reconciler has to perform it.
    """
    import app.modules.brokers.settlement as settlement

    settings = _settings(tmp_path, name="t1.db")
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="CN Live", kind="live", market="cn_stock", initial_balance=100_000
    )
    session.add(
        Position(
            account_id=account.id, symbol="600036.SH",
            quantity=1000, available_quantity=0, avg_price=10.0, realized_pnl=0.0,
        )
    )
    session.commit()

    # Pretend a trading session is open regardless of when the suite runs.
    monkeypatch.setattr(settlement, "is_session_open", lambda _ts=None: True)
    reconciler = LiveReconciler(db, settings)
    reconciler._broker = None
    reconciler._gateway = None
    monkeypatch.setattr(type(reconciler), "broker", property(lambda self: None))
    monkeypatch.setattr(type(reconciler), "gateway", property(lambda self: None))

    reconciler.tick()

    session.expire_all()
    position = session.scalar(select(Position))
    assert position.available_quantity == pytest.approx(1000), (
        "A-share buys must become sellable at the next session even with the "
        "strategy runtime disabled"
    )
    session.close()
    db.dispose()


def test_reconciler_releases_due_sale_proceeds_once_per_session(tmp_path, monkeypatch):
    """The same duty clears spent T+1 holds, and only once per day."""
    import app.modules.brokers.settlement as settlement

    settings = _settings(tmp_path, name="t1_hold.db")
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="CN Hold", kind="live", market="cn_stock", initial_balance=100_000
    )
    session.add(
        CashHold(
            account_id=account.id, amount=5000.0,
            release_day="2000-01-01", ref="sale",
        )
    )
    session.commit()

    monkeypatch.setattr(settlement, "is_session_open", lambda _ts=None: True)
    reconciler = LiveReconciler(db, settings)
    monkeypatch.setattr(type(reconciler), "broker", property(lambda self: None))
    monkeypatch.setattr(type(reconciler), "gateway", property(lambda self: None))

    reconciler.tick()
    session.expire_all()
    assert session.scalars(select(CashHold)).all() == []

    # A future hold must survive.
    session.add(
        CashHold(
            account_id=account.id, amount=100.0,
            release_day="2999-01-01", ref="future",
        )
    )
    session.commit()
    reconciler._settled_session = None  # force the sweep again
    reconciler.tick()
    session.expire_all()
    remaining = session.scalars(select(CashHold)).all()
    assert len(remaining) == 1 and remaining[0].ref == "future"
    session.close()
    db.dispose()


def test_reconciler_does_not_unlock_crypto_positions(tmp_path, monkeypatch):
    """Crypto settles instantly; a mismatch there is a bug, not a T+1 hold."""
    import app.modules.brokers.settlement as settlement

    settings = _settings(tmp_path, name="t1_crypto.db")
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="Crypto", kind="live", market="crypto", initial_balance=100_000
    )
    session.add(
        Position(
            account_id=account.id, symbol="BTCUSDT",
            quantity=1.0, available_quantity=0.0, avg_price=50_000.0, realized_pnl=0.0,
        )
    )
    session.commit()

    monkeypatch.setattr(settlement, "is_session_open", lambda _ts=None: True)
    reconciler = LiveReconciler(db, settings)
    monkeypatch.setattr(type(reconciler), "broker", property(lambda self: None))
    monkeypatch.setattr(type(reconciler), "gateway", property(lambda self: None))
    reconciler.tick()

    session.expire_all()
    assert session.scalar(select(Position)).available_quantity == pytest.approx(0.0)
    session.close()
    db.dispose()


def test_the_real_clock_is_used_when_no_engine_provides_one(tmp_path, monkeypatch):
    """A deployment with no simulated engine must not read a fake session."""
    import app.modules.brokers.settlement as settlement

    settings = _settings(tmp_path, name="clock.db")
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="CN", kind="live", market="cn_stock", initial_balance=100_000
    )
    session.add(
        Position(
            account_id=account.id, symbol="600036.SH",
            quantity=100, available_quantity=0, avg_price=10.0, realized_pnl=0.0,
        )
    )
    session.commit()

    seen: list[int] = []

    def fake_open(ts):
        seen.append(ts)
        return False

    monkeypatch.setattr(settlement, "is_session_open", fake_open)
    reconciler = LiveReconciler(db, settings)
    monkeypatch.setattr(type(reconciler), "broker", property(lambda self: None))
    monkeypatch.setattr(type(reconciler), "gateway", property(lambda self: None))
    before = int(time.time() * 1000)
    reconciler.tick()

    assert seen, "the settlement duty must still look at the clock"
    # Wall-clock based, within a generous window around the call.
    assert abs(seen[0] - before) < 60_000
    # Session closed → nothing released.
    session.expire_all()
    assert session.scalar(select(Position)).available_quantity == pytest.approx(0.0)
    session.close()
    db.dispose()
