"""Nine classic strategies used as built-in templates.

Signals are long-only friendly (buy = enter/flat, sell = exit). The execution
layer decides whether a suggestion is actionable based on current holdings.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from app.modules.market import indicators as ta
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.base import (
    Signal,
    Strategy,
    StrategyParams,
    StrategyState,
    crossed_above,
    crossed_above_level,
    crossed_below,
    crossed_below_level,
    hold,
)
from app.modules.strategies.registry import register


@register
class SmaCross(Strategy):
    key = "sma_cross"
    name = "双均线交叉"
    category = "trend"
    description = "快线上穿慢线做多，下穿离场。趋势跟踪的基线策略。"
    tags = ("均线", "趋势", "入门")

    class Params(StrategyParams):
        fast: int = Field(default=10, ge=2, le=200, title="快线周期")
        slow: int = Field(default=30, ge=3, le=400, title="慢线周期")
        regime_period: int = Field(default=200, ge=20, le=400, title="牛熊过滤均线")
        require_regime: bool = Field(
            default=False,
            title="只在长期均线上方做多",
            description="开启后需约一年数据暖机；实测招商银行 800 日回测 12%→36%",
        )

        @model_validator(mode="after")
        def _fast_below_slow(self) -> "SmaCross.Params":
            if self.fast >= self.slow:
                raise ValueError("快线周期必须小于慢线周期")
            return self

    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        closes = ta.closes(candles)
        state = StrategyState(candles=candles, params=params)
        state.series["fast"] = ta.sma(closes, params.fast)
        state.series["slow"] = ta.sma(closes, params.slow)
        state.series["regime"] = ta.sma(closes, params.regime_period)
        return state

    def warmup(self, params: Params) -> int:
        base = params.slow + 1
        return max(base, params.regime_period + 2) if params.require_regime else base

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        fast = state.series["fast"]
        slow = state.series["slow"]
        if crossed_above(fast, slow, index):
            regime_ok = True
            if state.params.require_regime:
                regime = state.series["regime"][index]
                regime_ok = regime is not None and state.candles[index].close > regime
            if regime_ok:
                return Signal("buy", f"MA{state.params.fast} 上穿 MA{state.params.slow}")
        if crossed_below(fast, slow, index):
            return Signal("sell", f"MA{state.params.fast} 下穿 MA{state.params.slow}")
        return hold()


@register
class EmaTrend(Strategy):
    key = "ema_trend"
    name = "EMA 趋势过滤"
    category = "trend"
    description = "价格站上 EMA 且 ADX 高于阈值时入场，跌破 EMA 离场。用 ADX 过滤震荡。"
    tags = ("EMA", "ADX", "趋势")

    class Params(StrategyParams):
        ema_period: int = Field(default=50, ge=5, le=300, title="EMA 周期")
        adx_period: int = Field(default=14, ge=5, le=50, title="ADX 周期")
        adx_threshold: float = Field(default=20.0, ge=5, le=50, title="ADX 阈值")
        regime_period: int = Field(
            default=120, ge=20, le=400, title="牛熊过滤均线"
        )
        require_regime: bool = Field(
            default=True,
            title="只在长期均线上方做多",
            description="关闭后策略会在下跌市持续开多，回测胜率通常极低",
        )

    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        closes = ta.closes(candles)
        state = StrategyState(candles=candles, params=params)
        state.series["ema"] = ta.ema(closes, params.ema_period)
        state.series["adx"] = ta.adx(candles, params.adx_period)["adx"]
        # Long-term reference: a trend-following long strategy needs the market
        # itself to be in an uptrend. Without it the strategy keeps buying
        # pullbacks inside a bear market (4% win rate on real 茅台 data).
        state.series["regime"] = ta.sma(closes, params.regime_period)
        return state

    def warmup(self, params: Params) -> int:
        base = max(params.ema_period, params.adx_period * 2) + 2
        return max(base, params.regime_period + 2) if params.require_regime else base

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        closes = [c.close for c in state.candles]
        ema_line = state.series["ema"]
        adx_line = state.series["adx"]
        ema_value = ema_line[index]
        adx_value = adx_line[index]
        if ema_value is None or adx_value is None:
            return hold()
        prev_ema = ema_line[index - 1]
        if prev_ema is None:
            return hold()
        # Entry has two acceptable triggers so a trend that strengthens *after*
        # the crossing is not missed (the strict "cross + ADX together" rule can
        # leave a whole backtest with zero entries on real data):
        #   1. price crosses above EMA while ADX already confirms strength;
        #   2. price is already above EMA and ADX crosses the threshold.
        crossed_up = closes[index - 1] <= prev_ema < closes[index]
        above = closes[index] > ema_value
        prev_adx = adx_line[index - 1]
        adx_rising_through = (
            prev_adx is not None
            and prev_adx < state.params.adx_threshold <= adx_value
        )
        # ADX measures trend *strength*, not direction: a strong downtrend also
        # pushes it above the threshold. Requiring the EMA itself to be rising
        # keeps this a trend-following long entry instead of a falling-knife
        # buyer (real-data win rate was 4% without this check).
        ema_rising = prev_ema is not None and ema_value > prev_ema
        regime_ok = True
        if state.params.require_regime:
            regime = state.series["regime"][index]
            regime_ok = regime is not None and closes[index] > regime
        if regime_ok and (
            (crossed_up and adx_value >= state.params.adx_threshold)
            or (above and adx_rising_through and ema_rising)
        ):
            return Signal(
                "buy",
                f"价格位于上升 EMA{state.params.ema_period} 上方，ADX={adx_value:.1f}",
            )
        if closes[index - 1] >= prev_ema > closes[index]:
            return Signal("sell", "价格跌破 EMA")
        return hold()


@register
class RsiReversion(Strategy):
    key = "rsi_reversion"
    name = "RSI 均值回归"
    category = "mean_reversion"
    description = "RSI 自超卖区回升时买入，自超买区回落时卖出。适合震荡市。"
    tags = ("RSI", "均值回归", "震荡")

    class Params(StrategyParams):
        period: int = Field(default=14, ge=5, le=50, title="RSI 周期")
        oversold: float = Field(default=30.0, ge=5, le=45, title="超卖阈值")
        overbought: float = Field(default=70.0, ge=55, le=95, title="超买阈值")

    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        state = StrategyState(candles=candles, params=params)
        state.series["rsi"] = ta.rsi(ta.closes(candles), params.period)
        return state

    def warmup(self, params: Params) -> int:
        return params.period + 2

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        values = state.series["rsi"]
        if crossed_above_level(values, state.params.oversold, index):
            return Signal("buy", f"RSI 回到 {state.params.oversold:.0f} 上方")
        if crossed_below_level(values, state.params.overbought, index):
            return Signal("sell", f"RSI 跌破 {state.params.overbought:.0f}")
        return hold()


@register
class BollingerReversion(Strategy):
    key = "bollinger_reversion"
    name = "布林带回归"
    category = "mean_reversion"
    description = "收盘价跌破下轨后收回时买入，回到中轨上方时卖出。"
    tags = ("BOLL", "均值回归")

    class Params(StrategyParams):
        period: int = Field(default=20, ge=5, le=100, title="中轨周期")
        deviations: float = Field(default=2.0, ge=1.0, le=4.0, title="标准差倍数")

    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        state = StrategyState(candles=candles, params=params)
        bands = ta.bollinger(ta.closes(candles), params.period, params.deviations)
        state.series.update(bands)
        return state

    def warmup(self, params: Params) -> int:
        return params.period + 2

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        close = state.candles[index].close
        close_series = [c.close for c in state.candles]
        if crossed_above(close_series, state.series["lower"], index):
            return Signal("buy", "价格自下轨回归")
        if crossed_above(close_series, state.series["middle"], index):
            return Signal("sell", "价格回到中轨")
        if crossed_below(close_series, state.series["lower"], index):
            return hold("跌破下轨，等待收回")
        return hold()


@register
class MacdMomentum(Strategy):
    key = "macd_momentum"
    name = "MACD 动量"
    category = "momentum"
    description = "MACD 柱由负转正买入，由正转负卖出。捕捉中周期动量切换。"
    tags = ("MACD", "动量")

    class Params(StrategyParams):
        fast: int = Field(default=12, ge=3, le=50, title="快线")
        slow: int = Field(default=26, ge=5, le=100, title="慢线")
        signal: int = Field(default=9, ge=2, le=50, title="信号线")

        @model_validator(mode="after")
        def _fast_below_slow(self) -> "MacdMomentum.Params":
            if self.fast >= self.slow:
                raise ValueError("快线周期必须小于慢线周期")
            return self

    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        state = StrategyState(candles=candles, params=params)
        result = ta.macd(ta.closes(candles), params.fast, params.slow, params.signal)
        state.series.update(result)
        return state

    def warmup(self, params: Params) -> int:
        return params.slow + params.signal + 2

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        histogram = state.series["histogram"]
        if crossed_above_level(histogram, 0.0, index):
            return Signal("buy", "MACD 柱转正")
        if crossed_below_level(histogram, 0.0, index):
            return Signal("sell", "MACD 柱转负")
        return hold()


@register
class DonchianBreakout(Strategy):
    key = "donchian_breakout"
    name = "唐奇安通道突破"
    category = "breakout"
    description = "突破前 N 根最高价入场，跌破前 M 根最低价离场。海龟交易法的简化版。"
    tags = ("唐奇安", "突破", "海龟")

    class Params(StrategyParams):
        entry: int = Field(default=20, ge=5, le=200, title="入场通道")
        exit: int = Field(default=10, ge=3, le=100, title="离场通道")
        regime_period: int = Field(default=200, ge=20, le=400, title="牛熊过滤均线")
        require_regime: bool = Field(
            default=False,
            title="只在长期均线上方做多",
            description="开启后需约一年数据暖机；实测招商银行 7.7%→22%",
        )

    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        state = StrategyState(candles=candles, params=params)
        state.series["regime"] = ta.sma(ta.closes(candles), params.regime_period)
        upper: list[float | None] = [None] * len(candles)
        lower: list[float | None] = [None] * len(candles)
        for i in range(len(candles)):
            if i >= params.entry:
                window = candles[i - params.entry : i]
                upper[i] = max(c.high for c in window)
            if i >= params.exit:
                window = candles[i - params.exit : i]
                lower[i] = min(c.low for c in window)
        state.series["upper"] = upper
        state.series["lower"] = lower
        return state

    def warmup(self, params: Params) -> int:
        base = max(params.entry, params.exit) + 1
        return max(base, params.regime_period + 2) if params.require_regime else base

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        close = state.candles[index].close
        upper = state.series["upper"][index]
        lower = state.series["lower"][index]
        regime_ok = True
        if state.params.require_regime:
            regime = state.series["regime"][index]
            regime_ok = regime is not None and close > regime
        if upper is not None and close > upper and regime_ok:
            return Signal("buy", f"突破 {state.params.entry} 根高点 {upper:.2f}")
        if lower is not None and close < lower:
            return Signal("sell", f"跌破 {state.params.exit} 根低点 {lower:.2f}")
        return hold()


@register
class AtrChannel(Strategy):
    key = "atr_channel"
    name = "ATR 通道突破"
    category = "breakout"
    description = "以 EMA 为中枢、ATR 为带宽构建通道：上穿上轨买入，跌破中枢离场。"
    tags = ("ATR", "通道", "波动率")

    class Params(StrategyParams):
        ema_period: int = Field(default=20, ge=5, le=200, title="EMA 周期")
        atr_period: int = Field(default=14, ge=5, le=50, title="ATR 周期")
        multiplier: float = Field(default=2.0, ge=0.5, le=5.0, title="通道宽度倍数")

    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        state = StrategyState(candles=candles, params=params)
        closes = ta.closes(candles)
        center = ta.ema(closes, params.ema_period)
        atr_line = ta.atr(candles, params.atr_period)
        upper: list[float | None] = [None] * len(candles)
        lower: list[float | None] = [None] * len(candles)
        for i in range(len(candles)):
            if center[i] is not None and atr_line[i] is not None:
                upper[i] = float(center[i]) + params.multiplier * float(atr_line[i])  # type: ignore[arg-type]
                lower[i] = float(center[i]) - params.multiplier * float(atr_line[i])  # type: ignore[arg-type]
        state.series.update({"center": center, "atr": atr_line, "upper": upper, "lower": lower})
        return state

    def warmup(self, params: Params) -> int:
        return max(params.ema_period, params.atr_period) + 2

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        close_series = [c.close for c in state.candles]
        if crossed_above(close_series, state.series["upper"], index):
            return Signal("buy", "价格突破 ATR 上轨")
        if crossed_below(close_series, state.series["center"], index):
            return Signal("sell", "价格跌破中枢")
        return hold()


@register
class StochasticReversal(Strategy):
    key = "stochastic_reversal"
    name = "随机指标反转"
    category = "mean_reversion"
    description = "K 线自超卖区上穿时买入，自超买区下穿时卖出。"
    tags = ("KDJ", "随机指标", "反转")

    class Params(StrategyParams):
        period: int = Field(default=14, ge=5, le=50, title="K 周期")
        smooth: int = Field(default=3, ge=1, le=10, title="平滑周期")
        oversold: float = Field(default=20.0, ge=5, le=40, title="超卖阈值")
        overbought: float = Field(default=80.0, ge=60, le=95, title="超买阈值")

    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        state = StrategyState(candles=candles, params=params)
        result = ta.stochastic(candles, params.period, params.smooth)
        state.series.update({"k": result["k"], "d": result["d"]})
        return state

    def warmup(self, params: Params) -> int:
        return params.period + params.smooth + 2

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        k = state.series["k"]
        if crossed_above_level(k, state.params.oversold, index):
            return Signal("buy", f"K 线回到 {state.params.oversold:.0f} 上方")
        if crossed_below_level(k, state.params.overbought, index):
            return Signal("sell", f"K 线跌破 {state.params.overbought:.0f}")
        return hold()


@register
class BuyAndHold(Strategy):
    key = "buy_and_hold"
    name = "买入持有"
    category = "benchmark"
    description = "首根可用 K 线买入并持有，作为其它策略的对照基准。"
    tags = ("基准", "被动")

    class Params(StrategyParams):
        pass

    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        return StrategyState(candles=candles, params=params)

    def warmup(self, params: Params) -> int:
        return 1

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        # Emit on every bar from warmup onward rather than only at index==1:
        # the runtime evaluates just the latest bar, so a "fire once at the start"
        # signal would never reach a live/paper instance. Position state decides
        # whether this actually opens anything.
        if index >= 1:
            return Signal("buy", "基准建仓")
        return hold()
