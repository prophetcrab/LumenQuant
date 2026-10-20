"""Strategy instance lifecycle and one-bar evaluation.

The runner is deliberately small: it converts a strategy signal into an order
through the trading module. All execution rules (risk, fees, fills) stay there.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError
from app.core.timeutil import iso, utcnow
from app.modules.market.service import SUPPORTED_TIMEFRAMES, MarketService
from app.modules.runtime.models import RuntimeLog, StrategyInstance, new_id
from app.modules.strategies.registry import get
from app.modules.trading.service import TradingService

MAX_LOGS = 200


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
    ) -> None:
        self.session = session
        self.accounts = accounts_service
        self.market = market
        self.trading = trading
        self.execution_price = execution_price

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
        row = StrategyInstance(
            id=new_id(),
            account_id=account_id,
            strategy_key=strategy_key,
            symbol=symbol.upper().strip(),
            timeframe=timeframe,
            params_json=json.dumps(parsed.model_dump()),
            position_pct=float(position_pct),
            max_order_notional=float(max_order_notional),
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

    def _place_entry(self, row: StrategyInstance, price: float) -> None:
        account = self.accounts.get(row.account_id)
        symbol = self.market.require_symbol(row.symbol)
        budget = min(account.cash_balance * row.position_pct, row.max_order_notional)
        raw = budget / price if price else 0.0
        # Orders must respect the exchange lot step, so floor to a whole step.
        lot = symbol.lot_size or 0.0001
        quantity = math.floor(raw / lot) * lot
        quantity = round(quantity, 8)
        if quantity <= 0 or quantity * price < 10:
            self._log(row.id, "warn", "skipped", "可用资金不足最小下单量，跳过买入", bar_time=row.last_bar_time)
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
