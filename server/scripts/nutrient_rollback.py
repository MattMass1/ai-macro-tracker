"""Read-only preflight and guarded rollback for migration 003 (unknown nutrients).

Default mode is READ-ONLY. It reports, without exposing credentials or row
contents:
  * migration ledger state (applied/pending/held/drift/unknown),
  * whether nutrition_entries protein/carbs/fat/fiber are nullable,
  * how many rows have unknown nutrients (count only),
  * the calorie-only operator switch state,
  * which rollback is safe.

Rollback policy (see docs/rollback/003_calorie_only_unknown_macros.md):
  * Forward-disable is always the first step: set CALORIE_ONLY_WRITES_ENABLED
    to false (or unset it). Unknown values stay NULL and readable.
  * Restoring NOT NULL is allowed ONLY when zero unknown rows exist, and only
    with ``--restore-not-null --confirm-checksum <sha256 of the rollback SQL>``.
    It runs in one transaction under a table lock, re-checks the invariant,
    and records itself in ``schema_rollbacks``. It never edits or deletes
    ``schema_migrations`` rows and never converts NULL to zero.

The 003 ledger row stays, so the deploy runner will not re-apply 003 after a
NOT NULL restore; re-enabling calorie-only writes after that requires a NEW
reviewed forward migration. The application also checks the live column
nullability, so calorie-only writes stay closed after a restore.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import asyncpg

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER / "src"))
from migrations import (  # noqa: E402
    LOCK_KEY, NULLABLE_NUTRIENTS_MIGRATION, migration_status, safe_database_identity,
)
from release_gates import calorie_only_writes_enabled  # noqa: E402

ROLLBACK_SQL = SERVER / "rollback" / "003_restore_nutrients_not_null.sql"
NUTRIENTS = ("protein", "carbs", "fat", "fiber")


class RollbackRefused(RuntimeError):
    """The requested rollback is unsafe or unapproved. Nothing was changed."""


def rollback_checksum() -> str:
    return hashlib.sha256(ROLLBACK_SQL.read_bytes()).hexdigest()


async def _nullable_columns(conn) -> list[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'nutrition_entries' AND column_name = ANY($1::text[]) "
        "AND is_nullable = 'YES' ORDER BY column_name", list(NUTRIENTS))
    return [str(row["column_name"]) for row in rows]


async def _unknown_rows(conn) -> int:
    return int(await conn.fetchval(
        "SELECT count(*) FROM nutrition_entries "
        "WHERE protein IS NULL OR carbs IS NULL OR fat IS NULL OR fiber IS NULL"))


async def preflight(conn) -> dict[str, object]:
    """Read-only state report. Issues SELECTs only."""
    status = await migration_status(conn)
    applied_003 = NULLABLE_NUTRIENTS_MIGRATION not in status["pending"]
    nullable = await _nullable_columns(conn)
    unknown = await _unknown_rows(conn) if nullable else 0
    has_ledger = await conn.fetchval("SELECT to_regclass('public.schema_rollbacks') IS NOT NULL")
    rollbacks = []
    if has_ledger:
        rollbacks = [dict(row) for row in await conn.fetch(
            "SELECT name, checksum, applied_at::text FROM schema_rollbacks ORDER BY applied_at")]
    if unknown:
        advice = ("forward-disable only: keep CALORIE_ONLY_WRITES_ENABLED off; unknown values stay NULL. "
                  "Do not restore NOT NULL, do not zero-fill, do not deploy pre-003 code or native builds.")
    elif nullable:
        advice = ("forward-disable is sufficient. NOT NULL restore is permitted (0 unknown rows) "
                  "only with separate owner approval and --confirm-checksum.")
    else:
        advice = "nutrient columns are NOT NULL; calorie-only writes cannot occur. Nothing to roll back."
    return {
        "migrations": status,
        "migration_003_applied": applied_003,
        "nullable_nutrient_columns": nullable,
        "unknown_nutrient_rows": unknown,
        "calorie_only_writes_switch": calorie_only_writes_enabled(),
        "rollback_sql_checksum": rollback_checksum(),
        "recorded_rollbacks": rollbacks,
        "advice": advice,
    }


async def restore_not_null(conn, confirm_checksum: str) -> dict[str, object]:
    """Apply the reviewed NOT NULL rollback atomically, or refuse with no change."""
    digest = rollback_checksum()
    if confirm_checksum.strip().casefold() != digest:
        raise RollbackRefused("rollback SQL checksum not confirmed; nothing changed")
    if calorie_only_writes_enabled():
        raise RollbackRefused("forward-disable first: CALORIE_ONLY_WRITES_ENABLED is on; nothing changed")
    before = await migration_status(conn)
    _require_rollback_ledger(before)
    await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
    try:
        try:
            async with conn.transaction():
                # A migration may have run while we waited for the shared lock.
                # Recheck before creating even the rollback ledger table.
                locked = await migration_status(conn)
                _require_rollback_ledger(locked)
                if locked != before:
                    raise RollbackRefused("migration ledger changed while acquiring lock; nothing changed")
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_rollbacks (name TEXT NOT NULL, checksum TEXT NOT NULL, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
                await conn.execute(ROLLBACK_SQL.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_rollbacks(name, checksum) VALUES($1, $2)",
                                   ROLLBACK_SQL.name, digest)
                # Both the report and the final invariant must precede commit:
                # a refusal here rolls back the DDL and append-only record.
                after = await preflight(conn)
                ledger_after = await migration_status(conn)
                if ledger_after != locked or not ledger_after["compatible"]:
                    raise RollbackRefused("migration ledger changed unexpectedly; nothing changed")
        except asyncpg.RaiseError as exc:
            raise RollbackRefused(str(exc).splitlines()[0] + "; nothing changed") from None
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
    return after


def _require_rollback_ledger(status) -> None:
    """Refuse an unapplied 003 or incompatible history before any rollback DDL."""
    if NULLABLE_NUTRIENTS_MIGRATION in status["pending"]:
        raise RollbackRefused("migration 003 is not applied; nothing to roll back")
    if not status["compatible"] or status["drift"] or status["unknown"]:
        raise RollbackRefused("migration ledger is incompatible; nothing changed")


async def run(database_url: str, *, restore: bool, confirm_checksum: str) -> dict[str, object]:
    conn = await asyncpg.connect(database_url)
    try:
        if restore:
            return {"mode": "restore_not_null", **await restore_not_null(conn, confirm_checksum)}
        return {"mode": "read_only_preflight", **await preflight(conn)}
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--restore-not-null", action="store_true",
                        help="apply the reviewed NOT NULL rollback (refuses unless 0 unknown rows)")
    parser.add_argument("--confirm-checksum", default="",
                        help="sha256 of rollback/003_restore_nutrients_not_null.sql")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")
    identity = safe_database_identity(args.database_url)
    print(f"target: database={identity['database']} host_class={identity['host_class']}")
    try:
        result = asyncio.run(run(args.database_url, restore=args.restore_not_null,
                                 confirm_checksum=args.confirm_checksum))
    except RollbackRefused as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
