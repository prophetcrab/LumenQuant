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
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.money import round_money, round_price, round_qty
from app.modules.backtest.metrics import TradeRecord, compute_metrics
from app.modules.cn.fees import CnFeeSchedule
from app.modules.cn.rules import price_band, round_tick
from app.modules.market.service_types import CandlePoint
from app.modules.risk.stops import StopRules, evaluate
from app.modules.strategies.base import Strategy, StrategyParams


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 100_000.0
    fee_bps: float = 10.0
    slippage_bps: float = 3.0
    position_pct: float = 0.95
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    trailing_stop_pct: float = 0.0
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
            trailing_stop_pct=max(float(raw.get("trailing_stop_pct", 0.0)), 0.0),
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
    peak_price: float = 0.0


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

    def _next_day(self, ts_ms: int) -> str:
        """Day on which A-share sale proceeds become spendable.

        Offsets by one calendar day; bars only exist on trading days, so the next
        bar's key is always greater and the release happens on its own schedule
        without needing the exchange calendar here.
        """
        day = datetime.fromtimestamp(ts_ms / 1000, timezone.utc) + timedelta(days=1)
        return day.strftime("%Y-%m-%d")

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
        # A-share proceeds settle T+1: sale money is in the ledger but cannot fund
        # a new purchase until the next trading day. `settling` holds
        # (release_day, amount) pairs; `spendable` is what entries may use.
        settling: list[tuple[str, float]] = []
        position: _OpenPosition | None = None
        pending: str | None = None
        trades: list[TradeRecord] = []
        equity_points: list[dict] = []
        equity_values: list[float] = []
        bars_in_market = 0
        stop_rules = StopRules(
            stop_loss_pct=cfg.stop_loss_pct,
            take_profit_pct=cfg.take_profit_pct,
            trailing_stop_pct=cfg.trailing_stop_pct,
        )
        blocked = {
            "limit_up": 0,
            "limit_down": 0,
            "t_plus_one": 0,
            "insufficient_capital": 0,
        }
        warnings: list[str] = []
        min_lot_cost: float | None = None

        def buy_price(price: float) -> float:
            if cfg.market == "cn_stock":
                return round_tick(price, cfg.tick_size)
            return round_price(price * (1 + cfg.slippage_bps / 10_000))

        def sell_price(price: float) -> float:
            if cfg.market == "cn_stock":
                return round_tick(price, cfg.tick_size)
            return round_price(price * (1 - cfg.slippage_bps / 10_000))

        for index, candle in enumerate(candles):
            today_key = self._day(candle.time)
            if settling and cfg.t_plus_one:
                still_held = []
                for release_day, amount in settling:
                    if release_day <= today_key:
                        cash = round_money(cash + amount)
                    else:
                        still_held.append((release_day, amount))
                settling = still_held
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
                    spendable = round_money(
                        cash - sum(amount for _day, amount in settling)
                    )
                    if spendable <= 0:
                        blocked["insufficient_capital"] += 1
                        pending = None
                        continue
                    budget = spendable * cfg.position_pct
                    # Reserve the fee (which may be a fixed minimum) before sizing.
                    gross = budget / price
                    gross = (budget - self._fee("buy", gross * price)) / price
                    if cfg.lot_size > 0:
                        gross = math.floor(gross / cfg.lot_size) * cfg.lot_size
                    quantity = (
                        round_qty(gross) if cfg.lot_size > 0 else math.floor(gross * 1e8) / 1e8
                    )
                    if cfg.lot_size > 0:
                        # One board lot may simply cost more than the capital.
                        # Silence here used to yield "0 trades, 0% return" with no
                        # explanation, which reads as a broken strategy.
                        lot_cost = cfg.lot_size * price
                        min_lot_cost = lot_cost if min_lot_cost is None else min(min_lot_cost, lot_cost)
                        if quantity <= 0 and lot_cost > cash:
                            blocked["insufficient_capital"] += 1
                    if quantity > 0:
                        fee = self._fee("buy", quantity * price)
                        cost = round_money(quantity * price + fee)
                        if cost <= cash + 1e-9:
                            cash = round_money(cash - cost)
                            position = _OpenPosition(
                                candle.time, price, quantity, fee, self._day(candle.time),
                                peak_price=price,
                            )
            elif pending == "sell" and position is not None:
                if at_limit_down:
                    blocked["limit_down"] += 1
                elif cfg.t_plus_one and position.entry_day == self._day(candle.time):
                    blocked["t_plus_one"] += 1  # same-day sell not allowed
                else:
                    proceeds_before = cash
                    cash, trade = self._close(
                        position, sell_price(candle.open), candle.time, cash, "signal"
                    )
                    trades.append(trade)
                    position = None
                    if cfg.t_plus_one:
                        # Park the proceeds until the next trading day.
                        settled = round_money(cash - proceeds_before)
                        cash = proceeds_before
                        settling.append((self._next_day(candle.time), settled))
            pending = None

            # 2) Intrabar protective exits (fixed stop, trailing stop, target).
            if position is not None:
                same_day = cfg.t_plus_one and position.entry_day == self._day(candle.time)
                decision = evaluate(
                    entry_price=position.entry_price,
                    current_peak=position.peak_price,
                    bar_open=candle.open,
                    bar_high=candle.high,
                    bar_low=candle.low,
                    rules=stop_rules,
                )
                if same_day:
                    # T+1 blocks every exit, including protective ones.
                    if decision.triggered:
                        blocked["t_plus_one"] += 1
                else:
                    position.peak_price = decision.new_peak
                    if decision.triggered:
                        proceeds_before = cash
                        cash, trade = self._close(
                            position, sell_price(decision.exit_price), candle.time, cash,
                            decision.reason,
                        )
                        trades.append(trade)
                        position = None
                        pending = None
                        if cfg.t_plus_one:
                            settled = round_money(cash - proceeds_before)
                            cash = proceeds_before
                            settling.append((self._next_day(candle.time), settled))

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
        if blocked["insufficient_capital"] and not trades:
            warnings.append(
                f"资金不足以买入一手：一手 {cfg.lot_size:g} 股约需 "
                f"{min_lot_cost:,.2f}，初始资金 {cfg.initial_capital:,.2f}。"
                "请提高初始资金或换用单价更低的标的。"
            )
        if not trades and not warnings:
            warnings.append("区间内没有产生可执行交易（策略未触发或被规则拦截）。")
        metrics["blocked_orders"] = blocked
        metrics["warnings"] = warnings
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
