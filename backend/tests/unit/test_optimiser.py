"""Grid-search optimiser: correctness, guards and overfitting warnings."""

from __future__ import annotations

import math

import pytest

from app.core.errors import ValidationError
from app.modules.backtest.engine import BacktestConfig
from app.modules.backtest.optimiser import (
    MAX_COMBINATIONS,
    MIN_BARS,
    OptimiserService,
    parse_space,
)
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.library.classics import SmaCross


def rising_candles(count: int = 600) -> list[CandlePoint]:
    out: list[CandlePoint] = []
    price = 100.0
    for i in range(count):
        drift = 0.0022 if i < count // 2 else -0.0012
        nxt = max(1.0, price * (1 + drift + math.sin(i / 5) * 0.004))
        out.append(
            CandlePoint(
                time=i * 86_400_000,
                open=price,
                high=max(price, nxt) * 1.003,
                low=min(price, nxt) * 0.997,
                close=nxt,
                volume=1_000_000,
            )
        )
        price = nxt
    return out


def cfg(**overrides) -> BacktestConfig:
    base = dict(
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
    base.update(overrides)
    return BacktestConfig(**base)


def test_build_grid_cartesian_product():
    grid = OptimiserService.build_grid(
        {"fast": [5, 10], "slow": [20, 30, 40]}, {"require_regime": False}
    )
    assert len(grid) == 6
    assert all("require_regime" in item for item in grid)
    assert {item["fast"] for item in grid} == {5, 10}


def test_build_grid_rejects_empty_space():
    with pytest.raises(ValidationError):
        OptimiserService.build_grid({}, {})


def test_build_grid_rejects_too_many_combinations():
    space = {f"p{i}": [1, 2, 3, 4, 5] for i in range(6)}  # 5^6 = 15625
    with pytest.raises(ValidationError) as exc:
        OptimiserService.build_grid(space, {})
    assert str(MAX_COMBINATIONS) in str(exc.value) or "超过上限" in str(exc.value)


def test_optimise_returns_ranked_candidates():
    service = OptimiserService()
    report = service.optimise(
        SmaCross(),
        rising_candles(),
        space={"fast": [5, 10, 15], "slow": [20, 30, 40], "require_regime": [True, False]},
        symbol="TEST",
        timeframe="1d",
        objective="risk_adjusted",
        config=cfg(),
    )
    assert report.tested == 18
    assert report.valid > 0
    assert report.best is not None
    assert report.top[0].score >= report.top[-1].score
    # This grid has no invalid combinations, so nothing is rejected.
    assert report.rejected == 0
    assert report.valid == report.tested


def test_invalid_param_combinations_are_rejected_not_fatal():
    """A grid that contains illegal combos must skip them, not abort."""
    service = OptimiserService()
    report = service.optimise(
        SmaCross(),
        rising_candles(500),
        # fast >= slow is illegal for this strategy.
        space={"fast": [10, 40], "slow": [20, 30]},
        symbol="TEST",
        timeframe="1d",
        objective="return",
        config=cfg(),
    )
    assert report.tested == 4
    assert report.rejected >= 1
    assert report.valid >= 1


def test_optimise_reports_in_and_out_of_sample():
    service = OptimiserService()
    report = service.optimise(
        SmaCross(),
        rising_candles(800),
        space={"fast": [5, 10], "slow": [20, 40]},
        symbol="TEST",
        timeframe="1d",
        objective="return",
        config=cfg(),
    )
    assert report.in_sample_bars > 0
    assert report.out_of_sample_bars > 0
    assert report.in_sample_bars + report.out_of_sample_bars == 800
    assert report.best is not None
    assert report.best.in_sample_return_pct != 0 or report.best.out_of_sample_return_pct != 0


def test_optimise_warns_on_wide_search():
    service = OptimiserService()
    space = {
        "fast": [5, 8, 10, 12, 15, 18, 20],
        "slow": [20, 25, 30, 35, 40, 50, 60, 80],
    }
    report = service.optimise(
        SmaCross(),
        rising_candles(700),
        space=space,
        symbol="TEST",
        timeframe="1d",
        objective="return",
        config=cfg(),
    )
    # 56 combinations is below the 100 threshold; widen further to trigger it.
    assert report.tested == 56
    space_wide = {
        "fast": [5, 8, 10, 12, 15, 18, 20, 25],
        "slow": [20, 25, 30, 35, 40, 50, 60, 80, 100, 120, 150, 180],
    }
    report_wide = service.optimise(
        SmaCross(),
        rising_candles(700),
        space=space_wide,
        symbol="TEST",
        timeframe="1d",
        objective="return",
        config=cfg(),
    )
    # 8 x 12 = 96 combinations: above the warning threshold of 100? No — but the
    # grid is capped at MAX_COMBINATIONS and the warning triggers at >=100, so use
    # a wider grid to exercise it.
    assert report_wide.tested == 96
    wider = {
        "fast": [5, 8, 10, 12, 15, 18, 20, 25, 30],
        "slow": [20, 25, 30, 35, 40, 50, 60, 80, 100, 120, 150, 180, 200],
    }
    report_wider = service.optimise(
        SmaCross(),
        rising_candles(700),
        space=wider,
        symbol="TEST",
        timeframe="1d",
        objective="return",
        config=cfg(),
    )
    assert report_wider.tested >= 100
    assert any("过拟合" in warning for warning in report_wider.warnings)


def test_optimise_warns_when_few_trades():
    service = OptimiserService()
    report = service.optimise(
        SmaCross(),
        rising_candles(400),
        space={"fast": [20], "slow": [100]},
        symbol="TEST",
        timeframe="1d",
        objective="return",
        config=cfg(),
    )
    if report.best and report.best.trade_count < 5:
        assert any("统计上不足" in warning for warning in report.warnings)


def test_optimise_requires_enough_bars():
    service = OptimiserService()
    with pytest.raises(ValidationError) as exc:
        service.optimise(
            SmaCross(),
            rising_candles(120),
            space={"fast": [5], "slow": [20]},
            symbol="TEST",
            timeframe="1d",
            config=cfg(),
        )
    assert str(MIN_BARS) in str(exc.value) or "至少需要" in str(exc.value)


def test_optimise_rejects_unknown_objective():
    service = OptimiserService()
    with pytest.raises(ValidationError):
        service.optimise(
            SmaCross(),
            rising_candles(400),
            space={"fast": [5], "slow": [20]},
            symbol="TEST",
            timeframe="1d",
            objective="magic",
            config=cfg(),
        )


def test_parse_space_validates_shapes():
    assert parse_space({"fast": [1, 2]}) == {"fast": [1, 2]}
    with pytest.raises(ValidationError):
        parse_space({"fast": 5})  # not a list
    with pytest.raises(ValidationError):
        parse_space([])  # not a dict


def test_distribution_reflects_grid():
    service = OptimiserService()
    report = service.optimise(
        SmaCross(),
        rising_candles(600),
        space={"fast": [5, 10, 15], "slow": [20, 30, 40]},
        symbol="TEST",
        timeframe="1d",
        objective="return",
        config=cfg(),
    )
    dist = report.distribution
    assert dist["profitable_count"] <= report.valid
    assert dist["best_return_pct"] >= dist["worst_return_pct"]
    assert dist["spread_pct"] >= 0


def test_synthetic_data_is_flagged():
    """Optimising on the synthetic feed must produce an explicit warning.

    The generator's trends are far too smooth: real runs showed +126% returns and
    a 100% profitable grid, which no real market produces.
    """
    service = OptimiserService()
    report = service.optimise(
        SmaCross(),
        rising_candles(600),
        space={"fast": [5, 10], "slow": [20, 30]},
        symbol="TEST",
        timeframe="1d",
        objective="return",
        config=cfg(),
        data_source="synthetic",
    )
    assert any("合成行情" in warning for warning in report.warnings)
    assert report.as_dict()["data_source"] == "synthetic"


def test_real_data_has_no_synthetic_warning():
    service = OptimiserService()
    report = service.optimise(
        SmaCross(),
        rising_candles(600),
        space={"fast": [5, 10], "slow": [20, 30]},
        symbol="TEST",
        timeframe="1d",
        objective="return",
        config=cfg(),
        data_source="real",
    )
    assert not any("合成行情" in warning for warning in report.warnings)


def test_large_in_sample_advantage_triggers_a_degradation_warning():
    """A positive out-of-sample result can still be a failure.

    Real data (600036.SH daily, 800 bars): best params scored +16.07% in-sample
    and +1.15% out-of-sample. The overfit check only fired when OOS was negative,
    so this 15-point collapse produced an empty warnings list while walk-forward
    would have flagged the same numbers.
    """
    from app.modules.backtest.optimiser import CandidateResult

    best = CandidateResult(
        params={"fast": 20, "slow": 60},
        total_return_pct=15.0,
        max_drawdown_pct=15.3,
        sharpe=0.4,
        trade_count=7,
        win_rate_pct=57.1,
        profit_factor=2.6,
        final_equity=230_000,
        score=1.18,
        in_sample_return_pct=16.07,
        out_of_sample_return_pct=1.15,
    )
    from app.modules.backtest.optimiser import OptimisationReport

    report = OptimisationReport(
        strategy="sma_cross", symbol="600036.SH", timeframe="1d", objective="return",
        bars=800, tested=6, valid=6, rejected=0,
    )
    warnings = OptimiserService._warnings(report, [best], enough_oos=True)
    joined = " ".join(warnings)
    assert "衰减" in joined
    assert "14.92" in joined


def test_small_degradation_does_not_warn():
    from app.modules.backtest.optimiser import CandidateResult, OptimisationReport

    best = CandidateResult(
        params={"fast": 5, "slow": 20}, total_return_pct=8.0, max_drawdown_pct=5.0,
        sharpe=0.9, trade_count=12, win_rate_pct=58.0, profit_factor=1.8,
        final_equity=108_000, score=1.6, in_sample_return_pct=10.0,
        out_of_sample_return_pct=6.0,
    )
    report = OptimisationReport(
        strategy="sma_cross", symbol="TEST", timeframe="1d", objective="return",
        bars=800, tested=6, valid=6, rejected=0,
    )
    warnings = OptimiserService._warnings(report, [best], enough_oos=True)
    assert not any("衰减" in warning for warning in warnings)
