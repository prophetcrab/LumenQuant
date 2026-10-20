"""Declared indexes must exist in the database.

create_all() does not add indexes to tables that already exist, so indexes added
after the first release silently never materialised. This test pins the contract.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from app.core.db import Base, Database


def test_all_declared_indexes_exist(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'idx.db').as_posix()}")
    try:
        db.create_schema()
        # Simulate an older database: drop an index, then re-run startup logic.
        inspector = inspect(db.engine)
        declared = {
            table.name: {index.name for index in table.indexes}
            for table in Base.metadata.tables.values()
            if table.indexes
        }
        assert declared, "expected at least one declared index"

        with db.engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX ix_candles_lookup")

        db.create_schema()  # must restore it

        inspector = inspect(db.engine)
        for table, names in declared.items():
            actual = {index["name"] for index in inspector.get_indexes(table)}
            missing = names - actual
            assert not missing, f"{table} missing indexes: {missing}"
    finally:
        db.dispose()


def test_schema_creation_is_idempotent(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'idx2.db').as_posix()}")
    try:
        db.create_schema()
        first = {
            table: {c["name"] for c in inspect(db.engine).get_columns(table)}
            for table in inspect(db.engine).get_table_names()
        }
        db.create_schema()
        second = {
            table: {c["name"] for c in inspect(db.engine).get_columns(table)}
            for table in inspect(db.engine).get_table_names()
        }
        assert first == second
    finally:
        db.dispose()
