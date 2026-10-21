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
from app.modules.risk.daykey import risk_day_key
from app.modules.risk.models import RiskState


@dataclass(frozen=True)
class RiskStateView:
    account_id: str
    day_start_equity: float
    peak_equity: float
    breaker_active: bool
    breaker_reason: str
    updated_at: str
    day_key: str = ""
    halt_kind: str = ""


class RiskService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _roll_day(self, row: RiskState, equity: float, market: str) -> bool:
        """Reset the daily baseline when the risk day changes.

        Called on every read/write of the baseline. Returns True when a rollover
        happened so callers can log or audit it. Peak equity intentionally
        survives the roll so drawdown breakers keep their memory, while the
        breaker itself is released for the new day.
        """
        today = risk_day_key(market)
        if row.day_key == today:
            if row.day_start_equity <= 0:
                row.day_start_equity = equity
            if equity > row.peak_equity:
                row.peak_equity = equity
            return False
        # New day: re-anchor the daily loss window.
        row.day_key = today
        row.day_start_equity = equity
        if equity > row.peak_equity:
            row.peak_equity = equity
        # A new session gets a fresh daily-loss budget, but a drawdown halt is a
        # structural risk decision and must survive the roll.
        row.daily_reason = ""
        if not row.drawdown_reason:
            row.breaker_reason = ""
        return True

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
        reason = row.drawdown_reason or row.daily_reason or row.breaker_reason
        return RiskStateView(
            account_id=account_id,
            day_start_equity=row.day_start_equity,
            peak_equity=row.peak_equity,
            breaker_active=bool(reason),
            breaker_reason=reason,
            updated_at=iso(row.updated_at),
            day_key=row.day_key,
            halt_kind=(
                "drawdown" if row.drawdown_reason else ("daily" if row.daily_reason else "")
            ),
        )

    def ensure_baseline(self, account_id: str, equity: float) -> RiskStateView:
        """Create baselines on first use without committing the caller's transaction.

        The order path calls this so the daily-loss breaker has a reference even
        before the background snapshotter has run for the day; leaving it at 0
        silently disabled the breaker until the first portfolio tick.
        """
        row = self._row(account_id, create=True)
        assert row is not None
        market = self._market_of(account_id)
        self._roll_day(row, equity, market)
        row.updated_at = utcnow()
        self.session.flush()
        return self.state(account_id)

    def _market_of(self, account_id: str) -> str:
        from app.modules.accounts.models import Account

        account = self.session.get(Account, account_id)
        return account.market if account is not None else "crypto"

    def observe(self, account_id: str, equity: float) -> RiskStateView:
        """Update baselines with the latest equity mark."""
        row = self._row(account_id, create=True)
        assert row is not None
        market = self._market_of(account_id)
        try:
            rolled = self._roll_day(row, equity, market)
        except Exception:
            rolled = False
        row.updated_at = utcnow()
        self.session.commit()
        if rolled:
            record_audit(
                self.session,
                action="risk.day_rolled",
                entity="account",
                entity_id=account_id,
                detail=json.dumps({"day": row.day_key, "baseline": equity}),
            )
            self.session.commit()
        return self.state(account_id)

    def trip(self, account_id: str, reason: str) -> RiskStateView:
        row = self._row(account_id, create=True)
        assert row is not None
        row.breaker_reason = reason[:240]
        if "drawdown" in reason.lower():
            row.drawdown_reason = reason[:240]
        else:
            row.daily_reason = reason[:240]
        record_audit(
            self.session,
            action="risk.breaker_tripped",
            entity="account",
            entity_id=account_id,
            detail=json.dumps({"reason": reason}),
        )
        # A tripped breaker stops all new orders. Nobody watching the screen at
        # that moment would otherwise learn about it until they tried to trade.
        from app.modules.alerts.service import AlertService

        AlertService(self.session).raise_alert(
            category="breaker",
            level="critical",
            account_id=account_id,
            title="风控熔断已触发，新开仓被禁止",
            message=reason,
            ref_type="account",
            ref_id=account_id,
        )
        self.session.commit()
        return self.state(account_id)

    def reset(self, account_id: str) -> RiskStateView:
        row = self._row(account_id, create=True)
        assert row is not None
        row.breaker_reason = ""
        row.daily_reason = ""
        row.drawdown_reason = ""
        row.day_start_equity = 0.0
        row.peak_equity = 0.0
        row.day_key = ""
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
