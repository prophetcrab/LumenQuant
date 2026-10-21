"""Portfolio read model assembled from accounts and trading read ports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.core.money import round_money, safe_div
from app.core.timeutil import iso, utcnow
from app.modules.accounts.service import AccountService
from app.modules.portfolio.models import EquitySnapshot
from app.modules.trading.service import PositionView, TradingService

SNAPSHOT_THROTTLE = timedelta(seconds=10)


@dataclass(frozen=True)
class Allocation:
    symbol: str
    market_value: float
    weight_pct: float
    unrealized_pnl: float


@dataclass(frozen=True)
class PortfolioOverview:
    account_id: str
    account_name: str
    base_currency: str
    initial_balance: float
    net_contributions: float
    cash: float
    available_cash: float
    reserved_cash: float
    positions_value: float
    equity: float
    total_return_pct: float
    realized_pnl: float
    unrealized_pnl: float
    exposure_pct: float
    position_count: int
    allocations: list[Allocation]
    change_pct: float
    updated_at: str


class PortfolioService:
    def __init__(self, session: Session, accounts: AccountService, trading: TradingService) -> None:
        self.session = session
        self.accounts = accounts
        self.trading = trading

    def _resolve(self, account_id: str | None) -> str:
        if account_id:
            self.accounts.require(account_id)
            return account_id
        rows = self.accounts.list(include_archived=False)
        if not rows:
            raise NotFoundError("no active account; create one first")
        return rows[0].id

    def overview(self, account_id: str | None = None, *, record: bool = True) -> PortfolioOverview:
        account_id = self._resolve(account_id)
        account = self.accounts.get(account_id)
        cash = round_money(account.cash_balance)
        reserved = self.trading.reserved_cash(account_id)
        # Delegated rather than recomputed: `available_cash` also subtracts sale
        # proceeds still inside their T+1 hold. Recomputing `cash - reserved` here
        # made this page disagree with the trading layer about what was spendable.
        available_cash = self.trading.available_cash(account_id)
        positions: list[PositionView] = self.trading.positions(account_id)
        positions_value = round_money(sum(p.market_value for p in positions))
        unrealized = round_money(sum(p.unrealized_pnl for p in positions))
        realized = round_money(sum(p.realized_pnl for p in positions))
        equity = round_money(cash + positions_value)
        net_contributions = self.accounts.net_contributions(account_id)
        allocations = [
            Allocation(
                symbol=p.symbol,
                market_value=p.market_value,
                weight_pct=round(safe_div(p.market_value, equity) * 100, 4),
                unrealized_pnl=p.unrealized_pnl,
            )
            for p in sorted(positions, key=lambda x: x.market_value, reverse=True)
        ]
        previous = self.session.scalars(
            select(EquitySnapshot)
            .where(EquitySnapshot.account_id == account_id)
            .order_by(EquitySnapshot.created_at.desc())
            .limit(1)
        ).first()
        change_pct = 0.0
        if previous is not None and previous.equity:
            change_pct = (equity / previous.equity - 1.0) * 100.0

        if record:
            self._record(
                account_id, equity, cash, positions_value, realized, net_contributions
            )

        return PortfolioOverview(
            account_id=account_id,
            account_name=account.name,
            base_currency=account.base_currency,
            initial_balance=account.initial_balance,
            net_contributions=net_contributions,
            cash=cash,
            available_cash=available_cash,
            reserved_cash=reserved,
            positions_value=positions_value,
            equity=equity,
            # Deposits move equity but are not profit; measure against net capital.
            total_return_pct=round(
                safe_div(equity - net_contributions, net_contributions) * 100, 4
            ),
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            exposure_pct=round(safe_div(positions_value, equity) * 100, 4),
            position_count=len(positions),
            allocations=allocations,
            change_pct=round(change_pct, 4),
            updated_at=iso(utcnow()),
        )

    def _record(
        self,
        account_id: str,
        equity: float,
        cash: float,
        positions_value: float,
        realized: float,
        net_contributions: float,
    ) -> None:
        last = self.session.scalars(
            select(EquitySnapshot)
            .where(EquitySnapshot.account_id == account_id)
            .order_by(EquitySnapshot.created_at.desc())
            .limit(1)
        ).first()
        now = utcnow()
        if last is not None and now - last.created_at.replace(tzinfo=now.tzinfo) < SNAPSHOT_THROTTLE:
            return
        self.session.add(
            EquitySnapshot(
                account_id=account_id,
                equity=equity,
                cash=cash,
                positions_value=positions_value,
                realized_pnl=realized,
                net_contributions=net_contributions,
                created_at=now,
            )
        )
        self.session.commit()

    def analytics(self, account_id: str | None = None, *, limit: int = 1000) -> dict:
        """Return-based analytics derived from equity snapshots + open positions."""
        resolved = self._resolve(account_id)
        rows = self.session.scalars(
            select(EquitySnapshot)
            .where(EquitySnapshot.account_id == resolved)
            .order_by(EquitySnapshot.created_at.asc())
            .limit(max(2, min(limit, 5000)))
        ).all()
        equities = [row.equity for row in rows]
        # Time-weighted return: compare capital-at-work, not raw equity, so a
        # deposit is not mistaken for performance.
        capital = [
            row.equity - row.net_contributions if row.net_contributions else row.equity
            for row in rows
        ]
        returns: list[float] = []
        for i in range(1, len(equities)):
            if capital[i - 1] > 0:
                returns.append((equities[i] - equities[i - 1]) / capital[i - 1])

        peak = equities[0] if equities else 0.0
        max_drawdown = 0.0
        current_streak = 0
        max_streak = 0
        for value in equities:
            if value >= peak:
                peak = value
                current_streak = 0
            else:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
                max_drawdown = max(max_drawdown, safe_div(peak - value, peak))

        mean = sum(returns) / len(returns) if returns else 0.0
        variance = (
            sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
            if len(returns) > 1
            else 0.0
        )
        volatility = variance**0.5
        downside = [r for r in returns if r < 0]
        downside_std = (
            (sum((r - sum(downside) / len(downside)) ** 2 for r in downside) / (len(downside) - 1)) ** 0.5
            if len(downside) > 1
            else 0.0
        )
        # Snapshots are throttled to >=10s; annualising assumes the observed cadence
        # instead of pretending they are daily bars.
        positive = sum(r for r in returns if r > 0)
        negative = abs(sum(r for r in returns if r < 0))

        positions = self.trading.positions(resolved)
        attribution = [
            {
                "symbol": p.symbol,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
                "realized_pnl": p.realized_pnl,
                "total_pnl": round_money(p.unrealized_pnl + p.realized_pnl),
            }
            for p in sorted(
                positions, key=lambda p: p.unrealized_pnl + p.realized_pnl, reverse=True
            )
        ]
        return {
            "account_id": resolved,
            "samples": len(equities),
            "start_equity": equities[0] if equities else 0.0,
            "current_equity": equities[-1] if equities else 0.0,
            # Consistent with the overview: profit against capital contributed.
            "total_return_pct": round(
                safe_div(equities[-1] - rows[-1].net_contributions, rows[-1].net_contributions)
                * 100,
                4,
            )
            if rows and rows[-1].net_contributions
            else 0.0,
            "max_drawdown_pct": round(max_drawdown * 100, 4),
            "max_drawdown_samples": max_streak,
            "volatility_pct": round(volatility * 100, 6),
            "sharpe_per_sample": round(safe_div(mean, volatility), 4) if volatility else 0.0,
            "sortino_per_sample": round(safe_div(mean, downside_std), 4) if downside_std else 0.0,
            "positive_return_pct": round(
                safe_div(len([r for r in returns if r > 0]), len(returns)) * 100, 2
            ),
            "profit_factor": round(safe_div(positive, negative), 4) if negative else None,
            "best_sample_pct": round(max(returns) * 100, 6) if returns else 0.0,
            "worst_sample_pct": round(min(returns) * 100, 6) if returns else 0.0,
            "attribution": attribution,
        }

    def history(self, account_id: str | None = None, *, limit: int = 500) -> list[dict]:
        account_id = self._resolve(account_id)
        rows = self.session.scalars(
            select(EquitySnapshot)
            .where(EquitySnapshot.account_id == account_id)
            .order_by(EquitySnapshot.created_at.desc())
            .limit(max(1, min(limit, 5000)))
        ).all()
        rows = list(reversed(rows))
        return [
            {
                "time": iso(row.created_at),
                "equity": row.equity,
                "cash": row.cash,
                "positions_value": row.positions_value,
                "net_contributions": row.net_contributions,
            }
            for row in rows
        ]
