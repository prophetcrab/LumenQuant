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
        # SQLite defaults to a 5+10 pool, which serialises bursts of read requests
        # into multi-second waits. Reads are cheap here, so widen the pool.
        pool_kwargs = (
            {"pool_size": 20, "max_overflow": 30, "pool_timeout": 10, "pool_recycle": 1800}
            if url.startswith("sqlite")
            else {}
        )
        self.url = url
        self.engine = create_engine(
            url, echo=echo, connect_args=connect_args, future=True, **pool_kwargs
        )
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
        self._apply_light_migrations()

    def _apply_light_migrations(self) -> None:
        """Additive columns introduced after the first release.

        create_all() never alters existing tables, and this project deliberately
        avoids a migration framework. Each entry is idempotent and additive only.
        """
        from sqlalchemy import inspect, text

        additions = {
            "equity_snapshots": {"net_contributions": "FLOAT NOT NULL DEFAULT 0"},
        }
        inspector = inspect(self.engine)
        with self.engine.begin() as connection:
            for table, columns in additions.items():
                if table not in inspector.get_table_names():
                    continue
                existing = {col["name"] for col in inspector.get_columns(table)}
                for name, ddl in columns.items():
                    if name not in existing:
                        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

    def dispose(self) -> None:
        self.engine.dispose()
