"""Event-driven long-only backtester.

Timing rule: a signal produced at the close of bar i is executed at the open of
bar i+1. Stops are checked intrabar against low/high and filled at the stop
price (conservative, no optimistic gap handling beyond the open).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.core.money import round_money, round_price, round_qty
from app.modules.backtest.metrics import TradeRecord, compute_metrics
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

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "BacktestConfig":
        raw = raw or {}
        return cls(
            initial_capital=float(raw.get("initial_capital", 100_000)),
            fee_bps=float(raw.get("fee_bps", 10)),
            slippage_bps=float(raw.get("slippage_bps", 3)),
            position_pct=min(max(float(raw.get("position_pct", 0.95)), 0.01), 1.0),
            stop_loss_pct=max(float(raw.get("stop_loss_pct", 0.0)), 0.0),
            take_profit_pct=max(float(raw.get("take_profit_pct", 0.0)), 0.0),
        )


@dataclass
class BacktestResult:
    equity_curve: list[dict] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    bars_in_market: int = 0


@dataclass
class _OpenPosition:
    entry_time: int
    entry_price: float
    quantity: float
    entry_fee: float


class BacktestEngine:
    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

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

        def buy_price(price: float) -> float:
            return round_price(price * (1 + cfg.slippage_bps / 10_000))

        def sell_price(price: float) -> float:
            return round_price(price * (1 - cfg.slippage_bps / 10_000))

        for index, candle in enumerate(candles):
            # 1) Execute the previous bar's pending signal at this bar's open.
            if pending == "buy" and position is None:
                price = buy_price(candle.open)
                # Budget includes the entry fee; floor so cost can't exceed cash.
                fee_rate = cfg.fee_bps / 10_000.0
                raw_qty = (cash * cfg.position_pct) / (price * (1.0 + fee_rate))
                quantity = math.floor(raw_qty * 1e8) / 1e8
                if quantity > 0:
                    fee = round_money(quantity * price * cfg.fee_bps / 10_000)
                    cost = round_money(quantity * price + fee)
                    if cost <= cash + 1e-9:
                        cash = round_money(cash - cost)
                        position = _OpenPosition(candle.time, price, quantity, fee)
            elif pending == "sell" and position is not None:
                cash, trade = self._close(position, sell_price(candle.open), candle.time, cash, cfg, "signal")
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
                if stop_price is not None and candle.low <= stop_price:
                    exit_price = min(stop_price, candle.open if candle.open < stop_price else stop_price)
                    reason = "stop_loss"
                elif target_price is not None and candle.high >= target_price:
                    exit_price = max(target_price, candle.open if candle.open > target_price else target_price)
                    reason = "take_profit"
                if exit_price is not None:
                    cash, trade = self._close(
                        position, sell_price(exit_price), candle.time, cash, cfg, reason
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
            cash, trade = self._close(
                position, sell_price(candles[-1].close), candles[-1].time, cash, cfg, "end_of_data"
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
        )
        return BacktestResult(
            equity_curve=equity_points,
            trades=trades,
            metrics=metrics,
            bars_in_market=bars_in_market,
        )

    def _close(
        self,
        position: _OpenPosition,
        price: float,
        time: int,
        cash: float,
        cfg: BacktestConfig,
        reason: str,
    ) -> tuple[float, TradeRecord]:
        notional = position.quantity * price
        fee = round_money(notional * cfg.fee_bps / 10_000)
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
