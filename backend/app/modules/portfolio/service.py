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
            self._record(account_id, equity, cash, positions_value, realized)

        return PortfolioOverview(
            account_id=account_id,
            account_name=account.name,
            base_currency=account.base_currency,
            initial_balance=account.initial_balance,
            net_contributions=net_contributions,
            cash=cash,
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

    def _record(self, account_id: str, equity: float, cash: float, positions_value: float, realized: float) -> None:
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
                created_at=now,
            )
        )
        self.session.commit()

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
            }
            for row in rows
        ]
