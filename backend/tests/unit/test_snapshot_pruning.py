"""Equity snapshot downsampling.

Snapshots are appended every ~10s per account by the portfolio engine and had no
retention policy at all: 8,640 rows/day/account, 3M/year, with every history
query scanning them. The pruning must keep the curve's shape, so the bucket
representative is the *last* row (the closing equity), never an average.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import Settings
from app.core.db import Database
from app.core.timeutil import utcnow
from app.modules.accounts.service import AccountService
from app.modules.portfolio.models import EquitySnapshot
from app.modules.portfolio.service import prune_equity_snapshots


@pytest.fixture()
def service(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'snap.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    with db.session() as session:
        accounts = AccountService(session, live_trading_enabled=False)
        account = accounts.create(
            name="快照清理", initial_balance=100_000.0, market="cn_stock"
        )
        yield db, session, account.id
    db.dispose()


def test_old_rows_are_downsampled_to_one_per_hour(service):
    db, session, account_id = service
    now = utcnow()
    # 48 hourly rows, three days old: they span two hourly buckets per... no —
    # one row per hour means every row is its own bucket and must survive.
    for hour in range(48):
        session.add(
            EquitySnapshot(
                account_id=account_id,
                equity=100_000.0 + hour,
                cash=100_000.0,
                positions_value=float(hour),
                realized_pnl=0.0,
                net_contributions=100_000.0,
                created_at=now - timedelta(days=10, hours=hour),
            )
        )
    session.commit()

    removed = prune_equity_snapshots(session, fine_days=7, hourly_days=365)
    assert removed == 0


def test_sub_hour_rows_collapse_and_keep_the_last_value(service):
    db, session, account_id = service
    now = utcnow()
    base = now - timedelta(days=10)
    # 12 rows inside one hour: 10:00 .. 10:55, equity climbing 100..111.
    for minute in range(0, 60, 5):
        session.add(
            EquitySnapshot(
                account_id=account_id,
                equity=100.0 + minute // 5,
                cash=100.0,
                positions_value=0.0,
                realized_pnl=0.0,
                net_contributions=100.0,
                created_at=base.replace(minute=minute, second=0, microsecond=0),
            )
        )
    session.commit()

    removed = prune_equity_snapshots(session, fine_days=7, hourly_days=365)
    assert removed == 11

    from sqlalchemy import select

    rows = session.scalars(
        select(EquitySnapshot).order_by(EquitySnapshot.created_at.asc())
    ).all()
    assert len(rows) == 1
    # The closing equity of the bucket, not an average of the twelve rows.
    assert rows[0].equity == pytest.approx(111.0)


def test_recent_rows_are_untouched(service):
    db, session, account_id = service
    now = utcnow()
    for minute in range(0, 60, 5):
        session.add(
            EquitySnapshot(
                account_id=account_id,
                equity=100.0,
                cash=100.0,
                positions_value=0.0,
                realized_pnl=0.0,
                net_contributions=100.0,
                created_at=now - timedelta(minutes=minute),
            )
        )
    session.commit()
    assert prune_equity_snapshots(session, fine_days=7, hourly_days=365) == 0


def test_very_old_rows_collapse_to_one_per_day(service):
    db, session, account_id = service
    now = utcnow()
    base = now - timedelta(days=400)
    for hour in range(24):
        session.add(
            EquitySnapshot(
                account_id=account_id,
                equity=200.0 + hour,
                cash=200.0,
                positions_value=0.0,
                realized_pnl=0.0,
                net_contributions=200.0,
                created_at=base.replace(hour=hour, minute=0, second=0, microsecond=0),
            )
        )
    session.commit()

    removed = prune_equity_snapshots(session, fine_days=7, hourly_days=365)
    assert removed == 23
    from sqlalchemy import select

    rows = session.scalars(select(EquitySnapshot)).all()
    assert len(rows) == 1
    assert rows[0].equity == pytest.approx(223.0)
