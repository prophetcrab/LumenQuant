"""A-share T+1 settlement duty.

Buying today's shares does not make them sellable today, and today's sale
proceeds are not spendable today. Both facts are expressed by state that has to be
released exactly once per trading day, when the next session opens.

Two background loops can own that duty — the trading engine (which also matches
paper limit orders) and the live reconciler (which must keep running even when the
strategy runtime is off). Whichever is alive has to perform it, and both must
perform it identically, so the release lives here rather than in either loop.

The release is deliberately scoped to A-share accounts: crypto settles instantly,
and clearing `available < quantity` everywhere would hide a genuine bookkeeping
mismatch instead of fixing one.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import delete, select

from app.modules.accounts.models import Account
from app.modules.cn.calendar import day_key, is_session_open
from app.modules.market.models import Symbol
from app.modules.trading.models import CashHold, Position

logger = logging.getLogger(__name__)


def release_t1(session, *, now_ms: int | None = None) -> tuple[int, int]:
    """Release A-share T+1 positions and sale-proceeds holds.

    Returns `(positions_released, holds_released)`. Safe to call repeatedly: the
    caller is responsible for the once-per-day guard, but a second call in the
    same session simply finds nothing left to do.
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cn_account_ids = session.scalars(
        select(Account.id).where(Account.market == "cn_stock")
    ).all()
    if not cn_account_ids:
        return 0, 0

    positions = session.scalars(
        select(Position).where(
            Position.quantity > Position.available_quantity,
            Position.account_id.in_(cn_account_ids),
        )
    ).all()
    for position in positions:
        position.available_quantity = position.quantity

    # Holds are removed rather than flipped: the ledger already carries the cash,
    # so a released hold simply stops subtracting from availability. Only holds
    # whose release day has arrived are cleared, so a mid-session call cannot
    # unlock proceeds the broker has not settled.
    today = day_key(now)
    released = session.execute(
        delete(CashHold).where(
            CashHold.account_id.in_(cn_account_ids),
            CashHold.release_day <= today.isoformat(),
        )
    )
    session.commit()
    return len(positions), int(released.rowcount or 0)


def refresh_prev_close(session, cn_engine) -> int:
    """Refresh A-share previous closes from the live engine, when there is one.

    Daily price limits are derived from this value. A stale one silently widens or
    narrows the band, which changes what counts as 涨停/跌停 for the day.
    """
    if cn_engine is None:
        return 0
    rows = session.scalars(select(Symbol).where(Symbol.market == "cn_stock")).all()
    updated = 0
    for row in rows:
        last = cn_engine.prev_close(row.code)
        if last:
            row.prev_close = last
            updated += 1
    if updated:
        session.commit()
    return updated


def session_key(now_ms: int) -> str:
    """Identity of the current trading session, for once-per-session guards."""
    return day_key(now_ms).isoformat()


def session_is_open(now_ms: int) -> bool:
    return is_session_open(now_ms)
