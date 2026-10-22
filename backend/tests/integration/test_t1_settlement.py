"""T+1 settlement must run even when there is no simulated A-share engine.

Real-data deployments (seed_market=False, cn_data_source=tencent) have no
CnMarketEngine, so the composite provider's `.cn` is None. The settlement duty
returned early whenever the engine clock was missing, which meant a position
bought today stayed unsellable forever — nothing else ever clears
`available_quantity`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.models import Account
from app.modules.accounts.service import AccountService
from app.modules.market.models import Candle, Symbol
from app.modules.market.service import MarketService
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.engine import TradingEngine
from app.modules.trading.models import Position
from app.modules.trading.service import TradingService


class _NoCnProvider:
    """Stand-in for CompositeLiveProvider in a real-data-only deployment."""

    cn = None

    def live_candle(self, _symbol):
        return None


@pytest.fixture()
def real_mode_db(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 't1-real.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=True,
        cn_data_source="synthetic",
        cn_sync_on_start=False,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    yield db, settings
    db.dispose()


def _seed_one_cn_symbol(db: Database) -> str:
    from datetime import datetime

    from app.modules.cn.calendar import CN_TZ
    from app.modules.cn.engine import CnMarketEngine

    CnMarketEngine(db, Settings()).ensure_symbols()
    with db.session() as session:
        row = session.scalars(
            select(Symbol).where(Symbol.market == "cn_stock").limit(1)
        ).first()
        code = row.code
        day = datetime(2026, 10, 21, tzinfo=CN_TZ)
        base = int(day.timestamp() * 1000)
        for offset in (0, 60_000, 120_000):
            session.add(
                Candle(
                    symbol=code,
                    timeframe="15m",
                    open_time=base + offset,
                    open=10.0,
                    high=10.1,
                    low=9.9,
                    close=10.0,
                    volume=1000,
                )
            )
        session.commit()
    return code


def test_settlement_runs_without_a_cn_engine(real_mode_db):
    db, settings = real_mode_db
    code = _seed_one_cn_symbol(db)

    with db.session() as session:
        accounts = AccountService(session)
        account = accounts.create(
            name="真实数据账户", initial_balance=300_000, market="cn_stock"
        )
        market = MarketService(session)
        trading = TradingService(
            session,
            cash=AccountCashAdapter(accounts),
            prices=MarketPriceAdapter(market),
            fee_bps=0,
            slippage_bps=0,
        )
        trading.place_order(
            account_id=account.id,
            symbol=code,
            side="buy",
            order_type="market",
            quantity=100,
        )
        position = session.scalar(
            select(Position).where(Position.account_id == account.id)
        )
        assert position.available_quantity == 0

        engine = TradingEngine(
            db, settings, market_engine=_NoCnProvider(), bus=None
        )
        # Force the duty to run regardless of the wall clock's session state.
        import app.modules.trading.engine as engine_module

        original = engine_module.is_session_open
        engine_module.is_session_open = lambda _ts=None: True
        try:
            engine._session_duties(session)
        finally:
            engine_module.is_session_open = original

        session.refresh(position)
        assert position.quantity == pytest.approx(100)
        assert position.available_quantity == pytest.approx(100)


def test_settlement_does_not_touch_crypto_positions(real_mode_db):
    """Crypto settles instantly; the sweep must not paper over mismatches."""
    db, _settings = real_mode_db
    with db.session() as session:
        accounts = AccountService(session)
        account = accounts.create(
            name="加密账户", initial_balance=100_000, market="crypto"
        )
        session.add(
            Position(
                account_id=account.id,
                symbol="BTCUSDT",
                quantity=1.0,
                available_quantity=0.0,  # deliberately inconsistent
                avg_price=50_000,
            )
        )
        session.commit()

        market = MarketService(session)
        trading = TradingService(
            session,
            cash=AccountCashAdapter(accounts),
            prices=MarketPriceAdapter(market),
            fee_bps=0,
            slippage_bps=0,
        )
        assert trading.settle_t1() == 0
        position = session.scalar(
            select(Position).where(Position.account_id == account.id)
        )
        assert position.available_quantity == 0.0


def test_close_position_is_blocked_by_t_plus_one(tmp_path):
    """Closing a same-day A-share position must still be refused.

    `close_position` now cancels resting sells and sends the *full* holding, so
    the T+1 guard is the only thing standing between "close" and selling shares
    that are not yet deliverable.
    """
    from app.core.config import Settings
    from app.core.db import Database
    from app.main import create_app
    from app.modules.accounts.service import AccountService
    from app.modules.market.service import MarketService
    from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
    from app.modules.trading.models import Position
    from app.modules.trading.service import TradingService
    from fastapi.testclient import TestClient

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'close_t1.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=10,
        market_seed_bars=120,
        runtime_enabled=False,
        cn_session_enforced=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app):
        with db.session() as session:
            accounts = AccountService(session)
            account = accounts.create(
                name="当日平仓", initial_balance=500_000, market="cn_stock"
            )
            market = MarketService(session)
            trading = TradingService(
                session,
                cash=AccountCashAdapter(accounts),
                prices=MarketPriceAdapter(market),
            )
            session.add(
                Position(
                    account_id=account.id,
                    symbol="600519.SH",
                    quantity=100.0,
                    available_quantity=0.0,   # bought today
                    avg_price=1300.0,
                )
            )
            session.commit()
            try:
                trading.close_position(account.id, "600519.SH")
            except Exception as exc:
                assert "T+1" in str(exc) or "可卖" in str(exc)
            else:
                raise AssertionError("close must respect T+1")
    db.dispose()
