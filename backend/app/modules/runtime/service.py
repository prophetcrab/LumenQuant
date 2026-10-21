"""Strategy instance lifecycle and one-bar evaluation.

The runner is deliberately small: it converts a strategy signal into an order
through the trading module. All execution rules (risk, fees, fills) stay there.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.timeutil import ensure_utc, iso, utcnow
from app.modules.market.service import SUPPORTED_TIMEFRAMES, MarketService
from app.modules.runtime.models import RuntimeLog, StrategyInstance, new_id
from app.modules.risk.stops import StopRules, evaluate_tick, parse_rules
from app.modules.strategies.registry import get
from app.modules.trading.service import TradingService

MAX_LOGS = 200
# Hard cap on rows kept per instance; the API never returns more than MAX_LOGS,
# so anything beyond this is pure disk growth.
MAX_LOGS_PER_INSTANCE = 2000
# A repeated identical condition is logged at most once per this many seconds.
LOG_DEDUPE_SECONDS = 300


@dataclass(frozen=True)
class InstanceView:
    id: str
    account_id: str
    strategy_key: str
    strategy_name: str
    symbol: str
    timeframe: str
    params: dict
    position_pct: float
    max_order_notional: float
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    peak_price: float
    running: bool
    last_bar_time: int
    last_signal_at: str
    last_error: str
    executions: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class LogView:
    id: str
    instance_id: str
    level: str
    event: str
    message: str
    order_id: str
    bar_time: int
    created_at: str


class RuntimeService:
    def __init__(
        self,
        session: Session,
        *,
        accounts_service,
        market: MarketService,
        trading: TradingService,
        execution_price=None,
        live_auto_trading_enabled: bool = False,
    ) -> None:
        self.session = session
        self.accounts = accounts_service
        self.market = market
        self.trading = trading
        self.execution_price = execution_price
        # Automated orders on a live account need their own opt-in; manual live
        # orders are gated separately by the X-Live-Confirm header.
        self.live_auto_trading_enabled = live_auto_trading_enabled

    # ------------------------------------------------------------------ read
    def _require(self, instance_id: str) -> StrategyInstance:
        row = self.session.get(StrategyInstance, instance_id)
        if row is None:
            raise NotFoundError(f"strategy instance {instance_id} not found")
        return row

    def _view(self, row: StrategyInstance) -> InstanceView:
        try:
            name = get(row.strategy_key).name
        except NotFoundError:
            name = row.strategy_key
        return InstanceView(
            id=row.id,
            account_id=row.account_id,
            strategy_key=row.strategy_key,
            strategy_name=name,
            symbol=row.symbol,
            timeframe=row.timeframe,
            params=json.loads(row.params_json),
            position_pct=row.position_pct,
            max_order_notional=row.max_order_notional,
            stop_loss_pct=row.stop_loss_pct,
            take_profit_pct=row.take_profit_pct,
            trailing_stop_pct=row.trailing_stop_pct,
            peak_price=row.peak_price,
            running=row.running,
            last_bar_time=row.last_bar_time,
            last_signal_at=iso(row.last_signal_at),
            last_error=row.last_error,
            executions=row.executions,
            created_at=iso(row.created_at),
            updated_at=iso(row.updated_at),
        )

    def list(self, *, account_id: str | None = None) -> list[InstanceView]:
        stmt = select(StrategyInstance).order_by(StrategyInstance.created_at.desc())
        if account_id:
            stmt = stmt.where(StrategyInstance.account_id == account_id)
        return [self._view(row) for row in self.session.scalars(stmt).all()]

    def logs(self, instance_id: str, *, limit: int = 100) -> list[LogView]:
        self._require(instance_id)
        rows = self.session.scalars(
            select(RuntimeLog)
            .where(RuntimeLog.instance_id == instance_id)
            .order_by(RuntimeLog.created_at.desc())
            .limit(max(1, min(limit, MAX_LOGS)))
        ).all()
        return [
            LogView(
                id=row.id,
                instance_id=row.instance_id,
                level=row.level,
                event=row.event,
                message=row.message,
                order_id=row.order_id,
                bar_time=row.bar_time,
                created_at=iso(row.created_at),
            )
            for row in rows
        ]

    # ----------------------------------------------------------------- write
    def create(
        self,
        *,
        account_id: str,
        strategy_key: str,
        symbol: str,
        timeframe: str,
        params: dict | None,
        position_pct: float = 0.25,
        max_order_notional: float = 50_000.0,
        stop_loss_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        trailing_stop_pct: float = 0.0,
        start: bool = False,
    ) -> InstanceView:
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValidationError(f"unsupported timeframe {timeframe}")
        self.accounts.require(account_id)
        strategy = get(strategy_key)
        try:
            parsed = strategy.validate_params(params)
        except Exception as exc:
            raise ValidationError(f"invalid strategy parameters: {exc}") from exc
        if not 0.01 <= position_pct <= 1.0:
            raise ValidationError("position_pct must be between 0.01 and 1")
        if max_order_notional <= 0:
            raise ValidationError("max_order_notional must be positive")
        # Market must match the account, otherwise every signal would be rejected
        # at order time and the failure would only surface in the logs.
        account = self.accounts.require(account_id)
        symbol_view = self.market.require_symbol(symbol)
        if account.market != symbol_view.market:
            raise ValidationError(
                f"{symbol_view.code} 属于 {symbol_view.market} 市场，与账户市场 {account.market} 不一致"
            )
        # An automated instance on a live account places real orders with no human
        # in the loop, so it must not be creatable without the same explicit
        # opt-in that a manual live order requires. Before this check, a live
        # account plus a started instance was enough to send real orders — the
        # X-Live-Confirm header only guarded the manual endpoint.
        if account.kind == "live" and not self.live_auto_trading_enabled:
            raise ForbiddenError(
                "实盘账户的自动运行需要 LQ_LIVE_AUTO_TRADING_ENABLED=true；"
                "仅开启 LQ_LIVE_TRADING_ENABLED 只允许手动下单"
            )
        rules = parse_rules(
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            trailing_stop_pct=trailing_stop_pct,
        )
        row = StrategyInstance(
            id=new_id(),
            account_id=account_id,
            strategy_key=strategy_key,
            symbol=symbol_view.code,
            timeframe=timeframe,
            params_json=json.dumps(parsed.model_dump()),
            position_pct=float(position_pct),
            max_order_notional=float(max_order_notional),
            stop_loss_pct=rules.stop_loss_pct,
            take_profit_pct=rules.take_profit_pct,
            trailing_stop_pct=rules.trailing_stop_pct,
            running=bool(start),
        )
        self.session.add(row)
        self.session.flush()
        self._log(row.id, "info", "created", f"创建实例 {strategy.name} · {row.symbol}")
        record_audit(
            self.session,
            action="runtime.created",
            entity="strategy_instance",
            entity_id=row.id,
            detail=json.dumps({"strategy": strategy_key, "symbol": row.symbol}),
        )
        self.session.commit()
        self.session.refresh(row)
        return self._view(row)

    def update(
        self,
        instance_id: str,
        *,
        params: dict | None = None,
        position_pct: float | None = None,
        max_order_notional: float | None = None,
        stop_loss_pct: float | None = None,
        take_profit_pct: float | None = None,
        trailing_stop_pct: float | None = None,
    ) -> InstanceView:
        row = self._require(instance_id)
        if params is not None:
            strategy = get(row.strategy_key)
            try:
                parsed = strategy.validate_params(params)
            except Exception as exc:
                raise ValidationError(f"invalid strategy parameters: {exc}") from exc
            row.params_json = json.dumps(parsed.model_dump())
        if position_pct is not None:
            if not 0.01 <= position_pct <= 1.0:
                raise ValidationError("position_pct must be between 0.01 and 1")
            row.position_pct = float(position_pct)
        if max_order_notional is not None:
            if max_order_notional <= 0:
                raise ValidationError("max_order_notional must be positive")
            row.max_order_notional = float(max_order_notional)
        if stop_loss_pct is not None or take_profit_pct is not None or trailing_stop_pct is not None:
            rules = parse_rules(
                stop_loss_pct=row.stop_loss_pct if stop_loss_pct is None else stop_loss_pct,
                take_profit_pct=row.take_profit_pct if take_profit_pct is None else take_profit_pct,
                trailing_stop_pct=(
                    row.trailing_stop_pct if trailing_stop_pct is None else trailing_stop_pct
                ),
            )
            row.stop_loss_pct = rules.stop_loss_pct
            row.take_profit_pct = rules.take_profit_pct
            row.trailing_stop_pct = rules.trailing_stop_pct
        self._log(row.id, "info", "updated", "参数已更新")
        self.session.commit()
        self.session.refresh(row)
        return self._view(row)

    def set_running(self, instance_id: str, running: bool) -> InstanceView:
        row = self._require(instance_id)
        if running:
            account = self.accounts.require(row.account_id)
            if account.status != "active":
                raise ConflictError("account is archived")
        row.running = running
        self._log(row.id, "info", "started" if running else "stopped", "运行状态切换")
        record_audit(
            self.session,
            action="runtime.started" if running else "runtime.stopped",
            entity="strategy_instance",
            entity_id=row.id,
        )
        self.session.commit()
        self.session.refresh(row)
        return self._view(row)

    def delete(self, instance_id: str) -> None:
        row = self._require(instance_id)
        if row.running:
            raise ConflictError("stop the instance before deleting it")
        self.session.delete(row)
        self.session.commit()

    # ---------------------------------------------------------------- runner
    def running_instances(self) -> list[StrategyInstance]:
        return list(
            self.session.scalars(
                select(StrategyInstance).where(StrategyInstance.running.is_(True))
            ).all()
        )

    def evaluate_instance(self, row: StrategyInstance) -> str:
        """Evaluate the latest closed bar once; returns a short outcome string."""
        strategy = get(row.strategy_key)
        params = strategy.validate_params(json.loads(row.params_json))
        # An archived account must never trade, even if an instance slipped
        # through (archived before this guard existed, or edited directly in the
        # DB). Archiving also stops instances now; this is the execution backstop.
        account = self.accounts.require(row.account_id)
        if account.status != "active":
            self._log(
                row.id, "warn", "account_archived", "账户已归档，跳过执行",
                bar_time=row.last_bar_time,
            )
            self.session.commit()
            return "account_archived"
        # Re-check the live opt-in on every evaluation, not only at creation.
        # Flipping LQ_LIVE_AUTO_TRADING_ENABLED off must take effect immediately;
        # an instance created while it was on would otherwise keep sending real
        # orders after the operator believed they had disabled automation.
        balance = self._live_guard(row)
        if balance is not None:
            return balance
        # Never act on an in-progress candle: only closed bars drive live signals.
        candles = self.market.candles(
            row.symbol, timeframe=row.timeframe, limit=600, closed_only=True
        )
        if len(candles) < strategy.warmup(params) + 3:
            self._log(row.id, "warn", "insufficient_data", "K 线数量不足，跳过本轮")
            return "insufficient_data"
        last = candles[-1]
        if last.time <= row.last_bar_time:
            return "no_new_bar"
        row.last_bar_time = last.time
        state = strategy.prepare(candles, params)
        signal = strategy.evaluate(state, len(candles) - 1)
        if not signal.is_actionable:
            self.session.commit()
            return "hold"

        row.last_signal_at = utcnow()
        self._log(
            row.id,
            "info",
            "signal",
            f"{signal.action} · {signal.reason}",
            bar_time=last.time,
        )
        position = self._position_quantity(row.account_id, row.symbol)
        try:
            if signal.action == "buy" and position <= 0:
                self._place_entry(row, last.close)
            elif signal.action == "sell" and position > 0:
                order = self.trading.close_position(row.account_id, row.symbol)
                if order is not None:
                    row.executions += 1
                    self._log(
                        row.id,
                        "info",
                        "exit",
                        f"平仓 {row.symbol} @ {order.avg_fill_price}",
                        order_id=order.id,
                        bar_time=last.time,
                    )
        except AppError as exc:
            row.last_error = str(exc)[:400]
            self._log(row.id, "error", "rejected", str(exc), bar_time=last.time)
        self.session.commit()
        return "executed"

    def protect_positions(self) -> dict[str, str]:
        """Check every running instance's open position against its stop rules.

        Runs on every scheduler tick rather than only on new bars: a 10% intrabar
        drop should exit at the stop, not wait for the bar to close. This is the
        live counterpart of the backtester's intrabar stop check — without it the
        same strategy behaves completely differently in research and production.
        """
        outcomes: dict[str, str] = {}
        for row in self.running_instances():
            # Deliberately NOT gated on live_auto_trading_enabled: turning off new
            # automated entries must not also disable protective exits. Blocking a
            # stop would leave a real position unprotected, which is strictly worse
            # than the exposure the operator was trying to avoid.
            rules = StopRules(
                stop_loss_pct=row.stop_loss_pct,
                take_profit_pct=row.take_profit_pct,
                trailing_stop_pct=row.trailing_stop_pct,
            )
            if not rules.enabled:
                continue
            position = self._position_row(row.account_id, row.symbol)
            if position is None or position.quantity <= 0:
                # Flat: reset the trailing peak so the next entry starts fresh.
                if row.peak_price:
                    row.peak_price = 0.0
                    self.session.flush()
                continue
            try:
                price = self.market.last_price(row.symbol)
            except Exception as exc:
                # Silent failure here would hide a broken price feed while the
                # position kept running without any protection.
                row.last_error = f"无法获取 {row.symbol} 价格：{exc}"[:400]
                self._log(
                    row.id, "error", "price_unavailable", row.last_error,
                    bar_time=row.last_bar_time,
                )
                outcomes[row.id] = "price_unavailable"
                continue
            if price <= 0:
                continue

            decision = evaluate_tick(
                entry_price=position.avg_price,
                current_peak=row.peak_price,
                last_price=price,
                rules=rules,
            )
            if decision.new_peak != row.peak_price:
                row.peak_price = decision.new_peak
            if not decision.triggered:
                continue
            try:
                order = self.trading.close_position(row.account_id, row.symbol)
            except Exception as exc:
                row.last_error = str(exc)[:400]
                self._log(row.id, "error", "exit_failed", str(exc), bar_time=row.last_bar_time)
                outcomes[row.id] = "exit_failed"
                continue
            row.executions += 1
            row.peak_price = 0.0
            self._log(
                row.id,
                "info",
                decision.reason,
                f"{decision.reason} 触发：{row.symbol} @ {price:.4f}"
                + (f"（委托 {order.id[:8]}）" if order else ""),
                order_id=order.id if order else "",
                bar_time=row.last_bar_time,
            )
            outcomes[row.id] = decision.reason
        self.session.commit()
        return outcomes

    def _position_row(self, account_id: str, symbol: str):
        from app.modules.trading.models import Position

        return self.session.scalar(
            select(Position).where(
                Position.account_id == account_id, Position.symbol == symbol.upper()
            )
        )

    def _live_guard(self, row: StrategyInstance) -> str | None:
        """Block automated orders on a live account when the opt-in is off.

        Returns the outcome string to report, or None when trading may proceed.
        Logged once per dedupe window rather than every tick.
        """
        account = self.accounts.require(row.account_id)
        if account.kind != "live" or self.live_auto_trading_enabled:
            return None
        message = (
            "实盘自动运行已关闭（LQ_LIVE_AUTO_TRADING_ENABLED=false），跳过执行"
        )
        self._log(row.id, "warn", "live_auto_disabled", message, bar_time=row.last_bar_time)
        self.session.commit()
        return "live_auto_disabled"

    def _place_entry(self, row: StrategyInstance, price: float) -> None:
        account = self.accounts.get(row.account_id)
        symbol = self.market.require_symbol(row.symbol)
        cash_budget = account.cash_balance * row.position_pct
        budget = min(cash_budget, row.max_order_notional)
        raw = budget / price if price else 0.0
        # Orders must respect the exchange lot step, so floor to a whole step.
        lot = symbol.lot_size or 0.0001
        quantity = math.floor(raw / lot) * lot
        quantity = round(quantity, 8)
        if quantity <= 0 or quantity * price < 10:
            # Say *which* limit bound the size. Reporting "cash insufficient" when
            # the notional cap is the binding constraint sends users hunting for
            # the wrong problem (a 1M account can still be blocked by a 200k cap
            # when one board lot costs 257k).
            lot_cost = lot * price
            if lot_cost > row.max_order_notional and lot_cost > cash_budget:
                reason = (
                    f"一手 {lot:g} 股约需 {lot_cost:,.2f}，"
                    f"超过可用资金 {cash_budget:,.2f} 且超过单笔上限 {row.max_order_notional:,.2f}"
                )
            elif lot_cost > row.max_order_notional:
                reason = (
                    f"一手 {lot:g} 股约需 {lot_cost:,.2f}，超过单笔上限 "
                    f"{row.max_order_notional:,.2f}；请调高 max_order_notional"
                )
            else:
                reason = (
                    f"可用资金 {cash_budget:,.2f} 不足以买入一手（约需 {lot_cost:,.2f}）"
                )
            self._log(row.id, "warn", "skipped", reason, bar_time=row.last_bar_time)
            return
        order = self.trading.place_order(
            account_id=row.account_id,
            symbol=row.symbol,
            side="buy",
            order_type="market",
            quantity=quantity,
            source=f"strategy:{row.id[:8]}",
        )
        row.executions += 1
        row.last_error = ""
        self._log(
            row.id,
            "info",
            "entry",
            f"买入 {order.filled_quantity} {row.symbol} @ {order.avg_fill_price}",
            order_id=order.id,
            bar_time=row.last_bar_time,
        )

    def _position_quantity(self, account_id: str, symbol: str) -> float:
        for position in self.trading.positions(account_id):
            if position.symbol == symbol:
                return position.quantity
        return 0.0

    def _log(
        self,
        instance_id: str,
        level: str,
        event: str,
        message: str,
        *,
        order_id: str = "",
        bar_time: int = 0,
    ) -> None:
        """Append a log row, suppressing an identical repeat within the window.

        The scheduler ticks every few seconds. A persistent condition — insufficient
        data, a dead price feed, a sizing limit below one board lot — would otherwise
        write one row per tick for as long as it lasts: ~14k rows per day per
        instance for a single unresolved warning.
        """
        # autoflush is off, so flush first or a row written earlier in this tick
        # would be invisible to the query below.
        self.session.flush()
        latest = self.session.scalar(
            select(RuntimeLog)
            .where(
                RuntimeLog.instance_id == instance_id,
                RuntimeLog.event == event,
                RuntimeLog.message == message,
            )
            .order_by(RuntimeLog.created_at.desc())
            .limit(1)
        )
        # Only suppress repeated warnings/errors. State changes ("started",
        # "updated") are user intent and stay in the audit trail even when they
        # happen twice in quick succession.
        if latest is not None and level in {"warn", "error"}:
            # SQLite returns naive datetimes even for timezone-aware columns.
            age = (utcnow() - ensure_utc(latest.created_at)).total_seconds()
            if age < LOG_DEDUPE_SECONDS:
                return
        self.session.add(
            RuntimeLog(
                instance_id=instance_id,
                level=level,
                event=event,
                message=message,
                order_id=order_id,
                bar_time=bar_time,
            )
        )

    def prune_logs(self, instance_id: str, *, keep: int = MAX_LOGS_PER_INSTANCE) -> int:
        """Drop all but the newest `keep` rows for an instance.

        Logs are the only per-instance table with no natural bound, and they are
        written by a background loop nobody is watching. Without this the file grows
        for the life of the deployment.
        """
        keep_ids = (
            select(RuntimeLog.id)
            .where(RuntimeLog.instance_id == instance_id)
            .order_by(RuntimeLog.created_at.desc())
            .limit(keep)
        )
        result = self.session.execute(
            delete(RuntimeLog).where(
                RuntimeLog.instance_id == instance_id,
                RuntimeLog.id.not_in(keep_ids),
            )
        )
        return int(result.rowcount or 0)

    def prune_all_logs(self, *, keep: int = MAX_LOGS_PER_INSTANCE) -> int:
        """Prune every instance's log tail; called by the scheduler, not the request path."""
        total = 0
        for row in self.session.scalars(select(StrategyInstance.id)).all():
            total += self.prune_logs(row, keep=keep)
        return total
