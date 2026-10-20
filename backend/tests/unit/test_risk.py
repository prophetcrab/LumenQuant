from __future__ import annotations

import pytest

from app.modules.market.service_types import SymbolView
from app.modules.risk.policy import OrderIntent, RiskContext, RiskLimits, RiskPolicy

SYMBOL = SymbolView(
    code="BTCUSDT", name="BTC", asset_class="crypto", base="BTC", quote="USDT",
    tick_size=0.01, lot_size=0.001, initial_price=100.0, enabled=True,
)


def context(**overrides):
    base = dict(
        cash=10_000.0,
        position_qty=0.0,
        position_avg_price=0.0,
        orders_last_minute=0,
        equity=10_000.0,
        day_start_equity=10_000.0,
        peak_equity=10_000.0,
    )
    base.update(overrides)
    return RiskContext(**base)


def intent(side="buy", quantity=1.0, price=100.0, fee_bps=0.0):
    return OrderIntent(side=side, quantity=quantity, price=price, fee_bps=fee_bps)


def test_order_limits_and_lot_size():
    policy = RiskPolicy(RiskLimits(max_order_notional=500, min_order_notional=10))
    assert policy.check(intent(quantity=1), context(), SYMBOL).allowed
    too_big = policy.check(intent(quantity=10), context(), SYMBOL)
    assert not too_big.allowed and "exceeds" in too_big.reason
    too_small = policy.check(intent(quantity=0.001), context(), SYMBOL)
    assert not too_small.allowed and "minimum" in too_small.reason
    bad_lot = policy.check(intent(quantity=1.0005), context(), SYMBOL)
    assert not bad_lot.allowed and "lot size" in bad_lot.reason


def test_cash_and_position_guards():
    policy = RiskPolicy()
    no_cash = policy.check(intent(quantity=1000), context(), SYMBOL)
    assert not no_cash.allowed and "insufficient cash" in no_cash.reason
    no_position = policy.check(intent(side="sell", quantity=1), context(), SYMBOL)
    assert not no_position.allowed and "insufficient position" in no_position.reason


def test_rate_limit():
    policy = RiskPolicy(RiskLimits(max_orders_per_minute=2))
    assert not policy.check(intent(), context(orders_last_minute=2), SYMBOL).allowed


def test_daily_loss_breaker():
    policy = RiskPolicy(RiskLimits(max_daily_loss_pct=0.05))
    ok = policy.breaker(context(equity=9_600, day_start_equity=10_000))
    assert ok.allowed
    tripped = policy.breaker(context(equity=9_400, day_start_equity=10_000))
    assert not tripped.allowed and "daily loss" in tripped.reason


def test_drawdown_breaker():
    policy = RiskPolicy(RiskLimits(max_drawdown_pct=0.20))
    assert policy.breaker(context(equity=8_500, peak_equity=10_000)).allowed
    tripped = policy.breaker(context(equity=7_900, peak_equity=10_000))
    assert not tripped.allowed and "drawdown" in tripped.reason


def test_zero_thresholds_disable_breakers():
    policy = RiskPolicy(RiskLimits(max_daily_loss_pct=0.0, max_drawdown_pct=0.0))
    assert policy.breaker(context(equity=1.0, day_start_equity=10_000, peak_equity=10_000)).allowed


def test_validate_raises():
    policy = RiskPolicy()
    with pytest.raises(Exception):
        policy.validate(intent(quantity=0), context(), SYMBOL)


def test_ensure_baseline_arms_breaker_without_background_tick(database):
    """The first order must seed the daily baseline.

    Otherwise day_start_equity stays 0, the daily-loss formula is skipped, and the
    breaker is silently inert until the portfolio snapshotter happens to run.
    """
    from app.modules.accounts.service import AccountService
    from app.modules.risk.service import RiskService

    with database.session() as session:
        accounts = AccountService(session)
        account = accounts.create(name="基准账户", initial_balance=50_000, market="crypto")
        service = RiskService(session)
        view = service.state(account.id)
        assert view.day_start_equity == 0.0

        armed = service.ensure_baseline(account.id, 50_000)
        assert armed.day_start_equity == 50_000
        assert armed.peak_equity == 50_000

        # Simulate a 10% loss: the breaker now has a reference to compare against.
        policy = RiskPolicy(RiskLimits(max_daily_loss_pct=0.05))
        decision = policy.breaker(
            context(equity=45_000, day_start_equity=armed.day_start_equity)
        )
        assert not decision.allowed


def test_daily_baseline_rolls_over_on_new_day(database, monkeypatch):
    """A new trading day must reset the daily-loss window and release the breaker."""
    from app.modules.accounts.service import AccountService
    from app.modules.risk.service import RiskService

    with database.session() as session:
        accounts = AccountService(session)
        account = accounts.create(name="跨日账户", initial_balance=100_000, market="crypto")
        service = RiskService(session)

        monkeypatch.setattr(
            "app.modules.risk.service.risk_day_key",
            lambda market, ts_ms=None: "2026-10-20",
        )
        service.observe(account.id, 100_000)
        state = service.trip(account.id, "daily loss limit hit")
        assert state.breaker_active is True
        assert state.day_start_equity == pytest.approx(100_000)

        monkeypatch.setattr(
            "app.modules.risk.service.risk_day_key",
            lambda market, ts_ms=None: "2026-10-21",
        )
        fresh = service.observe(account.id, 94_000)
        assert fresh.breaker_active is False
        assert fresh.day_start_equity == pytest.approx(94_000)
        assert fresh.day_key == "2026-10-21"


def test_peak_equity_survives_rollover(database, monkeypatch):
    """Drawdown memory must not be erased by a new day."""
    from app.modules.accounts.service import AccountService
    from app.modules.risk.service import RiskService

    with database.session() as session:
        accounts = AccountService(session)
        account = accounts.create(name="峰值账户", initial_balance=200_000, market="crypto")
        service = RiskService(session)
        monkeypatch.setattr(
            "app.modules.risk.service.risk_day_key",
            lambda market, ts_ms=None: "2026-10-20",
        )
        service.observe(account.id, 200_000)
        monkeypatch.setattr(
            "app.modules.risk.service.risk_day_key",
            lambda market, ts_ms=None: "2026-10-21",
        )
        rolled = service.observe(account.id, 150_000)
        assert rolled.peak_equity == pytest.approx(200_000)
        assert rolled.day_start_equity == pytest.approx(150_000)


def test_cn_risk_day_uses_beijing_date():
    from datetime import datetime, timezone

    from app.modules.risk.daykey import risk_day_key

    # 2026-10-20 23:30 UTC is 2026-10-21 07:30 Beijing.
    ts = int(datetime(2026, 10, 20, 23, 30, tzinfo=timezone.utc).timestamp() * 1000)
    assert risk_day_key("crypto", ts) == "2026-10-20"
    assert risk_day_key("cn_stock", ts) == "2026-10-21"
