"""Walk-forward validation: window layout, aggregation and warnings."""

from __future__ import annotations

import math

import pytest

from app.core.errors import ValidationError
from app.modules.backtest.engine import BacktestConfig
from app.modules.backtest.walkforward import (
    MIN_TEST_BARS,
    MIN_TOTAL_BARS,
    WalkForwardService,
)
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.library.classics import SmaCross


def series(count: int = 800, drift_a: float = 0.002, drift_b: float = -0.001) -> list[CandlePoint]:
    out: list[CandlePoint] = []
    price = 100.0
    for i in range(count):
        # Alternating regimes so windows differ from each other.
        drift = drift_a if (i // 100) % 2 == 0 else drift_b
        nxt = max(1.0, price * (1 + drift + math.sin(i / 4) * 0.005))
        out.append(
            CandlePoint(
                time=i * 86_400_000,
                open=price,
                high=max(price, nxt) * 1.004,
                low=min(price, nxt) * 0.996,
                close=nxt,
                volume=1_000_000,
            )
        )
        price = nxt
    return out


def cfg() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=1_000_000,
        fee_bps=0,
        slippage_bps=0,
        position_pct=0.95,
        market="cn_stock",
        limit_pct=0.10,
        tick_size=0.01,
        lot_size=100,
        t_plus_one=True,
    )


SPACE = {"fast": [5, 10], "slow": [20, 30]}


def test_auto_split_fits_whenever_geometry_allows():
    """The split must fit whenever the minimums leave room.

    With 400 bars and 4 windows the hard minimums (200 + 60*4 = 440) cannot fit;
    the service reduces the window count in that case rather than lying about it.
    """
    for total in (400, 600, 800, 1500):
        for windows in (2, 3, 4, 6):
            train, test = WalkForwardService._auto_split(total, windows, min_test=60)
            assert train >= 200
            assert test >= 60
            if train + test * windows <= total:
                continue
            # Not fitting is acceptable only when the minimums cannot fit either.
            assert 200 + 60 * windows > total


def test_walk_forward_produces_ordered_windows():
    service = WalkForwardService()
    report = service.run(
        SmaCross(),
        series(800),
        space=SPACE,
        symbol="TEST",
        timeframe="1d",
        objective="risk_adjusted",
        config=cfg(),
        windows=3,
        data_source="real",
    )
    assert report.windows
    for window in report.windows:
        assert window.train_start < window.train_end
        assert window.train_end == window.test_start
        assert window.test_start < window.test_end
        assert window.params
        # Degradation is train minus test, by definition.
        assert window.degradation_pct == pytest.approx(
            window.train_return_pct - window.test_return_pct, abs=1e-6
        )


def test_windows_are_disjoint_in_test_phase():
    service = WalkForwardService()
    report = service.run(
        SmaCross(),
        series(900),
        space=SPACE,
        symbol="TEST",
        timeframe="1d",
        config=cfg(),
        windows=3,
    )
    ends = [window.test_end for window in report.windows]
    starts = [window.test_start for window in report.windows]
    for i in range(1, len(starts)):
        assert starts[i] >= ends[i - 1]


def test_aggregate_is_internally_consistent():
    service = WalkForwardService()
    report = service.run(
        SmaCross(),
        series(900),
        space=SPACE,
        symbol="TEST",
        timeframe="1d",
        config=cfg(),
        windows=3,
    )
    agg = report.aggregate
    returns = [window.test_return_pct for window in report.windows]
    assert agg["windows_completed"] == len(returns)
    assert agg["positive_windows"] == sum(1 for value in returns if value > 0)
    assert agg["worst_test_return_pct"] == pytest.approx(min(returns), abs=1e-6)
    assert agg["best_test_return_pct"] == pytest.approx(max(returns), abs=1e-6)
    assert 0 <= agg["positive_pct"] <= 100


def test_rejects_too_few_bars():
    service = WalkForwardService()
    with pytest.raises(ValidationError) as exc:
        service.run(
            SmaCross(),
            series(100),
            space=SPACE,
            symbol="TEST",
            timeframe="1d",
            config=cfg(),
            windows=2,
        )
    assert str(MIN_TOTAL_BARS) in str(exc.value) or "至少需要" in str(exc.value)


def test_rejects_bad_window_count():
    service = WalkForwardService()
    with pytest.raises(ValidationError):
        service.run(
            SmaCross(),
            series(900),
            space=SPACE,
            symbol="TEST",
            timeframe="1d",
            config=cfg(),
            windows=1,
        )


