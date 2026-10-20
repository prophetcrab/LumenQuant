"""Event-driven long-only backtester.

Timing rule: a signal produced at the close of bar i is executed at the open of
bar i+1. Stops are checked intrabar against low/high and filled at the stop
price (conservative, no optimistic gap handling beyond the open).

A-share mode adds the rules that decide whether a strategy is actually tradable:

* T+1 — a position opened on bar i cannot be sold before the next trading day;
* price limits — a bar opening at the limit-up price cannot be bought, and one
  opening at limit-down cannot be sold (no counterparty);
* asymmetric fees — commission floor, stamp duty on sells, transfer fee.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.money import round_money, round_price, round_qty
from app.modules.backtest.metrics import TradeRecord, compute_metrics
from app.modules.cn.fees import CnFeeSchedule
from app.modules.cn.rules import price_band, round_tick
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.base import Strategy, StrategyParams


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 100_000.0
    fee_bps: float = 10.0
    slippage_bps: float = 3.0
    position_pct: float = 0.95
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    market: str = "crypto"
    limit_pct: float = 0.0
    tick_size: float = 0.01
    lot_size: float = 0.0
    t_plus_one: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "BacktestConfig":
        raw = raw or {}
        market = str(raw.get("market", "crypto"))
        return cls(
            initial_capital=float(raw.get("initial_capital", 100_000)),
            fee_bps=float(raw.get("fee_bps", 10)),
            slippage_bps=float(raw.get("slippage_bps", 3)),
            position_pct=min(max(float(raw.get("position_pct", 0.95)), 0.01), 1.0),
            stop_loss_pct=max(float(raw.get("stop_loss_pct", 0.0)), 0.0),
            take_profit_pct=max(float(raw.get("take_profit_pct", 0.0)), 0.0),
            market=market,
            limit_pct=float(raw.get("limit_pct", 0.0)),
            tick_size=float(raw.get("tick_size", 0.01)),
            lot_size=float(raw.get("lot_size", 0.0)),
            t_plus_one=bool(raw.get("t_plus_one", market == "cn_stock")),
        )


@dataclass
class BacktestResult:
    equity_curve: list[dict] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    bars_in_market: int = 0
    blocked: dict = field(default_factory=dict)


@dataclass
class _OpenPosition:
    entry_time: int
    entry_price: float
    quantity: float
    entry_fee: float
    entry_day: str


class BacktestEngine:
    def __init__(
        self, config: BacktestConfig | None = None, *, cn_fees: CnFeeSchedule | None = None
    ) -> None:
        self.config = config or BacktestConfig()
        self.cn_fees = cn_fees or CnFeeSchedule()

    # ------------------------------------------------------------------ fees
    def _fee(self, side: str, notional: float) -> float:
        if self.config.market == "cn_stock":
            return self.cn_fees.total(side, notional)
        return round_money(notional * self.config.fee_bps / 10_000.0)

    def _band(self, prev_close: float):
        if self.config.market != "cn_stock" or self.config.limit_pct <= 0:
            return None
        return price_band(prev_close, "main", False, self.config.tick_size)

    def _day(self, ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------- run
    def run(
        self,
        strategy: Strategy,
        params: StrategyParams,
        candles: list[CandlePoint],
        timeframe: str,
    ) -> BacktestResult:
        if len(candles) < strategy.warmup(params) + 3:
            raise ValueError("not enough candles for warmup")
        state = strategy.prepare(candles, params)
        cfg = self.config
        cash = cfg.initial_capital
        position: _OpenPosition | None = None
        pending: str | None = None
        trades: list[TradeRecord] = []
        equity_points: list[dict] = []
        equity_values: list[float] = []
        bars_in_market = 0
        blocked = {"limit_up": 0, "limit_down": 0, "t_plus_one": 0}

        def buy_price(price: float) -> float:
            if cfg.market == "cn_stock":
                return round_tick(price, cfg.tick_size)
            return round_price(price * (1 + cfg.slippage_bps / 10_000))

        def sell_price(price: float) -> float:
            if cfg.market == "cn_stock":
                return round_tick(price, cfg.tick_size)
            return round_price(price * (1 - cfg.slippage_bps / 10_000))

        for index, candle in enumerate(candles):
            prev_bar = candles[index - 1] if index > 0 else candle
            band = self._band(prev_bar.close)
            at_limit_up = band is not None and candle.open >= band.up - cfg.tick_size / 2
            at_limit_down = band is not None and candle.open <= band.down + cfg.tick_size / 2

            # 1) Execute the previous bar's pending signal at this bar's open.
            if pending == "buy" and position is None:
                if at_limit_up:
                    blocked["limit_up"] += 1  # cannot buy a locked limit-up open
                else:
                    price = buy_price(candle.open)
                    budget = cash * cfg.position_pct
                    # Reserve the fee (which may be a fixed minimum) before sizing.
                    gross = budget / price
                    gross = (budget - self._fee("buy", gross * price)) / price
                    if cfg.lot_size > 0:
                        gross = math.floor(gross / cfg.lot_size) * cfg.lot_size
                    quantity = (
                        round_qty(gross) if cfg.lot_size > 0 else math.floor(gross * 1e8) / 1e8
                    )
                    if quantity > 0:
                        fee = self._fee("buy", quantity * price)
                        cost = round_money(quantity * price + fee)
                        if cost <= cash + 1e-9:
                            cash = round_money(cash - cost)
                            position = _OpenPosition(
                                candle.time, price, quantity, fee, self._day(candle.time)
                            )
            elif pending == "sell" and position is not None:
                if at_limit_down:
                    blocked["limit_down"] += 1
                elif cfg.t_plus_one and position.entry_day == self._day(candle.time):
                    blocked["t_plus_one"] += 1  # same-day sell not allowed
                else:
                    cash, trade = self._close(
                        position, sell_price(candle.open), candle.time, cash, "signal"
                    )
                    trades.append(trade)
                    position = None
            pending = None

            # 2) Intrabar protective exits.
            if position is not None:
                stop_price = (
                    position.entry_price * (1 - cfg.stop_loss_pct) if cfg.stop_loss_pct else None
                )
                target_price = (
                    position.entry_price * (1 + cfg.take_profit_pct)
                    if cfg.take_profit_pct
                    else None
                )
                exit_price = None
                reason = ""
                same_day = cfg.t_plus_one and position.entry_day == self._day(candle.time)
                if same_day:
                    # T+1 blocks every exit, including protective ones.
                    would_exit = (stop_price is not None and candle.low <= stop_price) or (
                        target_price is not None and candle.high >= target_price
                    )
                    if would_exit:
                        blocked["t_plus_one"] += 1
                    pass
                elif stop_price is not None and candle.low <= stop_price:
                    exit_price = min(stop_price, candle.open if candle.open < stop_price else stop_price)
                    reason = "stop_loss"
                elif target_price is not None and candle.high >= target_price:
                    exit_price = max(target_price, candle.open if candle.open > target_price else target_price)
                    reason = "take_profit"
                if exit_price is not None:
                    cash, trade = self._close(
                        position, sell_price(exit_price), candle.time, cash, reason
                    )
                    trades.append(trade)
                    position = None
                    pending = None

            # 3) Mark to market and collect the new signal.
            equity = cash + (position.quantity * candle.close if position else 0.0)
            equity = round_money(equity)
            equity_values.append(equity)
            equity_points.append({"time": candle.time, "equity": equity})
            if position is not None:
                bars_in_market += 1
            if index >= strategy.warmup(params):
                signal = strategy.evaluate(state, index)
                if signal.action == "buy" and position is None:
                    pending = "buy"
                elif signal.action == "sell" and position is not None:
                    pending = "sell"

        # Close any open position at the final close so metrics are comparable.
        if position is not None:
            if cfg.t_plus_one and position.entry_day == self._day(candles[-1].time):
                # Cannot legally exit; mark to market and leave it open.
                equity_values[-1] = round_money(
                    cash + position.quantity * candles[-1].close
                )
                equity_points[-1]["equity"] = equity_values[-1]
            else:
                cash, trade = self._close(
                    position, sell_price(candles[-1].close), candles[-1].time, cash, "end_of_data"
                )
                trades.append(trade)
                equity_values[-1] = round_money(cash)
                equity_points[-1]["equity"] = equity_values[-1]

        metrics = compute_metrics(
            initial=cfg.initial_capital,
            equity=equity_values,
            trades=trades,
            timeframe=timeframe,
            bars_in_market=bars_in_market,
            market=cfg.market,
        )
        metrics["blocked_orders"] = blocked
        return BacktestResult(
            equity_curve=equity_points,
            trades=trades,
            metrics=metrics,
            bars_in_market=bars_in_market,
            blocked=blocked,
        )

    def _close(
        self,
        position: _OpenPosition,
        price: float,
        time: int,
        cash: float,
        reason: str,
    ) -> tuple[float, TradeRecord]:
        notional = position.quantity * price
        fee = self._fee("sell", notional)
        proceeds = round_money(notional - fee)
        new_cash = round_money(cash + proceeds)
        pnl = round_money((price - position.entry_price) * position.quantity - fee - position.entry_fee)
        cost_basis = position.quantity * position.entry_price + position.entry_fee
        trade = TradeRecord(
            entry_time=position.entry_time,
            exit_time=time,
            entry_price=position.entry_price,
            exit_price=price,
            quantity=position.quantity,
            pnl=pnl,
            pnl_pct=pnl / cost_basis if cost_basis else 0.0,
            fee=round_money(fee + position.entry_fee),
            reason=reason,
        )
        return new_cash, trade
