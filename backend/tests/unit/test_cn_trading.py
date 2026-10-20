from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.modules.accounts.service import AccountService
from app.modules.market.service_types import SymbolView
from app.modules.trading.adapters import AccountCashAdapter
from app.modules.trading.service import TradingService


class CnPrices:
    """A-share price feed with a price band derived from prev_close."""

    def __init__(self, price: float = 10.0, prev_close: float = 10.0, suspended: bool = False):
        self.current = price
        self.prev_close = prev_close
        self.suspended = suspended

    def price(self, _symbol):
        return self.current

    def symbol(self, code):
        return SymbolView(
            code=code,
            name="测试股份",
            asset_class="stock",
            base=code.split(".")[0],
            quote="CNY",
            tick_size=0.01,
            lot_size=100.0,
            initial_price=self.prev_close,
            enabled=True,
            market="cn_stock",
            exchange="SH",
            board="main",
            prev_close=self.prev_close,
            limit_pct=0.10,
            suspended=self.suspended,
        )


class LockedMarket:
    def __init__(self, locked: str = "") -> None:
        self.locked = locked

    def is_locked(self, _symbol):
        return self.locked

    def prev_close(self, _symbol):
        return 0.0


def make_env(database, *, price=10.0, prev_close=10.0, locked="", suspended=False):
    prices = CnPrices(price, prev_close, suspended)
    session = database.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="A股账户", kind="paper", initial_balance=100_000, market="cn_stock"
    )
    trading = TradingService(
        session,
        cash=AccountCashAdapter(accounts),
        prices=prices,
        fee_bps=0.0,
        slippage_bps=0.0,
    )
    trading.live_market = LockedMarket(locked)
    return session, trading, accounts, account, prices


def test_account_market_and_currency(database):
    session, _, accounts, account, _ = make_env(database)
    try:
        assert account.market == "cn_stock"
        assert account.base_currency == "CNY"
        assert account.leverage == 1.0
    finally:
        session.close()


def test_buy_requires_whole_lots(database):
    session, trading, _, account, _ = make_env(database)
    try:
        with pytest.raises(AppError):
            trading.place_order(
                account_id=account.id, symbol="600519.SH", side="buy",
                order_type="market", quantity=150,
            )
        order = trading.place_order(
            account_id=account.id, symbol="600519.SH", side="buy",
            order_type="market", quantity=300,
        )
        assert order.status == "filled"
        assert order.filled_quantity == 300
    finally:
        session.close()


def test_t_plus_one_blocks_same_day_sell(database):
    session, trading, _, account, _ = make_env(database)
    try:
        trading.place_order(
            account_id=account.id, symbol="600519.SH", side="buy",
            order_type="market", quantity=300,
        )
        positions = trading.positions(account.id)
        assert positions[0].quantity == 300
        assert positions[0].available_quantity == 0  # T+1 held

        with pytest.raises(AppError) as exc:
            trading.place_order(
                account_id=account.id, symbol="600519.SH", side="sell",
                order_type="market", quantity=300,
            )
        assert "可用" in str(exc.value) or "T+1" in str(exc.value)

        # Settlement unlocks the next session.
        assert trading.settle_t1() == 1
        assert trading.positions(account.id)[0].available_quantity == 300
        order = trading.place_order(
            account_id=account.id, symbol="600519.SH", side="sell",
            order_type="market", quantity=300,
        )
        assert order.status == "filled"
    finally:
        session.close()


def test_limit_price_must_stay_in_band(database):
    session, trading, _, account, _ = make_env(database, price=10.0, prev_close=10.0)
    try:
        with pytest.raises(AppError):
            trading.place_order(
                account_id=account.id, symbol="600519.SH", side="buy",
                order_type="limit", quantity=100, limit_price=12.0,
            )
        ok = trading.place_order(
            account_id=account.id, symbol="600519.SH", side="buy",
            order_type="limit", quantity=100, limit_price=10.5,
        )
        assert ok.status in {"open", "filled"}
    finally:
        session.close()


def test_limit_up_blocks_buy(database):
    session, trading, _, account, _ = make_env(database, price=11.0, prev_close=10.0)
    try:
        with pytest.raises(AppError) as exc:
            trading.place_order(
                account_id=account.id, symbol="600519.SH", side="buy",
                order_type="market", quantity=100,
            )
        assert "涨停" in str(exc.value)
    finally:
        session.close()


def test_limit_down_blocks_sell(database):
    session, trading, _, account, prices = make_env(database, price=10.5, prev_close=10.0)
    try:
        trading.place_order(
            account_id=account.id, symbol="600519.SH", side="buy",
            order_type="market", quantity=100,
        )
        trading.settle_t1()
        prices.current = 9.0  # limit-down
        with pytest.raises(AppError) as exc:
            trading.place_order(
                account_id=account.id, symbol="600519.SH", side="sell",
                order_type="market", quantity=100,
            )
        assert "跌停" in str(exc.value)
    finally:
        session.close()


def test_suspended_symbol_rejected(database):
    session, trading, _, account, _ = make_env(database, suspended=True)
    try:
        with pytest.raises(AppError) as exc:
            trading.place_order(
                account_id=account.id, symbol="600519.SH", side="buy",
                order_type="market", quantity=100,
            )
        assert "停牌" in str(exc.value)
    finally:
        session.close()


def test_asymmetric_fees_charged(database):
    session, trading, _, account, _ = make_env(database, price=10.0)
    try:
        trading.place_order(
            account_id=account.id, symbol="600519.SH", side="buy",
            order_type="market", quantity=1000,
        )
        trading.settle_t1()
        sell = trading.place_order(
            account_id=account.id, symbol="600519.SH", side="sell",
            order_type="market", quantity=1000,
        )
        # Stamp duty is sell-side only, so the sell fee must exceed the buy fee.
        fills = trading.fills(account.id)
        buy_fill = next(f for f in fills if f.side == "buy")
        sell_fill = next(f for f in fills if f.side == "sell")
        assert sell_fill.fee > buy_fill.fee
        assert sell.fee > 0
    finally:
        session.close()


def test_market_mismatch_rejected(database):
    session, trading, accounts, account, _ = make_env(database)
    try:
        crypto_account = accounts.create(
            name="币账户", kind="paper", initial_balance=100_000, market="crypto"
        )
        with pytest.raises(AppError) as exc:
            trading.place_order(
                account_id=crypto_account.id, symbol="600519.SH", side="buy",
                order_type="market", quantity=100,
            )
        assert "市场" in str(exc.value)
    finally:
        session.close()


def test_close_position_respects_available(database):
    session, trading, _, account, _ = make_env(database)
    try:
        trading.place_order(
            account_id=account.id, symbol="600519.SH", side="buy",
            order_type="market", quantity=100,
        )
        with pytest.raises(AppError):
            trading.close_position(account.id, "600519.SH")
        trading.settle_t1()
        order = trading.close_position(account.id, "600519.SH")
        assert order is not None and order.status == "filled"
    finally:
        session.close()
