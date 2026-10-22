"""Alert creation and acknowledgement."""

from __future__ import annotations

from dataclasses import dataclass

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.timeutil import ensure_utc, iso, utcnow
from app.modules.alerts.models import Alert

MAX_TITLE = 160
# Repeating the same condition on every scheduler tick would bury the operator in
# identical rows. One open (unacknowledged) alert per (account, category, ref) is
# enough to make the problem visible.
DEDUPE_SECONDS = 300


def _apply_window(stmt, *, since: datetime | None, until: datetime | None):
    """Restrict a query to [since, until).

    Both bounds are optional and normalised to UTC before comparison: the column
    is timezone-aware, and a naive datetime would silently compare against UTC in
    SQLite and produce an off-by-hours window with no error.
    """
    if since is not None:
        stmt = stmt.where(Alert.created_at >= ensure_utc(since))
    if until is not None:
        stmt = stmt.where(Alert.created_at < ensure_utc(until))
    return stmt


@dataclass(frozen=True)
class AlertView:
    id: str
    account_id: str
    level: str
    category: str
    title: str
    message: str
    ref_type: str
    ref_id: str
    acknowledged: bool
    acknowledged_at: str
    created_at: str


class AlertService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ----------------------------------------------------------------- write
    def raise_alert(
        self,
        *,
        category: str,
        title: str,
        message: str = "",
        level: str = "warning",
        account_id: str = "",
        ref_type: str = "",
        ref_id: str = "",
        commit: bool = False,
    ) -> Alert:
        """Record an alert, collapsing repeats of an already-open one.

        `commit=False` by default so callers can raise an alert inside a larger
        transaction (an order rejection that rolls back must not leave an alert
        pointing at an order that never existed).
        """
        if level not in {"info", "warning", "critical"}:
            raise ValidationError(f"unknown alert level {level}")
        latest = self.session.scalar(
            select(Alert)
            .where(
                Alert.account_id == account_id,
                Alert.category == category,
                Alert.ref_id == ref_id,
                Alert.acknowledged.is_(False),
            )
            .order_by(Alert.created_at.desc())
            .limit(1)
        )
        if latest is not None:
            age = (utcnow() - ensure_utc(latest.created_at)).total_seconds()
            if age < DEDUPE_SECONDS:
                return latest
        row = Alert(
            account_id=account_id,
            level=level,
            category=category,
            title=title[:MAX_TITLE],
            message=message,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        self.session.add(row)
        self.session.flush()
        if commit:
            self.session.commit()
        return row

    def acknowledge(self, alert_id: str) -> AlertView:
        row = self.session.get(Alert, alert_id)
        if row is None:
            raise NotFoundError(f"alert {alert_id} not found")
        row.acknowledged = True
        row.acknowledged_at = utcnow()
        self.session.commit()
        return self._view(row)

    def acknowledge_all(self, *, account_id: str | None = None) -> int:
        stmt = update(Alert).where(Alert.acknowledged.is_(False))
        if account_id:
            stmt = stmt.where(Alert.account_id == account_id)
        result = self.session.execute(
            stmt.values(acknowledged=True, acknowledged_at=utcnow())
        )
        self.session.commit()
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------ read
    def list(
        self,
        *,
        account_id: str | None = None,
        unacknowledged_only: bool = False,
        level: str | None = None,
        category: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[AlertView]:
        stmt = select(Alert).order_by(Alert.created_at.desc())
        if account_id:
            # Platform-wide alerts (empty account_id) concern every account: a
            # dead price feed is exactly what a trader needs to see, so filtering
            # strictly by account would hide the most important alert there is.
            stmt = stmt.where(
                (Alert.account_id == account_id) | (Alert.account_id == "")
            )
        if unacknowledged_only:
            stmt = stmt.where(Alert.acknowledged.is_(False))
        if level:
            stmt = stmt.where(Alert.level == level)
        if category:
            stmt = stmt.where(Alert.category == category)
        stmt = _apply_window(stmt, since=since, until=until)
        rows = self.session.scalars(stmt.limit(max(1, min(limit, 500)))).all()
        return [self._view(row) for row in rows]

    def categories(self, *, account_id: str | None = None) -> list[dict]:
        """Distinct categories with counts, for building a filter control."""
        stmt = select(Alert.category, func.count(Alert.id))
        if account_id:
            # Same rule as `list()`: include platform-wide rows, otherwise the
            # "行情中断" filter option would be missing on an otherwise populated
            # alert center and the rows behind it unreachable.
            stmt = stmt.where(
                (Alert.account_id == account_id) | (Alert.account_id == "")
            )
        rows = self.session.execute(
            stmt.group_by(Alert.category).order_by(Alert.category)
        ).all()
        return [{"category": category, "count": int(count)} for category, count in rows]

    def prune(self, *, keep_days: int = 30, account_id: str | None = None) -> int:
        """Delete acknowledged alerts older than `keep_days`.

        Unacknowledged rows are never removed: an alert nobody looked at is still
        an open question, however old. Acknowledged ones are history — useful for
        a while, then just table growth.
        """
        if keep_days < 0:
            raise ValidationError("keep_days 必须为非负数")
        cutoff = utcnow() - timedelta(days=keep_days)
        stmt = delete(Alert).where(
            Alert.acknowledged.is_(True), Alert.created_at < cutoff
        )
        if account_id:
            stmt = stmt.where(Alert.account_id == account_id)
        result = self.session.execute(stmt)
        self.session.commit()
        return int(result.rowcount or 0)

    def summary(
        self,
        *,
        account_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        all_accounts: bool = False,
    ) -> dict:
        """Open-alert counts, optionally windowed and split per account.

        `all_accounts` exists because the sidebar badge answers "is anything
        wrong?" while the page shows one account. Without a cross-account total an
        operator has to click through every account to discover that a *different*
        one is tripping a breaker.
        """
        stmt = select(Alert.level, func.count(Alert.id)).where(
            Alert.acknowledged.is_(False)
        )
        if account_id and not all_accounts:
            # Match `list()`: platform-wide alerts count for every account, so the
            # sidebar badge cannot stay silent while the feed is down.
            stmt = stmt.where(
                (Alert.account_id == account_id) | (Alert.account_id == "")
            )
        stmt = _apply_window(stmt, since=since, until=until)
        counts = {level: 0 for level in ("info", "warning", "critical")}
        for level, count in self.session.execute(stmt.group_by(Alert.level)).all():
            counts[level] = int(count)

        payload = {
            "unacknowledged": sum(counts.values()),
            "by_level": counts,
        }
        if not all_accounts:
            return payload

        # Per-account breakdown, including the rows attached to no account at all
        # (feed outages) which are reported under an empty key rather than
        # silently folded into a random account.
        per_stmt = (
            select(Alert.account_id, Alert.level, func.count(Alert.id))
            .where(Alert.acknowledged.is_(False))
            .group_by(Alert.account_id, Alert.level)
        )
        per_stmt = _apply_window(per_stmt, since=since, until=until)
        accounts: dict[str, dict] = {}
        for owner, level, count in self.session.execute(per_stmt).all():
            bucket = accounts.setdefault(
                owner or "",
                {"account_id": owner or "", "unacknowledged": 0, "by_level": {}},
            )
            bucket["unacknowledged"] += int(count)
            bucket["by_level"][level] = int(count)
        payload["accounts"] = sorted(
            accounts.values(), key=lambda item: item["unacknowledged"], reverse=True
        )
        return payload

    @staticmethod
    def _view(row: Alert) -> AlertView:
        return AlertView(
            id=row.id,
            account_id=row.account_id,
            level=row.level,
            category=row.category,
            title=row.title,
            message=row.message,
            ref_type=row.ref_type,
            ref_id=row.ref_id,
            acknowledged=bool(row.acknowledged),
            acknowledged_at=iso(row.acknowledged_at),
            created_at=iso(row.created_at),
        )