def test_rejects_empty_space():
    service = WalkForwardService()
    with pytest.raises(ValidationError):
        service.run(
            SmaCross(),
            series(900),
            space={},
            symbol="TEST",
            timeframe="1d",
            config=cfg(),
            windows=3,
        )


def test_synthetic_source_is_flagged():
    service = WalkForwardService()
    report = service.run(
        SmaCross(),
        series(900),
        space=SPACE,
        symbol="TEST",
        timeframe="1d",
        config=cfg(),
        windows=3,
        data_source="synthetic",
    )
    assert any("合成行情" in warning for warning in report.warnings)


def test_manual_window_sizes_are_honoured():
    service = WalkForwardService()
    report = service.run(
        SmaCross(),
        series(1200),
        space=SPACE,
        symbol="TEST",
        timeframe="1d",
        config=cfg(),
        windows=2,
        train_bars=600,
        test_bars=200,
    )
    assert len(report.windows) == 2
    assert report.windows[0].train_end - report.windows[0].train_start == 600
    assert report.windows[0].test_end - report.windows[0].test_start == 200


def test_min_test_bars_is_respected():
    assert MIN_TEST_BARS >= 40


def test_test_window_is_sized_for_the_slowest_grid_member():
    """Warmup must be measured across the whole grid, not one sample.

    Regression (real data, 600036.SH daily, 800 bars): the test window was sized
    from {"fast": 5, "slow": 30} (warmup 31), so it came out at MIN_TEST_BARS = 50.
    The optimiser then picked slow=60 (warmup 61), the test slice could not warm
    up, and that window was dropped entirely — the report returned 3 windows while
    claiming 4 were requested, with no warning explaining the gap.
    """
    service = WalkForwardService()
    space = {"fast": [5, 10], "slow": [30, 60]}
    warmup = service._max_warmup(SmaCross(), space, {})
    assert warmup == SmaCross().warmup(SmaCross.Params(fast=5, slow=60))

    report = service.run(
        SmaCross(),
        series(800),
        space=space,
        symbol="TEST",
        timeframe="1d",
        config=cfg(),
        windows=4,
        data_source="real",
    )
    # Every requested window must survive: the split now reserves enough bars.
    assert report.aggregate["requested_windows"] == 4
    assert report.aggregate["windows_completed"] == 4
    for window in report.windows:
        assert window.test_end - window.test_start >= warmup + 3
    assert not any("未能完成验证" in warning for warning in report.warnings)


def test_max_warmup_handles_an_empty_space():
    service = WalkForwardService()
    assert service._max_warmup(SmaCross(), {}, {}) == SmaCross().warmup(
        SmaCross.Params()
    )


def test_skipped_windows_are_reported_when_they_happen(monkeypatch):
    """If a window is dropped anyway, the report must say so.

    Auto-sizing can still lose a window when the grid contains a combination that
    fails validation (so its warmup was never counted). Silence would make a
    partial validation look complete.
    """
    from app.modules.backtest.walkforward import WindowResult

    service = WalkForwardService()
    original = service.optimiser.optimise
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        result = original(*args, **kwargs)
        if result.best is not None and calls["n"] == 1:
            # First window optimises fine but its test slice cannot run.
            result.best.params = {"fast": 5, "slow": 60}
        return result

    monkeypatch.setattr(service.optimiser, "optimise", flaky)
    # Force a test window that warms up for slow=30 but not slow=60.
    report = service.run(
        SmaCross(),
        series(800),
        space=SPACE,
        symbol="TEST",
        timeframe="1d",
        config=cfg(),
        windows=3,
        train_bars=500,
        test_bars=52,   # enough for warmup 32, not for warmup 61
        data_source="real",
    )
    assert report.aggregate["windows_completed"] == 2
    assert report.aggregate["requested_windows"] == 3
    assert any("未能完成验证" in warning for warning in report.warnings)


def test_window_reduction_is_reported_to_the_caller():
    """A shortened run must say so; the message used to be overwritten."""
    service = WalkForwardService()
    # 800 bars with a fixed 300+90 layout fits 5 windows, not 6; the service
    # reduces the count and must say so.
    report = service.run(
        SmaCross(),
        series(800),
        space=SPACE,
        symbol="TEST",
        timeframe="1d",
        config=cfg(),
        windows=6,
        train_bars=300,
        test_bars=90,
        data_source="real",
    )
    assert report.aggregate["requested_windows"] == 6
    assert report.aggregate["windows_completed"] < 6
    assert any("数据只够完成" in warning for warning in report.warnings)
