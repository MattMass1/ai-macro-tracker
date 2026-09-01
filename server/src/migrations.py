"""Ordered, checksummed PostgreSQL migrations for Macro Coach."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"
LOCK_KEY = 6_291_470_021


class MigrationError(RuntimeError):
    """Raised when migration history is missing, unknown, or has drifted."""


def safe_database_identity(database_url: str) -> dict[str, str]:
    """Validate a PostgreSQL URL and return non-secret deploy preflight fields."""
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise MigrationError("DATABASE_URL must be a PostgreSQL URL with a host")
    database = parsed.path.lstrip("/")
    if not database or "/" in database:
        raise MigrationError("DATABASE_URL must name exactly one database")
    host = parsed.hostname.casefold()
    host_class = "localhost" if host in {"localhost", "127.0.0.1", "::1"} else "public_dns"
    try:
        address = ipaddress.ip_address(host)
        if address.is_private:
            host_class = "private_ip"
    except ValueError:
        if host.endswith((".internal", ".local")):
            host_class = "private_dns"
    return {"database": database, "host_class": host_class}


def migration_files() -> list[tuple[str, Path]]:
    """Return the immutable migration stream in application order."""
    files = [(path.name, path) for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))]
    names = [name for name, _ in files]
    if len(names) != len(set(names)):
        raise MigrationError("duplicate migration filename")
    return files


def checksum(path: Path) -> str:
    """Return the SHA-256 of a migration's exact bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _ensure_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "filename TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )


async def migration_status(conn: asyncpg.Connection) -> dict[str, object]:
    """Check migration compatibility without writing or exposing connection data."""
    expected = migration_files()
    try:
        exists = await conn.fetchval("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        rows = await conn.fetch("SELECT filename,checksum FROM schema_migrations ORDER BY filename") if exists else []
    except asyncpg.PostgresError as exc:
        raise MigrationError("database migration state unavailable") from exc
    applied = {str(row["filename"]): str(row["checksum"]) for row in rows}
    expected_map = {name: checksum(path) for name, path in expected}
    drift = sorted(name for name in applied if name in expected_map and applied[name] != expected_map[name])
    unknown = sorted(set(applied) - set(expected_map))
    pending = [name for name, _ in expected if name not in applied]
    compatible = not drift and not unknown and not pending
    return {"compatible": compatible, "pending": pending, "drift": drift, "unknown": unknown}


async def apply_migrations(database_url: str) -> list[str]:
    """Apply pending files transactionally under a PostgreSQL advisory lock."""
    conn = await asyncpg.connect(database_url)
    applied_now: list[str] = []
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
        await _ensure_table(conn)
        rows = await conn.fetch("SELECT filename,checksum FROM schema_migrations")
        applied = {str(row["filename"]): str(row["checksum"]) for row in rows}
        expected = migration_files()
        expected_names = {name for name, _ in expected}
        unknown = sorted(set(applied) - expected_names)
        if unknown:
            raise MigrationError("database contains unknown migration(s): " + ", ".join(unknown))
        for name, path in expected:
            digest = checksum(path)
            if name in applied:
                if applied[name] != digest:
                    raise MigrationError(f"checksum drift for applied migration {name}")
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations(filename,checksum) VALUES($1,$2)", name, digest
                )
            applied_now.append(name)
        return applied_now
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
        finally:
            await conn.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.database_url:
        import os
        args.database_url = os.environ.get("DATABASE_URL", "")
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")
    identity = safe_database_identity(args.database_url)
    print(f"migration target: database={identity['database']} host_class={identity['host_class']}")
    applied = asyncio.run(apply_migrations(args.database_url))
    print(f"migrations applied: {len(applied)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
