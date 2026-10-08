"""Offline R1 ordering regressions using real functions and synthetic connections.

The doubles model transaction commit/rollback and query ordering; they do NOT
execute SQL or establish PostgreSQL constraint/locking correctness.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path

import asyncpg
import pytest

import migrations

SERVER = Path(__file__).resolve().parents[1]
M003 = migrations.NULLABLE_NUTRIENTS_MIGRATION


@pytest.fixture
def rollback(monkeypatch):
    spec = importlib.util.spec_from_file_location("ordering_rollback", SERVER / "scripts/nutrient_rollback.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("CALORIE_ONLY_WRITES_ENABLED", "CALORIE_ONLY_WRITES_USER_IDS", "APPROVED_MIGRATIONS"):
        monkeypatch.delenv(name, raising=False)
    return module


class SyntheticTransaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        conn = self.conn
        assert not conn.in_transaction
        conn.in_transaction = True
        conn.events.append("transaction_started")
        self.before = deepcopy((conn.nullable, conn.rollback_ledger, conn.records))

    async def __aexit__(self, exc_type, _exc, _tb):
        conn = self.conn
        if exc_type is None:
            conn.events.append("transaction_committed")
            conn.committed_sql.extend(conn.staged_sql)
        else:
            conn.events.append("transaction_rolled_back")
            conn.nullable, conn.rollback_ledger, conn.records = self.before
        conn.staged_sql.clear()
        conn.in_transaction = False


class SyntheticConnection:
    def __init__(self, *, at_lock=None, after_sql=None, unknown_rows=0):
        self.ledger = {name: migrations.checksum(path) for name, path in migrations.migration_files()}
        self.at_lock = at_lock
        self.after_sql = after_sql
        self.unknown_rows = unknown_rows
        self.nullable = ["carbs", "fat", "fiber", "protein"]
        self.rollback_ledger = False
        self.records = []
        self.events = []
        self.attempted_ddl = []
        self.staged_sql = []
        self.committed_sql = []
        self.in_transaction = False
        self.locked = False

    def transaction(self):
        return SyntheticTransaction(self)

    async def fetchval(self, sql, *_args):
        self.events.append("read_inside_transaction" if self.in_transaction else "read_outside_transaction")
        if "schema_migrations" in sql:
            return True
        if "schema_rollbacks" in sql:
            return self.rollback_ledger
        if "count(*) FROM nutrition_entries" in sql:
            return self.unknown_rows
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetch(self, sql, *_args):
        if "FROM schema_migrations" in sql:
            self.events.append("ledger_read_locked" if self.locked else "ledger_read_unlocked")
            return [{"filename": name, "checksum": digest} for name, digest in sorted(self.ledger.items())]
        if "information_schema.columns" in sql:
            return [{"column_name": name} for name in self.nullable]
        if "FROM schema_rollbacks" in sql:
            return deepcopy(self.records)
        raise AssertionError(f"unexpected fetch: {sql}")

    async def execute(self, sql, *args):
        if "pg_advisory_unlock" in sql:
            assert self.locked
            self.locked = False
            self.events.append("unlocked")
            return
        if "pg_advisory_lock" in sql:
            assert not self.locked
            self.locked = True
            self.events.append("locked")
            if self.at_lock:
                self.at_lock(self)
            return
        assert self.in_transaction and self.locked
        self.attempted_ddl.append(sql)
        self.staged_sql.append(sql)
        if "CREATE TABLE IF NOT EXISTS schema_rollbacks" in sql:
            self.rollback_ledger = True
        elif "ALTER TABLE nutrition_entries" in sql:
            self.events.append("rollback_sql_executed")
            if self.unknown_rows:
                raise asyncpg.RaiseError("refusing rollback: row(s) have unknown nutrients; use forward-disable")
            self.nullable = []
        elif "INSERT INTO schema_rollbacks" in sql:
            self.records.append({"name": args[0], "checksum": args[1], "applied_at": "synthetic timestamp"})
            if self.after_sql:
                self.after_sql(self)
        else:
            raise AssertionError(f"unexpected execute: {sql}")


def change_ledger(conn, kind):
    if kind == "drift":
        conn.ledger["001_food_catalog.sql"] = "0" * 64
    elif kind == "unknown":
        conn.ledger["999_unreviewed.sql"] = "a" * 64
    elif kind == "pending_003":
        conn.ledger.pop(M003)
    elif kind == "pending_other":
        conn.ledger.pop("001_food_catalog.sql")
    else:
        raise AssertionError(kind)


def assert_no_committed_rollback(conn):
    assert conn.committed_sql == []
    assert conn.records == []
    assert conn.rollback_ledger is False
    assert conn.nullable == ["carbs", "fat", "fiber", "protein"]
    assert conn.locked is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["drift", "unknown", "pending_other", "pending_003"])
async def test_preexisting_incompatible_ledger_refuses_before_any_ddl(rollback, kind):
    conn = SyntheticConnection()
    change_ledger(conn, kind)
    with pytest.raises(rollback.RollbackRefused):
        await rollback.restore_not_null(conn, rollback.rollback_checksum())
    assert conn.attempted_ddl == []
    assert "transaction_committed" not in conn.events
    assert_no_committed_rollback(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["drift", "unknown", "pending_003", "pending_other"])
async def test_ledger_changed_while_awaiting_lock_refuses_before_any_ddl(rollback, kind):
    conn = SyntheticConnection(at_lock=lambda c: change_ledger(c, kind))
    with pytest.raises(rollback.RollbackRefused):
        await rollback.restore_not_null(conn, rollback.rollback_checksum())
    assert conn.attempted_ddl == []
    assert conn.events[-1] == "unlocked"
    assert "ledger_read_locked" in conn.events
    assert_no_committed_rollback(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["drift", "unknown", "pending_003", "pending_other"])
async def test_final_ledger_mismatch_raises_inside_transaction_and_rolls_back(rollback, kind):
    conn = SyntheticConnection(after_sql=lambda c: change_ledger(c, kind))
    with pytest.raises(rollback.RollbackRefused, match="migration ledger changed unexpectedly.*nothing changed"):
        await rollback.restore_not_null(conn, rollback.rollback_checksum())
    assert "rollback_sql_executed" in conn.events
    assert "transaction_rolled_back" in conn.events
    assert "transaction_committed" not in conn.events
    assert conn.events.index("transaction_rolled_back") < conn.events.index("unlocked")
    assert_no_committed_rollback(conn)


@pytest.mark.asyncio
async def test_success_and_repeated_restore_preserve_migration_history(rollback):
    conn = SyntheticConnection()
    before = deepcopy(conn.ledger)
    for _ in range(2):
        result = await rollback.restore_not_null(conn, rollback.rollback_checksum())
        assert result["nullable_nutrient_columns"] == []
        assert result["unknown_nutrient_rows"] == 0
        assert result["migrations"]["compatible"] is True
        assert conn.ledger == before
        assert not conn.locked
    assert conn.events.count("transaction_committed") == 2
    assert len(conn.records) == 2  # Existing append-only ledger behavior, no history edits.
    assert all(r["checksum"] == rollback.rollback_checksum() for r in conn.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["checksum", "switch"])
async def test_invocation_gates_refuse_before_lock_or_ddl(rollback, monkeypatch, gate):
    conn = SyntheticConnection()
    digest = rollback.rollback_checksum()
    if gate == "switch":
        monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
    else:
        digest = "0" * 64
    with pytest.raises(rollback.RollbackRefused, match="nothing changed"):
        await rollback.restore_not_null(conn, digest)
    assert conn.events == []
    assert_no_committed_rollback(conn)


@pytest.mark.asyncio
async def test_unknown_rows_sql_refusal_rolls_back_and_releases_lock(rollback):
    conn = SyntheticConnection(unknown_rows=1)
    with pytest.raises(rollback.RollbackRefused, match="unknown nutrients.*nothing changed"):
        await rollback.restore_not_null(conn, rollback.rollback_checksum())
    assert "transaction_rolled_back" in conn.events
    assert_no_committed_rollback(conn)


@pytest.mark.asyncio
async def test_readonly_preflight_performs_no_lock_or_ddl(rollback):
    conn = SyntheticConnection(unknown_rows=1)
    report = await rollback.preflight(conn)
    assert report["unknown_nutrient_rows"] == 1
    assert report["nullable_nutrient_columns"] == conn.nullable
    assert report["calorie_only_writes_switch"] is False  # Invocation env only, not live-server attestation.
    assert "forward-disable only" in report["advice"]
    assert conn.attempted_ddl == []
    assert "locked" not in conn.events
    assert_no_committed_rollback(conn)
