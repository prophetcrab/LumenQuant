"""XGBoost hybrid strategy.

Three layers, each solving a different problem:

1. **XGBoost** estimates the probability that the next few bars move up by more
   than the round-trip cost. It is the only part that looks at many features at
   once.
2. **Regime filter** (long EMA) vetoes ML buys in a downtrend. Gradient-boosted
   trees are pattern matchers; a pattern that worked in a bull market can fire
   identically in a bear one.
3. **ATR / volatility gate** stands aside when the market is too quiet to cover
   costs or too violent to size safely.

The single most important rule here is that nothing may see the future. A
gradient-boosted model trained on the whole series and then backtested on it
produces spectacular, entirely fictional equity curves — it has already been shown
the answers. So training is strictly walk-forward:

    for each block of bars:
        train only on bars whose *label* is fully realised before the block
        predict the block, keep the predictions

A purged gap (`horizon` bars) separates train from test, because the label at bar
`i` depends on the price at `i + horizon`; without the gap the last training rows
would carry test-period information.

**Measured result — read this before tuning anything.** BTCUSDT 1h, 2019-01 to
2026-10, 19,999 bars, walk-forward, 16 retrain folds:

    out-of-sample IC (signed return)      +0.0008
    out-of-sample IC (|return|)           +0.2586
    per-fold IC range                     +0.083 … -0.102   (sign flips)
    round-trip cost                       26 bp (10 bp fee + 3 bp slippage, each way)
    backtest with real costs              -2.66 %
    backtest with zero costs              +0.03 %
    buy & hold, same window               +32.5 %

The model predicts **magnitude, not direction**: its probability correlates 0.26
with absolute future return and 0.0008 with signed return, and the top and bottom
deciles of predicted probability have nearly the same mean forward return (-0.3 bp
vs -1.9 bp). All 24 parameter combinations tried (horizon 4/12/24 × min hold
0/12/24/48 × entry 0.58/0.65) lost money, and at zero cost the return was flat.

So this is a **working implementation of the method, not a profitable strategy**.
It exists so the pipeline, the walk-forward discipline and the evaluation
methodology are available and testable. Do not deploy it with real money on the
strength of a backtest; the honest reading is that hourly BTC direction is not
predictable from these features at this cost level.
"""

from __future__ import annotations

import math
import warnings

import numpy as np
from pydantic import Field

from app.modules.market import indicators as ta
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.base import (
    Signal,
    Strategy,
    StrategyParams,
    StrategyState,
    hold,
)
from app.modules.strategies.registry import register

# Feature blocks that need the longest lookback before any value is defined.
_FEATURE_WARMUP = 220


