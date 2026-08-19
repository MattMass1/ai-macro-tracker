#!/usr/bin/env python3
"""Adopt the legacy database as Matt's tenant and enable tenant constraints.

Safe to rerun: only unowned legacy rows are stamped, Matt's identity is upserted,
and an existing workout plan is never overwritten.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from uuid import UUID

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/src"))

import domain  # noqa: E402
from auth import DEFAULT_MATT_USER_ID  # noqa: E402

SCHEMA = ROOT / "server/src/schema.sql"
TENANT_TABLES = (
    "days", "meals", "nutrition_entries", "fitness_tracker", "max_weight",
    "exercise_max_reps", "meal_presets", "macro_targets", "briefs",
)
BOOTSTRAP_DEVICE_LABEL = "Matt migration bootstrap"


def matt_plan() -> dict[str, object]:
    """Return Matt's legacy rotation in the persisted workout-plan shape."""
    plan_days = []
    for workout_type in (*domain.WORKOUT_ROTATION, "Abs", "Cardio"):
        plan_days.append({
            "type": workout_type,
            "exercises": [
                {"name": name} for name in domain.get_default_exercises_for_type(workout_type)
            ],
        })
    return {"rotation": list(domain.WORKOUT_ROTATION), "days": plan_days}


async def run() -> None:
    """Apply the schema, adopt legacy rows, and seed Matt's workout plan."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    matt_user_id = UUID(
        os.environ.get("MATT_USER_ID", "").strip() or str(DEFAULT_MATT_USER_ID)
    )
    display_name = os.environ.get("MATT_DISPLAY_NAME", "Matt").strip() or "Matt"

    conn = await asyncpg.connect(database_url)
    raw_device_token: str | None = None
    try:
        schema = SCHEMA.read_text(encoding="utf-8")
        async with conn.transaction():
            # Legacy tables already exist. Hold these locks until every row is
            # stamped and the tenant keys are installed, leaving no old-server
            # write window between those operations.
            await conn.execute(
                "LOCK TABLE " + ", ".join(TENANT_TABLES) + " IN ACCESS EXCLUSIVE MODE"
            )
            await conn.execute(schema)
            await conn.execute(
                "INSERT INTO users(id,display_name,is_admin) VALUES($1,$2,true) "
                "ON CONFLICT(id) DO UPDATE SET display_name=EXCLUDED.display_name,is_admin=true",
                matt_user_id, display_name,
            )
            stamped: dict[str, int] = {}
            for table in TENANT_TABLES:
                result = await conn.execute(
                    f"UPDATE {table} SET user_id=$1 WHERE user_id IS NULL", matt_user_id
                )
                stamped[table] = int(result.rsplit(" ", 1)[-1])
                remaining = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE user_id IS NULL"
                )
                if remaining:
                    raise RuntimeError(f"Could not assign every {table} row to Matt")
            null_counts = {
                table: await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE user_id IS NULL"
                )
                for table in TENANT_TABLES
            }
            if any(null_counts.values()):
                raise RuntimeError(f"Tenant stamping left NULL user ids: {null_counts}")
            await conn.execute(
                "INSERT INTO workout_plans(user_id,plan) VALUES($1,$2::jsonb) "
                "ON CONFLICT(user_id) DO NOTHING",
                matt_user_id, json.dumps(matt_plan()),
            )
            # This pass sees no NULL tenant ids and widens keys while locks remain held.
            # schema.sql guards every constraint, making this safe to rerun.
            await conn.execute(schema)

            days_key = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint c "
                "WHERE c.conrelid='days'::regclass AND c.contype='p' "
                "AND (SELECT array_agg(a.attname ORDER BY k.ordinality) "
                "FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ordinality) "
                "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum) "
                "= ARRAY['user_id','date']::name[])"
            )
            meals_key = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint c "
                "WHERE c.conrelid='meals'::regclass AND c.contype='u' "
                "AND (SELECT array_agg(a.attname ORDER BY k.ordinality) "
                "FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ordinality) "
                "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum) "
                "= ARRAY['user_id','day','meal_type']::name[])"
            )
            if not days_key or not meals_key:
                raise RuntimeError(
                    "Tenant constraints missing after migration: "
                    f"days_pk={days_key}, meals_unique={meals_key}"
                )

            has_device = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM devices WHERE user_id=$1)", matt_user_id
            )
            if not has_device:
                raw_device_token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(raw_device_token.encode("utf-8")).hexdigest()
                await conn.execute(
                    "INSERT INTO devices(token_hash,user_id,label) VALUES($1,$2,$3)",
                    token_hash, matt_user_id, BOOTSTRAP_DEVICE_LABEL,
                )
        print(f"Matt user: {matt_user_id}")
        print("Stamped legacy rows: " + ", ".join(f"{k}={v}" for k, v in stamped.items()))
        if raw_device_token:
            print(f"Matt device token (shown once): {raw_device_token}")
        else:
            print("Matt already has a device; no new token was minted.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
