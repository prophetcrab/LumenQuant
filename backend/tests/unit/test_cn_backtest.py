from __future__ import annotations

import pytest

from app.modules.backtest.engine import BacktestConfig, BacktestEngine
from app.modules.backtest.metrics import CN_TRADING_DAYS, periods_per_year
from app.modules.cn.fees import CnFeeSchedule
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.library.classics import BuyAndHold

DAY = 86_400_000


def daily_candles(closes: list[float], opens: list[float] | None = None) -> list[CandlePoint]:
    out = []
    for i, close in enumerate(closes):
        open_price = opens[i] if opens else (closes[i - 1] if i else close)
        out.append(
            CandlePoint(
                time=i * DAY,
                open=open_price,
                high=max(open_price, close) * 1.005,
                low=min(open_price, close) * 0.995,
                close=close,
                volume=1_000_000,
            )
        )
    return out


def cn_config(**overrides) -> BacktestConfig:
    base = dict(
        initial_capital=100_000,
        fee_bps=0,
        slippage_bps=0,
        position_pct=1.0,
        market="cn_stock",
        limit_pct=0.10,
        tick_size=0.01,
        lot_size=100,
        t_plus_one=True,
    )
    base.update(overrides)
    return BacktestConfig(**base)


def test_buy_quantity_is_whole_lots():
    candles = daily_candles([10.0] * 20 + [10.5] * 5)
    result = BacktestEngine(cn_config(), cn_fees=CnFeeSchedule()).run(
        BuyAndHold(), BuyAndHold.Params(), candles, "1d"
    )
    trade = result.trades[0]
    assert trade.quantity % 100 == 0
    assert trade.quantity > 0


def test_t_plus_one_blocks_same_day_exit():
    # Buy at bar 2 open; a same-bar crash cannot be stopped out under T+1.
    candles = daily_candles([10.0] * 8, opens=[10.0] * 8)
    candles[2] = CandlePoint(candles[2].time, 10.0, 10.0, 8.0, 8.0, 1_000_000)
    config = cn_config(stop_loss_pct=0.05)
    result = BacktestEngine(config, cn_fees=CnFeeSchedule()).run(
        BuyAndHold(), BuyAndHold.Params(), candles, "1d"
    )
    # T+1 prevents the protective exit on the entry day.
    assert result.metrics["blocked_orders"]["t_plus_one"] >= 1 or not result.trades


def test_limit_up_open_blocks_buy():
    # Second bar opens at +10%: a locked limit-up open cannot be bought.
    closes = [10.0, 10.0, 11.0, 11.0, 11.0]
    opens = [10.0, 10.0, 11.0, 11.0, 11.0]
    candles = daily_candles(closes, opens)
    result = BacktestEngine(cn_config(), cn_fees=CnFeeSchedule()).run(
        BuyAndHold(), BuyAndHold.Params(), candles, "1d"
    )
    assert result.metrics["blocked_orders"]["limit_up"] >= 1
    assert result.trades == []


def test_limit_down_open_blocks_sell():
    closes = [10.0, 10.0, 10.0, 9.0, 9.0]
    opens = [10.0, 10.0, 10.0, 9.0, 9.0]
    candles = daily_candles(closes, opens)

    class AlwaysSell(BuyAndHold):
        key = "test_always_sell"
        name = "test"

        def evaluate(self, state, index):
            from app.modules.strategies.base import Signal

            return Signal("buy") if index == 1 else Signal("sell")

    result = BacktestEngine(cn_config(), cn_fees=CnFeeSchedule()).run(
        AlwaysSell(), AlwaysSell.Params(), candles, "1d"
    )
    assert result.metrics["blocked_orders"]["limit_down"] >= 1


def test_native_daily_annualization():
    assert periods_per_year("1d", "cn_stock") == float(CN_TRADING_DAYS)
    assert periods_per_year("15m", "cn_stock") == float(CN_TRADING_DAYS * 16)
    assert periods_per_year("1d", "crypto") == 365.0


def test_cn_fees_reduce_pnl_vs_free():
    candles = daily_candles([10.0] * 5 + [12.0] * 5)
    free = BacktestEngine(cn_config(), cn_fees=CnFeeSchedule(commission_bps=0, min_commission=0, stamp_duty_bps=0, transfer_fee_bps=0)).run(
        BuyAndHold(), BuyAndHold.Params(), candles, "1d"
    )
    paid = BacktestEngine(cn_config(), cn_fees=CnFeeSchedule()).run(
        BuyAndHold(), BuyAndHold.Params(), candles, "1d"
    )
    assert paid.metrics["final_equity"] < free.metrics["final_equity"]
    assert paid.trades[0].fee > free.trades[0].fee