@register
class XgbHybrid(Strategy):
    key = "xgb_hybrid"
    name = "XGBoost 混合策略"
    category = "ml"
    description = (
        "梯度提升树预测未来数根 K 线的上涨概率，叠加长期均线趋势过滤与波动率门控。"
        "训练严格走前向验证：每个区块只用标签已完全实现的样本训练，"
        "并在训练集与测试集之间留出与预测周期等长的隔离带。"
    )
    tags = ("机器学习", "XGBoost", "混合", "择时")

    class Params(StrategyParams):
        horizon: int = Field(
            default=4, ge=1, le=48, title="预测周期（根）",
            description="标签用的未来收益跨度；同时作为训练/测试隔离带的长度",
        )
        label_bps: float = Field(
            default=25.0, ge=0, le=500, title="上涨判定阈值（基点）",
            description="未来收益超过该值才算正样本；应覆盖往返成本",
        )
        min_train_bars: int = Field(
            default=4000, ge=800, le=20_000, title="首次训练最少样本",
        )
        retrain_every: int = Field(
            default=1000, ge=100, le=5000, title="重新训练间隔（根）",
        )
        n_estimators: int = Field(default=200, ge=20, le=800, title="树的数量")
        max_depth: int = Field(default=4, ge=2, le=10, title="树深度")
        learning_rate: float = Field(default=0.05, gt=0, le=0.5, title="学习率")
        entry_prob: float = Field(
            default=0.58, ge=0.5, le=0.95, title="开仓概率阈值",
        )
        exit_prob: float = Field(
            default=0.45, ge=0.05, le=0.65, title="平仓概率阈值",
            description="低于该概率离场；应低于开仓阈值以形成滞回，避免反复进出",
        )
        require_regime: bool = Field(
            default=True, title="只在长期均线上方做多",
        )
        regime_period: int = Field(
            default=200, ge=20, le=400, title="趋势过滤均线",
        )
        max_atr_pct: float = Field(
            default=6.0, ge=0.5, le=30.0, title="波动率上限（ATR%）",
            description="ATR/收盘价超过该值时空仓，避免在高波动中承受过大滑点",
        )
        min_hold_bars: int = Field(
            default=0, ge=0, le=72, title="最短持有（根）",
            description="入场后该根数内不响应离场信号；0 表示不限制。"
            "用于摊薄往返成本——短持有期的毛优势通常小于手续费加滑点",
        )

    # ------------------------------------------------------------------ prepare
    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        state = StrategyState(candles=candles, params=params)
        if len(candles) < _FEATURE_WARMUP + 50:
            state.series["prob"] = [None] * len(candles)
            return state

        features = self._build_features(candles)
        prob = self._walk_forward_probabilities(candles, features, params)

        state.series.update(features)
        state.series["prob"] = prob
        # Bookkeeping for the UI/audit: how many folds actually produced output.
        state.extra["folds"] = getattr(self, "_last_folds", 0)
        state.extra["cooldown"] = self._cooldown_mask(state.series, params)
        return state

    def _cooldown_mask(
        self, series: dict[str, list[float | None]], params: Params
    ) -> list[bool]:
        """Bars where an exit signal must be ignored, given a minimum hold.

        This is a deterministic simulation of the same state the execution layer
        would track, computed up front so `evaluate` has no memory. It follows the
        identical entry/exit rules; on a disagreement the effect is only a missed
        or early exit, never a fabricated entry.
        """
        n = len(series["prob"])
        if params.min_hold_bars <= 0 or n == 0:
            return [False] * n
        mask = [False] * n
        entry_at: int | None = None
        for i in range(n):
            prob = series["prob"][i]
            if prob is None:
                continue
            if entry_at is not None:
                if i - entry_at < params.min_hold_bars:
                    mask[i] = True
                    continue
                if prob <= params.exit_prob:
                    entry_at = None
                    continue
            if prob >= params.entry_prob:
                regime = series["regime_ma"][i]
                close = series["close"][i] if "close" in series else None
                if params.require_regime and regime and close is not None and close < regime:
                    continue
                entry_at = i
        return mask

    def _build_features(
        self, candles: list[CandlePoint]
    ) -> dict[str, list[float | None]]:
        """Every series here is causal: value at `i` uses bars at or before `i`."""
        n = len(candles)
        closes = ta.closes(candles)
        opens = [c.open for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        rsi = ta.rsi(closes, 14)
        macd_line = ta.macd(closes, 12, 26, 9)
        adx_block = ta.adx(candles, 14)
        atr = ta.atr(candles, 14)
        bb = ta.bollinger(closes, 20, 2.0)
        ema20 = ta.ema(closes, 20)
        ema50 = ta.ema(closes, 50)
        ema200 = ta.ema(closes, 200)

        def ret(lag: int) -> list[float | None]:
            out: list[float | None] = [None] * n
            for i in range(lag, n):
                prev = closes[i - lag]
                out[i] = (closes[i] / prev - 1.0) if prev else None
            return out

        r1 = ret(1)
        rolling_vol = [None] * n
        vol96 = [None] * n
        for i in range(24, n):
            window = [v for v in r1[i - 23 : i + 1] if v is not None]
            if len(window) >= 20:
                mean = sum(window) / len(window)
                rolling_vol[i] = math.sqrt(
                    sum((v - mean) ** 2 for v in window) / (len(window) - 1)
                )
        for i in range(96, n):
            window = [v for v in r1[i - 95 : i + 1] if v is not None]
            if len(window) >= 80:
                mean = sum(window) / len(window)
                vol96[i] = math.sqrt(
                    sum((v - mean) ** 2 for v in window) / (len(window) - 1)
                )

        volume_z = [None] * n
        for i in range(24, n):
            window = volumes[i - 23 : i + 1]
            mean = sum(window) / len(window)
            var = sum((v - mean) ** 2 for v in window) / max(1, len(window) - 1)
            sd = math.sqrt(var)
            volume_z[i] = (volumes[i] - mean) / sd if sd > 0 else 0.0

        # Intraday/calendar phase. Crypto has real hour-of-day and day-of-week
        # volume patterns; encoding them cyclically keeps 23:00 adjacent to 00:00.
        hour_sin: list[float | None] = [None] * n
        hour_cos: list[float | None] = [None] * n
        dow_sin: list[float | None] = [None] * n
        dow_cos: list[float | None] = [None] * n
        for i, candle in enumerate(candles):
            hours = (candle.time // 3_600_000) % 24
            days = (candle.time // 86_400_000) % 7
            hour_sin[i] = math.sin(2 * math.pi * hours / 24)
            hour_cos[i] = math.cos(2 * math.pi * hours / 24)
            dow_sin[i] = math.sin(2 * math.pi * days / 7)
            dow_cos[i] = math.cos(2 * math.pi * days / 7)

        features: dict[str, list[float | None]] = {
            "ret_1": r1,
            "ret_3": ret(3),
            "ret_6": ret(6),
            "ret_12": ret(12),
            "ret_24": ret(24),
            "rsi_14": rsi,
            "adx_14": adx_block["adx"],
            "vol_24": rolling_vol,
            "vol_ratio": [
                (rolling_vol[i] / vol96[i]) if rolling_vol[i] is not None and vol96[i] else None
                for i in range(n)
            ],
            "volume_z": volume_z,
            "hour_sin": hour_sin,
            "hour_cos": hour_cos,
            "dow_sin": dow_sin,
            "dow_cos": dow_cos,
        }

        # Normalised by price so the features are scale-free across BTC's range.
        features["macd_hist"] = [
            (v / closes[i]) if v is not None and closes[i] else None
            for i, v in enumerate(macd_line["histogram"])
        ]
        features["atr_pct"] = [
            (v / closes[i] * 100.0) if v is not None and closes[i] else None
            for i, v in enumerate(atr)
        ]
        features["bb_pctb"] = [
            ((closes[i] - bb["lower"][i]) / (bb["upper"][i] - bb["lower"][i]))
            if bb["upper"][i] is not None
            and bb["lower"][i] is not None
            and (bb["upper"][i] - bb["lower"][i]) > 0
            else None
            for i in range(n)
        ]
        features["bb_width"] = [
            ((bb["upper"][i] - bb["lower"][i]) / closes[i] * 100.0)
            if bb["upper"][i] is not None
            and bb["lower"][i] is not None
            and closes[i]
            else None
            for i in range(n)
        ]
        for label, series in (("dist_ema20", ema20), ("dist_ema50", ema50), ("dist_ema200", ema200)):
            features[label] = [
                (closes[i] / v - 1.0) if v else None for i, v in enumerate(series)
            ]

        # Candle anatomy: where the bar closed inside its own range, and wick
        # dominance. Both are backwards-looking by construction.
        features["body_ratio"] = [
            ((closes[i] - opens[i]) / (highs[i] - lows[i]))
            if (highs[i] - lows[i]) > 0
            else 0.0
            for i in range(n)
        ]
        features["upper_wick"] = [
            ((highs[i] - max(opens[i], closes[i])) / (highs[i] - lows[i]))
            if (highs[i] - lows[i]) > 0
            else 0.0
            for i in range(n)
        ]
        features["lower_wick"] = [
            ((min(opens[i], closes[i]) - lows[i]) / (highs[i] - lows[i]))
            if (highs[i] - lows[i]) > 0
            else 0.0
            for i in range(n)
        ]

        # The regime filter and the close used by the cooldown simulation live
        # here too, so `evaluate` stays a pure lookup.
        features["regime_ma"] = list(ema200)
        features["close"] = list(closes)

        return features

    # ------------------------------------------------------------ walk-forward
    def _walk_forward_probabilities(
        self,
        candles: list[CandlePoint],
        features: dict[str, list[float | None]],
        params: Params,
    ) -> list[float | None]:
        """Out-of-sample probability for every bar, or None before the first fold.

        Nothing produced here for bar `i` was informed by any bar after `i`.
        """
        try:
            import xgboost as xgb  # imported lazily: the app runs without ML deps
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "XGBoost 混合策略需要 xgboost，请执行 "
                "`pip install -r backend/requirements-ml.txt`"
            ) from exc

        n = len(candles)
        closes = ta.closes(candles)
        horizon = params.horizon
        feature_names = [
            name for name in features if name not in {"regime_ma", "close"}
        ]

        # Dense matrix + a validity mask (rows with any missing feature are dropped).
        matrix = np.full((n, len(feature_names)), np.nan, dtype=np.float32)
        for col, name in enumerate(feature_names):
            series = features[name]
            for i in range(n):
                value = series[i]
                if value is not None and math.isfinite(value):
                    matrix[i, col] = value
        valid = ~np.isnan(matrix).any(axis=1)

        # Label: did price rise by more than `label_bps` over `horizon` bars?
        labels = np.full(n, np.nan, dtype=np.float32)
        for i in range(n - horizon):
            base = closes[i]
            if base:
                forward = closes[i + horizon] / base - 1.0
                labels[i] = 1.0 if forward * 10_000.0 > params.label_bps else 0.0

        prob: list[float | None] = [None] * n
        folds = 0
        start = max(_FEATURE_WARMUP, params.min_train_bars)
        while start < n:
            end = min(start + params.retrain_every, n)
            # Purge: a training row at `i` needs closes[i + horizon] to be known
            # before the test block begins, so the training set stops short.
            train_end = start - horizon
            if train_end <= _FEATURE_WARMUP:
                start = end
                continue

            train_rows = np.arange(_FEATURE_WARMUP, train_end)
            train_rows = train_rows[valid[train_rows]]
            train_rows = train_rows[~np.isnan(labels[train_rows])]
            if len(train_rows) < 400:
                start = end
                continue

            y = labels[train_rows].astype(np.int8)
            if y.min() == y.max():
                # Degenerate window (no up-moves at all, or all up-moves): it
                # carries no information, so leave the block without predictions
                # rather than emitting a constant.
                start = end
                continue

            model = xgb.XGBClassifier(
                n_estimators=params.n_estimators,
                max_depth=params.max_depth,
                learning_rate=params.learning_rate,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_weight=5,
                reg_lambda=1.0,
                tree_method="hist",
                n_jobs=4,
                random_state=42,
                eval_metric="logloss",
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model.fit(matrix[train_rows], y)

            test_rows = np.arange(start, end)
            test_rows = test_rows[valid[test_rows]]
            if len(test_rows):
                preds = model.predict_proba(matrix[test_rows])[:, 1]
                for row, p in zip(test_rows, preds):
                    prob[int(row)] = float(p)
                folds += 1
            start = end

        self._last_folds = folds
        return prob

    # ----------------------------------------------------------------- evaluate
    def warmup(self, params: Params) -> int:
        # Feature warmup plus the first training block: no signal can exist before
        # the model has been fitted once.
        return _FEATURE_WARMUP + params.min_train_bars + params.horizon

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        params: XgbHybrid.Params = state.params  # type: ignore[assignment]
        prob = state.series["prob"][index]
        if prob is None:
            return hold("模型尚未覆盖该 K 线")

        closes = state.candles[index].close
        regime = state.series["regime_ma"][index]
        atr_pct = state.series["atr_pct"][index]

        # Volatility gate: costs and slippage scale with ATR, and an extreme
        # reading usually means an event rather than a tradeable trend.
        if atr_pct is not None and atr_pct > params.max_atr_pct:
            return hold(f"波动率过高 ATR {atr_pct:.2f}% > {params.max_atr_pct:.1f}%")

        # Cooldown after an entry, precomputed in `prepare` so `evaluate` stays a
        # pure function of (state, index). The edge being measured is tens of
        # basis points while a round trip costs 2 x (fee + slippage); flipping in
        # and out every few bars pays that cost repeatedly. This only suppresses
        # exits the model already wanted — it never invents a signal.
        cooldown = state.extra.get("cooldown")
        if isinstance(cooldown, list) and cooldown[index]:
            return hold(f"最短持有 {params.min_hold_bars} 根未满，暂不离场")

        if prob >= params.entry_prob:
            if params.require_regime and regime and closes < regime:
                return hold(
                    f"概率 {prob:.3f} 达标，但价格 {closes:.2f} 低于 "
                    f"EMA{params.regime_period} {regime:.2f}，趋势过滤否决"
                )
            return Signal("buy", f"XGB 概率 {prob:.3f} ≥ {params.entry_prob:.2f}")

        if prob <= params.exit_prob:
            return Signal("sell", f"XGB 概率 {prob:.3f} ≤ {params.exit_prob:.2f}")

        return hold(f"概率 {prob:.3f} 处于区间内")
