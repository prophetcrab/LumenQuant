from __future__ import annotations

import pytest

from app.core.errors import ConflictError
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


def test_drawdown_halt_survives_daily_rollover(database, monkeypatch):
    """A structural drawdown halt must not be released by a new trading day.

    Only the daily-loss budget resets; the drawdown breaker keeps its reason until
    an explicit reset, otherwise a single -30% month would silently un-halt.
    """
    from app.modules.accounts.service import AccountService
    from app.modules.risk.service import RiskService

    with database.session() as session:
        accounts = AccountService(session)
        account = accounts.create(name="回撤账户", initial_balance=100_000, market="crypto")
        service = RiskService(session)

        monkeypatch.setattr(
            "app.modules.risk.service.risk_day_key",
            lambda market, ts_ms=None: "2026-10-20",
        )
        service.observe(account.id, 100_000)
        tripped = service.trip(account.id, "drawdown circuit breaker (30.00% >= 25.00%)")
        assert tripped.halt_kind == "drawdown"

        monkeypatch.setattr(
            "app.modules.risk.service.risk_day_key",
            lambda market, ts_ms=None: "2026-10-21",
        )
        rolled = service.observe(account.id, 70_000)
        assert rolled.breaker_active is True
        assert rolled.halt_kind == "drawdown"
        assert "drawdown" in rolled.breaker_reason

        # Explicit reset is the only way out.
        cleared = service.reset(account.id)
        assert cleared.breaker_active is False


def test_daily_halt_releases_while_drawdown_persists(database, monkeypatch):
    from app.modules.accounts.service import AccountService
    from app.modules.risk.service import RiskService

    with database.session() as session:
        accounts = AccountService(session)
        account = accounts.create(name="混合账户", initial_balance=100_000, market="crypto")
        service = RiskService(session)
        monkeypatch.setattr(
            "app.modules.risk.service.risk_day_key",
            lambda market, ts_ms=None: "2026-10-20",
        )
        service.observe(account.id, 100_000)
        service.trip(account.id, "drawdown circuit breaker (26.00% >= 25.00%)")
        service.trip(account.id, "daily loss limit hit (-5.20% <= -5.00%)")

        monkeypatch.setattr(
            "app.modules.risk.service.risk_day_key",
            lambda market, ts_ms=None: "2026-10-21",
        )
        after = service.observe(account.id, 74_000)
        # Drawdown reason wins and is still present.
        assert after.breaker_active is True
        assert after.halt_kind == "drawdown"


def test_position_limit_uses_market_value_not_cost():
    """Exposure is what the position is worth now, not what it cost.

    A holding bought at 100 and now trading at 200 carries twice the risk the cost
    figure implies. Projecting the new exposure from cost let a buy double the real
    exposure while appearing to stay under the limit.
    """
    policy = RiskPolicy(RiskLimits(max_order_notional=1_000_000, max_position_notional=300_000))
    intent = OrderIntent(side="buy", quantity=1000, price=200.0, fee_bps=0)

    # 2000 shares at cost 100 = 200,000 booked; at market 200 = 400,000 real.
    # Adding 1000 more at 200 pushes real exposure to 600,000 > 300,000 limit.
    ctx = RiskContext(
        cash=1_000_000,
        position_qty=2000,
        position_avg_price=100.0,
        position_market_price=200.0,
        orders_last_minute=0,
    )
    with pytest.raises(ConflictError) as exc:
        policy.validate(intent, ctx, SYMBOL)
    assert "notional limit" in str(exc.value)

    # The same order at cost valuation would have looked acceptable, which is
    # exactly the bug: 2000*100 + 200,000 = 400,000... still over, so pick a case
    # where only the market valuation trips it.
    ctx_cost_ok = RiskContext(
        cash=1_000_000,
        position_qty=1000,
        position_avg_price=100.0,
        position_market_price=0.0,   # unknown -> cost fallback
        orders_last_minute=0,
    )
    small = OrderIntent(side="buy", quantity=100, price=200.0, fee_bps=0)
    # 1000*100 + 20,000 = 120,000 -> allowed at cost.
    policy.validate(small, ctx_cost_ok, SYMBOL)

    ctx_market_high = RiskContext(
        cash=1_000_000,
        position_qty=1000,
        position_avg_price=100.0,
        position_market_price=200.0,  # same holding, now worth 200,000
        orders_last_minute=0,
    )
    # 1000*200 + 20,000 = 220,000, still under 300k, so this must pass.
    policy.validate(small, ctx_market_high, SYMBOL)

    # And 1000 shares more at 200: 1000*200 + 200,000 = 400,000 -> blocked.
    big = OrderIntent(side="buy", quantity=1000, price=200.0, fee_bps=0)
    with pytest.raises(ConflictError):
        policy.validate(big, ctx_market_high, SYMBOL)


def test_same_day_a_share_position_counts_toward_the_notional_limit():
    """T+1 makes a holding unsellable, not unowned.

    `position_qty` (sellable) is 0 on the purchase day for A-shares, so the
    exposure limit used to see no position at all and let a second buy through
    entirely unprotected. Exposure and sellability are different questions.
    """
    policy = RiskPolicy(RiskLimits(max_order_notional=1_000_000, max_position_notional=150_000))
    ctx = RiskContext(
        cash=1_000_000,
        position_qty=0.0,          # bought today: frozen by T+1
        exposure_qty=100.0,        # but owned, and carrying market risk
        position_avg_price=1310.0,
        position_market_price=1310.0,
        orders_last_minute=0,
    )
    with pytest.raises(ConflictError) as exc:
        policy.validate(
            OrderIntent(side="buy", quantity=100, price=1310.0, fee_bps=0), ctx, SYMBOL
        )
    assert "notional limit" in str(exc.value)
    # Selling still obeys T+1: nothing is sellable, so a sell is refused.
    with pytest.raises(ConflictError):
        policy.validate(
            OrderIntent(side="sell", quantity=100, price=1310.0, fee_bps=0), ctx, SYMBOL
        )
