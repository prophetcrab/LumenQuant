"""Database infrastructure shared by all modules.

Modules own their tables and never reach into another module's repository.
The Database object is injected at composition time.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.url = url
        self.engine = create_engine(url, echo=echo, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            self._install_sqlite_pragmas()
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False, future=True
        )

    def _install_sqlite_pragmas(self) -> None:
        @event.listens_for(self.engine, "connect")
        def _set_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    @contextmanager
    def session(self) -> Iterator[Session]:
        db = self.session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_schema(self) -> None:
        from app.core import schema  # noqa: F401  (imports every module model)

        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()
