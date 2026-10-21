from __future__ import annotations

import math

import pytest

from app.modules.backtest.engine import BacktestConfig, BacktestEngine
from app.modules.backtest.metrics import TradeRecord, compute_metrics
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.library.classics import BuyAndHold, SmaCross


def flat_candles(closes: list[float], opens: list[float] | None = None) -> list[CandlePoint]:
    candles = []
    for i, close in enumerate(closes):
        open_price = opens[i] if opens else (closes[i - 1] if i else close)
        candles.append(
            CandlePoint(
                time=i * 3_600_000,
                open=open_price,
                high=max(open_price, close) * 1.001,
                low=min(open_price, close) * 0.999,
                close=close,
                volume=100.0,
            )
        )
    return candles


def test_signals_execute_on_next_bar_open():
    # Index 1 signal -> entry at index 2 open (105), not at index 1 close.
    closes = [100, 101, 106, 108, 110, 112, 114, 116, 118, 120]
    opens = [100, 100, 105, 106, 108, 110, 112, 114, 116, 118]
    candles = flat_candles(closes, opens)
    config = BacktestConfig(initial_capital=10_000, fee_bps=0, slippage_bps=0, position_pct=1.0)
    result = BacktestEngine(config).run(BuyAndHold(), BuyAndHold.Params(), candles, "1h")
    assert len(result.trades) == 1
    assert result.trades[0].entry_price == pytest.approx(105.0)
    assert result.trades[0].entry_time == candles[2].time
    assert result.metrics["final_equity"] > 10_000


def test_no_trade_metrics_are_zero_safe():
    closes = [100.0] * 60
    candles = flat_candles(closes)
    config = BacktestConfig(initial_capital=10_000, fee_bps=0, slippage_bps=0)
    result = BacktestEngine(config).run(SmaCross(), SmaCross.Params(fast=5, slow=20), candles, "1h")
    assert result.trades == []
    assert result.metrics["trade_count"] == 0
    assert result.metrics["total_return_pct"] == pytest.approx(0.0)
    assert result.metrics["profit_factor"] is None
    assert result.metrics["max_drawdown_pct"] >= 0.0


def test_fees_and_pnl_conservation():
    closes = [100.0] * 20 + [120.0] * 20
    candles = flat_candles(closes)
    config = BacktestConfig(
        initial_capital=10_000, fee_bps=10, slippage_bps=0, position_pct=1.0
    )
    result = BacktestEngine(config).run(BuyAndHold(), BuyAndHold.Params(), candles, "1h")
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(100.0)
    # final_equity is rounded to cents for display; allow one cent of slack.
    assert result.metrics["final_equity"] == pytest.approx(10_000 + trade.pnl, abs=0.01)
    assert trade.pnl > 0


def test_stop_loss_caps_loss():
    closes = [100.0] * 20 + [70.0] + [70.0] * 10
    candles = flat_candles(closes)
    candles[20] = CandlePoint(
        time=candles[20].time,
        open=100.0,
        high=100.0,
        low=70.0,
        close=70.0,
        volume=100.0,
    )
    config = BacktestConfig(
        initial_capital=10_000,
        fee_bps=0,
        slippage_bps=0,
        position_pct=1.0,
        stop_loss_pct=0.05,
    )
    result = BacktestEngine(config).run(BuyAndHold(), BuyAndHold.Params(), candles, "1h")
    assert result.trades[0].reason == "stop_loss"
    assert result.trades[0].exit_price == pytest.approx(95.0)
    assert result.trades[0].pnl_pct < 0


def test_take_profit_trigger():
    closes = [100.0] * 20 + [130.0] * 10
    candles = flat_candles(closes)
    candles[20] = CandlePoint(
        time=candles[20].time,
        open=100.0,
        high=130.0,
        low=100.0,
        close=130.0,
        volume=100.0,
    )
    config = BacktestConfig(
        initial_capital=10_000,
        fee_bps=0,
        slippage_bps=0,
        position_pct=1.0,
        take_profit_pct=0.10,
    )
    result = BacktestEngine(config).run(BuyAndHold(), BuyAndHold.Params(), candles, "1h")
    assert result.trades[0].reason == "take_profit"
    assert result.trades[0].exit_price == pytest.approx(110.0)


def test_metrics_math():
    equity = [100.0, 110.0, 99.0, 120.0]
    trades = [
        TradeRecord(0, 1, 100, 110, 1, 10, 0.1, 0, "signal"),
        TradeRecord(1, 2, 110, 99, 1, -11, -0.1, 0, "stop_loss"),
    ]
    metrics = compute_metrics(
        initial=100.0, equity=equity, trades=trades, timeframe="1d", bars_in_market=2
    )
    assert metrics["total_return_pct"] == pytest.approx(20.0)
    assert metrics["max_drawdown_pct"] == pytest.approx(10.0)
    assert metrics["trade_count"] == 2
    assert metrics["win_rate_pct"] == pytest.approx(50.0)
    assert metrics["profit_factor"] == pytest.approx(10 / 11, rel=1e-4)
    assert metrics["exposure_pct"] == pytest.approx(50.0)


def test_avg_hold_converts_timestamps_to_bars():
    hour = 3_600_000
    trades = [
        TradeRecord(0, 3 * hour, 100, 101, 1, 1, 0.01, 0, "signal"),
        TradeRecord(3 * hour, 4 * hour, 101, 102, 1, 1, 0.01, 0, "signal"),
    ]
    metrics = compute_metrics(
        initial=100.0, equity=[100.0, 101.0, 102.0, 103.0, 104.0],
        trades=trades, timeframe="1h", bars_in_market=4,
    )
    assert metrics["avg_hold_bars"] == pytest.approx(2.0)


def test_warmup_insufficient_data():
    candles = flat_candles([100.0] * 10)
    with pytest.raises(ValueError):
        BacktestEngine().run(SmaCross(), SmaCross.Params(fast=5, slow=30), candles, "1h")


def test_implausible_bar_movement_is_reported():
    """Aggregate metrics only look at the endpoints; a mid-series collapse hides.

    Regression class: a settling-cash accounting error made equity drop ~95% on a
    sale bar and recover on release. Total return was unchanged, so nothing
    surfaced it until the per-bar series was inspected.
    """
    from app.modules.backtest.engine import extreme_bar_return

    assert extreme_bar_return([100.0, 101.0, 100.5, 102.0]) is None
    flagged = extreme_bar_return([100.0, 5.0, 100.0])
    assert flagged is not None
    index, pct = flagged
    assert index == 1
    assert pct == pytest.approx(-95.0, abs=0.1)


def test_backtest_warns_when_equity_jumps_implausibly():
    from app.modules.backtest.engine import BacktestConfig, BacktestEngine
    from app.modules.strategies.library.classics import BuyAndHold

    candles = [
        CandlePoint(
            time=i * 86_400_000,
            open=10.0, high=10.2, low=9.8, close=10.0, volume=1_000_000,
        )
        for i in range(30)
    ]
    cfg = BacktestConfig(
        initial_capital=100_000, fee_bps=0, slippage_bps=0, position_pct=0.5,
        market="crypto", tick_size=0.01, lot_size=0.0, t_plus_one=False,
    )
    result = BacktestEngine(cfg).run(BuyAndHold(), BuyAndHold.Params(), candles, "1d")
    # Flat prices and a 50% position: nothing implausible should be flagged.
    assert not any("超出正常价格波动" in warning for warning in result.metrics["warnings"])
