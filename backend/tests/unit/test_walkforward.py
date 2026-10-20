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
