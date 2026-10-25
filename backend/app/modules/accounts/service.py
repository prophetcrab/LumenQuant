"""Account lifecycle and cash ledger.

This module knows nothing about orders or positions. Other modules consume it
through the read-only protocols defined at the bottom of this file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.money import round_money
from app.core.timeutil import iso
from app.modules.accounts.models import Account, LedgerEntry


@dataclass(frozen=True)
class AccountView:
    id: str
    name: str
    kind: str
    market: str
    broker: str
    base_currency: str
    initial_balance: float
    leverage: float
    status: str
    cash_balance: float
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class LedgerView:
    id: str
    account_id: str
    kind: str
    amount: float
    balance_after: float
    ref: str
    note: str
    created_at: str


class AccountService:
    def __init__(self, session: Session, *, live_trading_enabled: bool = False) -> None:
        self.session = session
        self.live_trading_enabled = live_trading_enabled

    # ------------------------------------------------------------------ reads
    def require(self, account_id: str) -> Account:
        account = self.session.get(Account, account_id)
        if account is None:
            raise NotFoundError(f"account {account_id} not found")
        return account

    def cash(self, account_id: str) -> float:
        self.require(account_id)
        return self._cash(account_id)

    def net_contributions(self, account_id: str) -> float:
        """Capital actually put in by the owner (initial + deposits - withdrawals)."""
        self.require(account_id)
        total = self.session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount), 0.0)).where(
                LedgerEntry.account_id == account_id,
                LedgerEntry.kind.in_(("initial", "deposit", "withdraw")),
            )
        )
        return round_money(float(total or 0.0))

    def _cash(self, account_id: str) -> float:
        total = self.session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount), 0.0)).where(
                LedgerEntry.account_id == account_id
            )
        )
        return round_money(float(total or 0.0))

    def view(self, account: Account) -> AccountView:
        return AccountView(
            id=account.id,
            name=account.name,
            kind=account.kind,
            market=account.market,
            broker=account.broker,
            base_currency=account.base_currency,
            initial_balance=round_money(account.initial_balance),
            leverage=account.leverage,
            status=account.status,
            cash_balance=self._cash(account.id),
            created_at=iso(account.created_at),
            updated_at=iso(account.updated_at),
        )

    def list(self, *, include_archived: bool = True) -> list[AccountView]:
        stmt = select(Account).order_by(Account.created_at.asc())
        if not include_archived:
            stmt = stmt.where(Account.status == "active")
        return [self.view(a) for a in self.session.scalars(stmt).all()]

    def get(self, account_id: str) -> AccountView:
        return self.view(self.require(account_id))

    def ledger(self, account_id: str, *, limit: int = 100) -> list[LedgerView]:
        self.require(account_id)
        rows = self.session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.account_id == account_id)
            .order_by(LedgerEntry.created_at.desc())
            .limit(limit)
        ).all()
        return [
            LedgerView(
                id=e.id,
                account_id=e.account_id,
                kind=e.kind,
                amount=e.amount,
                balance_after=e.balance_after,
                ref=e.ref,
                note=e.note,
                created_at=iso(e.created_at),
            )
            for e in rows
        ]

    # ----------------------------------------------------------------- writes
    def create(
        self,
        *,
        name: str,
        kind: str = "paper",
        initial_balance: float = 100_000.0,
        base_currency: str | None = None,
        broker: str | None = None,
        leverage: float = 1.0,
        market: str = "crypto",
    ) -> AccountView:
        name = name.strip()
        if not name:
            raise ValidationError("account name must not be empty")
        if kind not in {"paper", "live"}:
            raise ValidationError("account kind must be paper or live")
        if market not in {"crypto", "cn_stock"}:
            raise ValidationError("market must be crypto or cn_stock")
        if kind == "live" and not self.live_trading_enabled:
            raise ForbiddenError("live trading is disabled by server configuration")
        if initial_balance <= 0:
            raise ValidationError("initial balance must be positive")
        if market == "cn_stock":
            # A-share cash accounts are not margin accounts in this platform.
            leverage = 1.0
            base_currency = base_currency or "CNY"
        else:
            base_currency = base_currency or "USDT"
        if leverage < 1 or leverage > 10:
            raise ValidationError("leverage must be between 1 and 10")
        broker = broker or ("paper" if kind == "paper" else ("ths" if market == "cn_stock" else "binance"))
        if self.session.scalar(select(Account.id).where(Account.name == name)) is not None:
            raise ConflictError(f"account name '{name}' already exists")

        account = Account(
            name=name,
            kind=kind,
            market=market,
            broker=broker,
            base_currency=base_currency.upper(),
            initial_balance=round_money(initial_balance),
            leverage=float(leverage),
        )
        try:
            self.session.add(account)
            self.session.flush()
            self._append_ledger(
                account.id,
                amount=account.initial_balance,
                kind="initial",
                note="initial funding",
                current_cash=0.0,
            )
            record_audit(
                self.session,
                action="account.created",
                entity="account",
                entity_id=account.id,
                detail=json.dumps({"name": name, "kind": kind, "market": market}),
            )
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(f"account name '{name}' already exists") from exc
        self.session.refresh(account)
        return self.view(account)

    def update(
        self,
        account_id: str,
        *,
        name: str | None = None,
        status: str | None = None,
        leverage: float | None = None,
    ) -> AccountView:
        account = self.require(account_id)
        if name is not None:
            name = name.strip()
            if not name:
                raise ValidationError("account name must not be empty")
            account.name = name
        stopped: list[str] = []
        if status is not None:
            if status not in {"active", "archived"}:
                raise ValidationError("status must be active or archived")
            account.status = status
            if status == "archived":
                # Archiving means "stop trading this account". Leaving running
                # instances behind kept placing orders: the scheduler only checked
                # status when an instance was started, so an archived account's
                # instance went on trading until someone noticed.
                from app.modules.runtime.models import StrategyInstance

                running = self.session.scalars(
                    select(StrategyInstance).where(
                        StrategyInstance.account_id == account.id,
                        StrategyInstance.running.is_(True),
                    )
                ).all()
                for instance in running:
                    instance.running = False
                    stopped.append(instance.id)
        if leverage is not None:
            if leverage < 1 or leverage > 10:
                raise ValidationError("leverage must be between 1 and 10")
            account.leverage = float(leverage)
        record_audit(
            self.session,
            action="account.updated",
            entity="account",
            entity_id=account.id,
            detail=json.dumps(
                {
                    "status": account.status,
                    "leverage": account.leverage,
                    "stopped_instances": len(stopped),
                }
            ),
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(f"account name '{name}' already exists") from exc
        self.session.refresh(account)
        return self.view(account)

    def deposit(self, account_id: str, amount: float, *, note: str = "") -> AccountView:
        if amount <= 0:
            raise ValidationError("deposit amount must be positive")
        account = self.require(account_id)
        if account.status != "active":
            raise ConflictError("cannot fund an archived account")
        cash = self._cash(account.id)
        self._append_ledger(account.id, amount=round_money(amount), kind="deposit", note=note, current_cash=cash)
        record_audit(
            self.session,
            action="account.deposited",
            entity="account",
            entity_id=account.id,
            detail=json.dumps({"amount": round_money(amount)}),
        )
        self.session.commit()
        self.session.refresh(account)
        return self.view(account)

    def withdraw(self, account_id: str, amount: float, *, note: str = "") -> AccountView:
        if amount <= 0:
            raise ValidationError("withdraw amount must be positive")
        account = self.require(account_id)
        if account.kind != "paper":
            raise ForbiddenError("withdrawals are only supported on paper accounts")
        cash = self._cash(account.id)
        if amount > cash:
            raise ConflictError("insufficient cash balance")
        self._append_ledger(
            account.id,
            amount=-round_money(amount),
            kind="withdraw",
            note=note,
            current_cash=cash,
        )
        record_audit(
            self.session,
            action="account.withdrawn",
            entity="account",
            entity_id=account.id,
            detail=json.dumps({"amount": round_money(amount)}),
        )
        self.session.commit()
        self.session.refresh(account)
        return self.view(account)

    def align_to_broker_cash(
        self, account_id: str, broker_cash: float, *, note: str = ""
    ) -> AccountView:
        """Move the local cash book to the broker's reported balance.

        A live account is created with an `initial_balance` that is only a local
        bookkeeping starting point; the money that can actually be spent sits at
        the broker. Until the two agree, every real order is sized against a
        number that has nothing to do with reality — a live account funded with
        the documented `initial_balance=1` can never buy anything, and one funded
        too high tries to spend money that is not there.

        The adjustment is a ledger entry, not a silent field edit, so the
        difference stays visible as a funding event in the account history.
        """
        if broker_cash < 0:
            raise ValidationError("broker cash must not be negative")
        account = self.require(account_id)
        if account.kind != "live":
            raise ConflictError("only live accounts are funded from a broker balance")
        if account.status != "active":
            raise ConflictError("cannot adjust an archived account")
        cash = self._cash(account.id)
        delta = round_money(broker_cash - cash)
        if abs(delta) < 0.0001:
            return self.view(account)
        self._append_ledger(
            account.id,
            amount=delta,
            kind="deposit" if delta > 0 else "withdraw",
            note=note or f"对齐券商余额：{cash:,.2f} -> {broker_cash:,.2f}",
            current_cash=cash,
        )
        record_audit(
            self.session,
            action="account.aligned_to_broker",
            entity="account",
            entity_id=account.id,
            detail=json.dumps(
                {"before": round_money(cash), "broker": round_money(broker_cash)}
            ),
        )
        self.session.commit()
        self.session.refresh(account)
        return self.view(account)

    def post_cash(
        self,
        account_id: str,
        *,
        amount: float,
        kind: str,
        ref: str = "",
        note: str = "",
    ) -> float:
        """Book a cash movement inside the caller's transaction (no commit).

        Trading uses this to keep order, position and ledger updates atomic.
        """
        account = self.require(account_id)
        if account.status != "active":
            raise ConflictError("account is not active")
        cash = self._cash(account_id)
        entry = self._append_ledger(
            account_id, amount=amount, kind=kind, ref=ref, note=note, current_cash=cash
        )
        self.session.flush()
        return entry.balance_after

    def _append_ledger(
        self,
        account_id: str,
        *,
        amount: float,
        kind: str,
        note: str = "",
        ref: str = "",
        current_cash: float | None = None,
    ) -> LedgerEntry:
        cash = self._cash(account_id) if current_cash is None else current_cash
        entry = LedgerEntry(
            account_id=account_id,
            kind=kind,
            amount=round_money(amount),
            balance_after=round_money(cash + amount),
            ref=ref,
            note=note,
        )
        self.session.add(entry)
        return entry


class AccountReader(Protocol):
    """Minimal read contract other modules depend on."""

    def require(self, account_id: str) -> Account: ...

    def cash(self, account_id: str) -> float: ...
