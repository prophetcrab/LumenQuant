"""Equity baseline tracking for the daily-loss and drawdown breakers."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.errors import NotFoundError
from app.core.timeutil import iso, utcnow
from app.modules.portfolio.models import EquitySnapshot
from app.modules.risk.models import RiskState


@dataclass(frozen=True)
class RiskStateView:
    account_id: str
    day_start_equity: float
    peak_equity: float
    breaker_active: bool
    breaker_reason: str
    updated_at: str


class RiskService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _row(self, account_id: str, *, create: bool) -> RiskState | None:
        row = self.session.scalar(
            select(RiskState).where(RiskState.account_id == account_id)
        )
        if row is None and create:
            row = RiskState(account_id=account_id, day_start_equity=0.0, peak_equity=0.0)
            self.session.add(row)
            self.session.flush()
        return row

    def state(self, account_id: str) -> RiskStateView:
        row = self._row(account_id, create=False)
        if row is None:
            return RiskStateView(account_id, 0.0, 0.0, False, "", "")
        return RiskStateView(
            account_id=account_id,
            day_start_equity=row.day_start_equity,
            peak_equity=row.peak_equity,
            breaker_active=bool(row.breaker_reason),
            breaker_reason=row.breaker_reason,
            updated_at=iso(row.updated_at),
        )

    def ensure_baseline(self, account_id: str, equity: float) -> RiskStateView:
        """Create baselines on first use without committing the caller's transaction.

        The order path calls this so the daily-loss breaker has a reference even
        before the background snapshotter has run for the day; leaving it at 0
        silently disabled the breaker until the first portfolio tick.
        """
        row = self._row(account_id, create=True)
        assert row is not None
        if row.day_start_equity <= 0:
            row.day_start_equity = equity
        if equity > row.peak_equity:
            row.peak_equity = equity
        row.updated_at = utcnow()
        self.session.flush()
        return self.state(account_id)

    def observe(self, account_id: str, equity: float) -> RiskStateView:
        """Update baselines with the latest equity mark."""
        row = self._row(account_id, create=True)
        assert row is not None
        if row.day_start_equity <= 0:
            row.day_start_equity = equity
        if equity > row.peak_equity:
            row.peak_equity = equity
        row.updated_at = utcnow()
        self.session.commit()
        return self.state(account_id)

    def trip(self, account_id: str, reason: str) -> RiskStateView:
        row = self._row(account_id, create=True)
        assert row is not None
        row.breaker_reason = reason[:240]
        record_audit(
            self.session,
            action="risk.breaker_tripped",
            entity="account",
            entity_id=account_id,
            detail=json.dumps({"reason": reason}),
        )
        self.session.commit()
        return self.state(account_id)

    def reset(self, account_id: str) -> RiskStateView:
        row = self._row(account_id, create=True)
        assert row is not None
        row.breaker_reason = ""
        row.day_start_equity = 0.0
        row.peak_equity = 0.0
        record_audit(
            self.session, action="risk.breaker_reset", entity="account", entity_id=account_id
        )
        self.session.commit()
        return self.state(account_id)

    def recent_equity(self, account_id: str, *, limit: int = 200) -> list[float]:
        rows = self.session.scalars(
            select(EquitySnapshot.equity)
            .where(EquitySnapshot.account_id == account_id)
            .order_by(EquitySnapshot.created_at.desc())
            .limit(limit)
        ).all()
        return [float(v) for v in rows]
