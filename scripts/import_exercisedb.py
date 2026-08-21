#!/usr/bin/env python3
"""Idempotently import the full no-key ExerciseDB V1 catalog."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/src"))

import domain  # noqa: E402

API_URL = "https://oss.exercisedb.dev/api/v1/exercises"
SCHEMA = ROOT / "server/src/schema.sql"

_PUSH_TERMS = {"chest", "shoulders", "triceps", "serratus anterior"}
_PULL_TERMS = {
    "back", "biceps", "brachialis", "forearms", "lats", "lower arms",
    "rear deltoids", "rhomboids", "traps", "upper back",
}
_LEG_TERMS = {
    "calves", "glutes", "hamstrings", "lower legs", "quads", "quadriceps",
    "upper legs",
}
_ABS_TERMS = {"abs", "abdominals", "core", "obliques", "waist"}


def _display_name(value: Any) -> str:
    """Return the source name with stable app-friendly capitalization."""
    return " ".join(str(value or "").split()).title()


def map_muscles_and_type(exercise: dict[str, Any]) -> tuple[list[str], str]:
    """Map ExerciseDB body parts and targets to app muscle/type taxonomies."""
    body_parts = [str(value).strip().casefold() for value in exercise.get("bodyParts") or []]
    targets = [str(value).strip().casefold() for value in exercise.get("targetMuscles") or []]
    terms = set(body_parts + targets)

    # Specific target muscles disambiguate broad body parts such as upper arms.
    if terms & _ABS_TERMS:
        primary = "Abs"
    elif "cardio" in terms:
        primary = "Cardio"
    elif terms & _LEG_TERMS:
        primary = "Quads" if not terms & {"hamstrings"} else "Hams"
    elif terms & _PUSH_TERMS:
        primary = "Push"
    elif terms & _PULL_TERMS:
        primary = "Pull"
    else:
        primary = "Full Body"

    workout_type = domain.workout_type_from_muscle([primary]) or "Full Body"
    return [primary], workout_type


def exercise_to_row(exercise: dict[str, Any]) -> tuple[object, ...]:
    """Convert one ExerciseDB object to a workout_library insert tuple."""
    muscles, workout_type = map_muscles_and_type(exercise)
    equipment = exercise.get("equipments") or exercise.get("equipment") or []
    if isinstance(equipment, list):
        equipment_text = next(
            (str(value).strip() for value in equipment if str(value).strip()), ""
        )
    else:
        equipment_text = str(equipment).strip()
    instructions = exercise.get("instructions") or []
    if isinstance(instructions, list):
        instruction_text = "\n".join(str(step).strip() for step in instructions if str(step).strip())
    else:
        instruction_text = str(instructions).strip()
    return (
        _display_name(exercise.get("name")), muscles, workout_type,
        equipment_text or "other", "intermediate", [],
        exercise.get("videoUrl") or exercise.get("gifUrl"), instruction_text or None,
    )


def new_rows(
    exercises: Iterable[dict[str, Any]], existing_names: Iterable[str],
) -> list[tuple[object, ...]]:
    """Return valid source rows not already present, deduped case-insensitively."""
    seen = {str(name).strip().casefold() for name in existing_names}
    rows: list[tuple[object, ...]] = []
    for exercise in exercises:
        row = exercise_to_row(exercise)
        name_key = str(row[0]).casefold()
        if not name_key or name_key in seen:
            continue
        seen.add(name_key)
        rows.append(row)
    return rows


def fetch_exercises() -> list[dict[str, Any]]:
    """Download every cursor page from ExerciseDB's no-key hosted V1 API."""
    exercises: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        url = API_URL + f"?limit=25{f'&after={cursor}' if cursor else ''}"
        request = Request(url, headers={"User-Agent": "MacroCoach importer/1.0"})
        for attempt in range(6):
            try:
                with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed host
                    payload = json.load(response)
                break
            except HTTPError as exc:
                if exc.code != 429 or attempt == 5:
                    raise
                time.sleep(float(exc.headers.get("Retry-After") or 2 ** attempt))
        exercises.extend(payload.get("data") or [])
        meta = payload.get("meta") or {}
        cursor = meta.get("nextCursor")
        if not meta.get("hasNextPage") or not cursor:
            return exercises
        time.sleep(0.1)


async def run() -> None:
    """Apply schema and add ExerciseDB rows without modifying curated rows."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    source = await asyncio.to_thread(fetch_exercises)
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(SCHEMA.read_text(encoding="utf-8"))
        existing = await conn.fetch("SELECT name FROM workout_library")
        rows = new_rows(source, (str(row["name"]) for row in existing))
        await conn.executemany(
            "INSERT INTO workout_library(name,muscle_group,workout_type,equipment,"
            "difficulty,swaps,video_url,instructions) VALUES($1,$2,$3,$4,$5,$6,$7,$8) "
            "ON CONFLICT(name) DO NOTHING",
            rows,
        )
        total = await conn.fetchval("SELECT count(*) FROM workout_library")
    finally:
        await conn.close()
    print(f"ExerciseDB import: source={len(source)} inserted={len(rows)} total={total}")


if __name__ == "__main__":
    asyncio.run(run())
