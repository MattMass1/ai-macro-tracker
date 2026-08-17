#!/usr/bin/env python3
"""Build the days -> meals -> nutrition_entries hierarchy from legacy rows.

Safe to rerun: rollups are overwritten from the current food-entry data and
existing meal identities are retained through the (day, meal_type) key.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import asyncpg

SCHEMA = Path(__file__).resolve().parents[1] / "server/src/schema.sql"
MACROS = ("calories", "protein", "carbs", "fat", "fiber")


def stable_meal_id(day: object, meal_type: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"macro-tracker:{day}:{meal_type}"))


async def run() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(SCHEMA.read_text())
        async with conn.transaction():
            groups = await conn.fetch(
                "SELECT day,meal AS meal_type,sum(calories) AS calories,"
                "sum(protein) AS protein,sum(carbs) AS carbs,sum(fat) AS fat,"
                "sum(fiber) AS fiber FROM nutrition_entries GROUP BY day,meal "
                "ORDER BY day,meal"
            )
            for group in groups:
                await conn.execute(
                    "INSERT INTO days(date) VALUES($1) ON CONFLICT(date) DO NOTHING",
                    group["day"],
                )
                meal_id = await conn.fetchval(
                    "INSERT INTO meals(id,day,meal_type,calories,protein,carbs,fat,fiber) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(day,meal_type) "
                    "DO UPDATE SET calories=EXCLUDED.calories,protein=EXCLUDED.protein,"
                    "carbs=EXCLUDED.carbs,fat=EXCLUDED.fat,fiber=EXCLUDED.fiber RETURNING id",
                    stable_meal_id(group["day"], group["meal_type"]),
                    group["day"], group["meal_type"],
                    *(group[key] for key in MACROS),
                )
                await conn.execute(
                    "UPDATE nutrition_entries SET meal_id=$1 WHERE day=$2 AND meal=$3 "
                    "AND meal_id IS DISTINCT FROM $1",
                    meal_id, group["day"], group["meal_type"],
                )

            await conn.execute(
                "INSERT INTO days(date,calories,protein,carbs,fat,fiber) "
                "SELECT day,sum(calories),sum(protein),sum(carbs),sum(fat),sum(fiber) "
                "FROM nutrition_entries GROUP BY day ON CONFLICT(date) DO UPDATE SET "
                "calories=EXCLUDED.calories,protein=EXCLUDED.protein,carbs=EXCLUDED.carbs,"
                "fat=EXCLUDED.fat,fiber=EXCLUDED.fiber"
            )
        print(f"Backfilled {len(groups)} meal rollup(s).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
