"""Benchmark comparison: alpha/beta/capture maths and verdict wording."""

from __future__ import annotations

import math

import pytest

from app.modules.backtest.benchmark import (
    _verdict,
    compare,
    equity_option_points,
)


def equity_from_returns(returns: list[float], start: float = 100_000.0) -> list[float]:
    values = [start]
    for r in returns:
        values.append(values[-1] * (1 + r))
    return values


def test_buy_and_hold_has_near_unit_beta():
    """A fully invested strategy tracking the asset must look like the asset."""
    prices = [100.0]
    for i in range(200):
        prices.append(prices[-1] * (1 + 0.002 * math.sin(i / 3)))
    # Equity mirrors the price path (same returns).
    equity = [100_000.0]
    for i in range(1, len(prices)):
        equity.append(equity[-1] * (prices[i] / prices[i - 1]))
    result = compare(strategy_equity=equity, prices=prices, periods_per_year=252)
    assert result.beta == pytest.approx(1.0, abs=0.01)
    assert result.correlation == pytest.approx(1.0, abs=0.01)
    assert result.excess_return_pct == pytest.approx(0.0, abs=0.01)
    assert result.tracking_error_pct == pytest.approx(0.0, abs=0.01)


def test_flat_strategy_has_zero_beta_and_no_tracking_error():
    prices = [100.0] * 50
    equity = [100_000.0] * 50
    result = compare(strategy_equity=equity, prices=prices, periods_per_year=252)
    assert result.beta == 0.0
    assert result.strategy_vol_pct == 0.0
    assert result.tracking_error_pct == 0.0
    assert result.excess_return_pct == pytest.approx(0.0)


def test_excess_return_is_strategy_minus_benchmark():
    prices = [100.0, 110.0, 121.0]  # +21%
    equity = [100_000.0, 105_000.0, 112_000.0]  # +12%
    result = compare(strategy_equity=equity, prices=prices, periods_per_year=252)
    assert result.benchmark_return_pct == pytest.approx(21.0)
    assert result.strategy_return_pct == pytest.approx(12.0)
    assert result.excess_return_pct == pytest.approx(-9.0)


def test_capture_ratios_behave():
    # Benchmark rises then falls; strategy takes half the upside and half the downside.
    bench = [0.02, 0.02, -0.02, -0.02]
    prices = equity_from_returns(bench, start=100.0)
    strat = [r * 0.5 for r in bench]
    equity = equity_from_returns(strat, start=100_000.0)
    result = compare(strategy_equity=equity, prices=prices, periods_per_year=252)
    assert result.up_capture_pct == pytest.approx(50.0, abs=2.0)
    assert result.down_capture_pct == pytest.approx(50.0, abs=2.0)


def test_excess_win_rate_counts_outperforming_bars():
    prices = [100.0, 102.0, 101.0, 103.0, 104.0]
    equity = [100.0, 101.0, 102.0, 103.0, 104.0]
    result = compare(strategy_equity=equity, prices=prices, periods_per_year=252)
    # Bar returns: bench +2.0%, -0.98%, +1.98%, +0.97%
    #               strat +1.0%, +0.99%, +0.98%, +0.97%
    # The strategy beats the benchmark on the down bar and loses on every up bar.
    assert result.excess_win_rate_pct == pytest.approx(25.0)


def test_short_series_is_reported_not_crashed():
    result = compare(strategy_equity=[100.0], prices=[100.0], periods_per_year=252)
    assert result.verdict == "样本不足"
    assert result.periods == 0


def test_verdict_distinguishes_underperformance():
    # The exact case that was mis-判 earlier: -18% excess with slightly positive alpha.
    assert "跑输基准" in _verdict(-18.11, 0.46, 0.41, -0.37)
    assert "跑输基准" in _verdict(-23.49, -0.69, 0.37, -0.45)


def test_verdict_recognises_alpha_from_beta():
    assert "更高暴露" in _verdict(8.0, -2.0, 1.4, 0.2)
    assert "alpha为正" in _verdict(8.0, 3.0, 0.6, 0.8)


def test_verdict_flags_noise_and_low_exposure():
    assert "差异不显著" in _verdict(0.03, 0.32, 0.95, -0.16)
    assert "仓位暴露很低" in _verdict(1.0, 0.1, 0.1, 0.0)


def test_equity_option_points_rebase_to_100():
    points = equity_option_points(
        equity=[100_000, 110_000, 120_000],
        prices=[100.0, 105.0, 115.0],
        times=[1, 2, 3],
    )
    assert points[0]["strategy"] == pytest.approx(100.0)
    assert points[0]["benchmark"] == pytest.approx(100.0)
    assert points[-1]["strategy"] == pytest.approx(120.0)
    assert points[-1]["benchmark"] == pytest.approx(115.0)


def test_equity_option_points_handles_misalignment():
    points = equity_option_points(equity=[100.0, 110.0], prices=[100.0, 105.0, 110.0], times=[1, 2])
    assert len(points) == 2


def test_verdict_receives_percent_units(database=None):
    """Regression: verdict thresholds are in percent but compare() passed fractions.

    -18% excess was classified as "差异不显著（±5% 以内）" because the fraction
    (-0.18) was compared against -5.
    """
    prices = [100.0, 110.0, 121.0]
    equity = [100_000.0, 102_000.0, 103_000.0]  # +3% vs +21%
    result = compare(strategy_equity=equity, prices=prices, periods_per_year=252)
    assert result.excess_return_pct == pytest.approx(-18.0, abs=0.5)
    assert "跑输基准" in result.verdict, result.verdict


def test_verdict_outperformance_uses_percent_units():
    prices = [100.0, 105.0, 110.0]  # +10%
    equity = [100_000.0, 108_000.0, 121_000.0]  # +21%
    result = compare(strategy_equity=equity, prices=prices, periods_per_year=252)
    assert result.excess_return_pct == pytest.approx(11.0, abs=0.5)
    assert "跑赢基准" in result.verdict, result.verdict


def test_verdict_does_not_claim_exposure_when_flat():
    """A never-traded strategy beating a falling benchmark is not skill.

    Real-data case: 600519 daily, 400 bars, capital 100k, one board lot costs
    132k -> zero trades, 0% return, benchmark -11.58%. alpha and beta are both 0.
    The old wording said the excess came from 更高暴露 (higher exposure) while the
    strategy was 100% cash.
    """
    verdict = _verdict(11.58, 0.0, 0.0, 0.26)
    assert "空仓" in verdict
    assert "更高暴露" not in verdict

    # A genuinely levered-but-skill-less strategy keeps the old message.
    assert "更高暴露" in _verdict(8.0, -2.0, 1.4, 0.2)
