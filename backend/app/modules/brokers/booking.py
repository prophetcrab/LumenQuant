"""Booking a broker fill into the local ledger.

Two callers need this: the reconciler's polling loop, and the cancel path, which
has to look once at the broker's final state before closing an order (a broker can
have partially filled it in the window between the last poll and the cancel). They
must agree to the cent, or the ledger depends on which path happened to run.

The rule throughout is that the broker is the only source of truth: quantities,
prices and terminal states come from it, never from local inference.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import round_money, round_qty
from app.modules.accounts.service import AccountService
from app.modules.brokers.base import BrokerOrderResult
from app.modules.cn.fees import CnFeeSchedule
from app.modules.trading.models import CashHold, Fill, Order, Position, new_id


def booked_notional(order: Order) -> float:
    """Quote amount already accounted for on this order.

    `order.avg_fill_price` is cumulative, so the booked notional is simply
    `filled_quantity x avg_fill_price`.
    """
    return float(order.filled_quantity) * float(order.avg_fill_price)


def increment_price(order: Order, result: BrokerOrderResult) -> float:
    """Price of the newly reported slice, not the order's average.

    Brokers report a cumulative average. Booking `delta x average` understates
    the cost of the newest slice whenever it filled at a worse price than the
    earlier ones, and the error stays in the ledger permanently.

        first poll:  0.5 filled, 50 quote  -> avg 100, book 0.5 @ 100
        second poll: 1.0 filled, 110 quote -> avg 110, book 0.5 @ 110
                     total booked 105, actually spent 110

    Differencing the cumulative notional gives the true marginal price (120 in
    that example). When the broker does not supply a quote amount, the cumulative
    average is the best available answer.
    """
    if result.quote_quantity is None or result.filled_quantity <= 0:
        return result.avg_price or 0.0
    booked_qty = round_qty(order.filled_quantity)
    incremental = result.quote_quantity - booked_notional(order)
    incremental_qty = result.filled_quantity - booked_qty
    if incremental_qty <= 1e-12 or incremental <= 0:
        return result.avg_price or 0.0
    return incremental / incremental_qty


def fee_for(
    market: str, side: str, notional: float, *, fees: CnFeeSchedule, fee_bps: float
) -> float:
    if market == "cn_stock":
        return fees.total(side, notional)
    return round_money(notional * fee_bps / 10_000.0)


def book_broker_fill(
    session: Session,
    order: Order,
    result: BrokerOrderResult,
    *,
    fees: CnFeeSchedule,
    fee_bps: float,
    live_trading_enabled: bool = True,
) -> bool:
    """Mirror the broker's cumulative fill state into the local ledger.

    Idempotent by `(order_id, cumulative quantity)`: only the delta beyond what
    has already been booked is applied, so repeated polls settle to the same
    numbers.

    Returns False when nothing was booked — either because there is no new fill,
    or because the broker reported one without a usable price. In the latter case
    `order.reject_reason` explains it and the caller must keep the order open: a
    terminal status would drop that fill for good.
    """
    already = round_qty(order.filled_quantity)
    cumulative = round_qty(result.filled_quantity)
    delta = round_qty(cumulative - already)
    if delta <= 1e-9:
        if result.status == "filled" and order.status != "filled":
            order.status = "filled"
        return False

    price = increment_price(order, result)
    if price <= 0:
        order.reject_reason = (
            f"券商回报成交量 {cumulative:g} 但未提供成交价，等待再次轮询"
        )[:240]
        return False

    accounts = AccountService(session, live_trading_enabled=live_trading_enabled)
    market = accounts.require(order.account_id).market
    notional = round_money(delta * price)
    fee = fee_for(market, order.side, notional, fees=fees, fee_bps=fee_bps)

    position = session.scalar(
        select(Position).where(
            Position.account_id == order.account_id, Position.symbol == order.symbol
        )
    )
    if position is None:
        position = Position(
            id=new_id(),
            account_id=order.account_id,
            symbol=order.symbol,
            quantity=0.0,
            available_quantity=0.0,
            avg_price=0.0,
            realized_pnl=0.0,
        )
        session.add(position)
        session.flush()

    if order.side == "buy":
        new_qty = round_qty(position.quantity + delta)
        weighted = position.quantity * position.avg_price + notional + fee
        position.avg_price = round(weighted / new_qty, 8) if new_qty else 0.0
        position.quantity = new_qty
        # Crypto settles instantly; A-share buys are not sellable until the next
        # session (T+1), which the settlement duty releases.
        if market != "cn_stock":
            position.available_quantity = new_qty
        accounts.post_cash(
            order.account_id,
            amount=-round_money(notional + fee),
            kind="trade",
            ref=order.id,
            note=f"live buy {delta} {order.symbol} @ {price}",
        )
    else:
        realized = round_money((price - position.avg_price) * delta - fee)
        position.realized_pnl = round_money(position.realized_pnl + realized)
        position.quantity = round_qty(position.quantity - delta)
        position.available_quantity = max(
            0.0, round_qty(position.available_quantity - delta)
        )
        if position.quantity <= 1e-9:
            position.quantity = 0.0
            position.available_quantity = 0.0
            position.avg_price = 0.0
        proceeds = round_money(notional - fee)
        accounts.post_cash(
            order.account_id,
            amount=proceeds,
            kind="trade",
            ref=order.id,
            note=f"live sell {delta} {order.symbol} @ {price}",
        )
        if market == "cn_stock" and proceeds > 0:
            # A-share sale proceeds settle T+1: booked now, spendable the next
            # trading day. Live orders almost always fill asynchronously, so this
            # path — not the immediate one in TradingService — is the normal case.
            from datetime import date

            from app.modules.cn.calendar import next_trading_day
            from app.modules.risk.daykey import risk_day_key

            release = next_trading_day(date.fromisoformat(risk_day_key(market)))
            session.add(
                CashHold(
                    account_id=order.account_id,
                    amount=proceeds,
                    release_day=release.isoformat(),
                    ref=order.id,
                )
            )

    session.add(
        Fill(
            id=new_id(),
            order_id=order.id,
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            quantity=delta,
            price=price,
            fee=fee,
        )
    )
    order.filled_quantity = cumulative
    # Keep the *cumulative* average, because that is what the next poll's delta is
    # differenced against. Recording the marginal price here would make the next
    # `increment_price` subtract a number that never was a cumulative average.
    prior_qty = cumulative - delta
    prior_notional = prior_qty * order.avg_fill_price
    order.avg_fill_price = round((prior_notional + delta * price) / cumulative, 8)
    order.fee = round_money(order.fee + fee)
    if result.status in {"filled", "partially_filled"}:
        order.status = result.status
    if cumulative >= order.quantity - 1e-9:
        order.status = "filled"
    session.flush()
    return True
