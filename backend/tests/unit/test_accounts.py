from __future__ import annotations

import pytest

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.modules.accounts.service import AccountService


def test_create_account_books_initial_ledger(database):
    with database.session() as session:
        account = AccountService(session).create(name="Paper One", initial_balance=50_000)
        assert account.cash_balance == 50_000
        assert account.kind == "paper"
        ledger = AccountService(session).ledger(account.id)
        assert len(ledger) == 1
        assert ledger[0].kind == "initial"
        assert ledger[0].balance_after == 50_000


def test_duplicate_name_rejected(database):
    with database.session() as session:
        service = AccountService(session)
        service.create(name="Duplicated")
        with pytest.raises(ConflictError):
            service.create(name="Duplicated")


def test_deposit_and_withdraw(database):
    with database.session() as session:
        service = AccountService(session)
        account = service.create(name="Cash Flow", initial_balance=1_000)
        service.deposit(account.id, 500)
        service.withdraw(account.id, 200)
        assert service.get(account.id).cash_balance == 1_300
        with pytest.raises(ConflictError):
            service.withdraw(account.id, 99_999)


def test_live_account_blocked_when_disabled(database):
    with database.session() as session:
        service = AccountService(session, live_trading_enabled=False)
        with pytest.raises(ForbiddenError):
            service.create(name="Live", kind="live")


def test_validation_rules(database):
    with database.session() as session:
        service = AccountService(session)
        with pytest.raises(ValidationError):
            service.create(name="  ")
        with pytest.raises(ValidationError):
            service.create(name="Bad Money", initial_balance=0)
        with pytest.raises(ValidationError):
            service.create(name="Leverage", leverage=99)
        with pytest.raises(NotFoundError):
            service.get("missing")


def test_archive_blocks_funding(database):
    with database.session() as session:
        service = AccountService(session)
        account = service.create(name="Archive Me")
        service.update(account.id, status="archived")
        with pytest.raises(ConflictError):
            service.deposit(account.id, 100)
