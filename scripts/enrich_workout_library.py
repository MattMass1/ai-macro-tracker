#!/usr/bin/env python3
"""One-time ExerciseDB media and instruction enrichment for workout_library."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import asyncpg

SCHEMA = Path(__file__).resolve().parents[1] / "server/src/schema.sql"
API_URL = "https://oss.exercisedb.dev/api/v1/exercises"


def normalize_name(value: str) -> str:
    """Normalize exercise names for conservative fuzzy matching."""
    value = re.sub(r"\([^)]*\)", " ", value.casefold())
    replacements = {
        "dumbbell": "db", "barbell": "bb", "pull ups": "pull up",
        "pullups": "pull up", "squats": "squat", "curls": "curl",
        "raises": "raise", "lunges": "lunge", "planks": "plank",
    }
    words = re.findall(r"[a-z0-9]+", value)
    normalized = " ".join(words)
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    return normalized


def match_exercise(name: str, exercises: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a high-confidence exact or fuzzy ExerciseDB name match."""
    wanted = normalize_name(name)
    ranked = sorted(
        ((SequenceMatcher(None, wanted, normalize_name(str(row.get("name", "")))).ratio(), row)
         for row in exercises),
        key=lambda item: item[0], reverse=True,
    )
    if not ranked:
        return None
    score, row = ranked[0]
    candidate = normalize_name(str(row.get("name", "")))
    if candidate == wanted or score >= 0.78:
        return row
    return None


def fetch_exercises() -> list[dict[str, Any]]:
    """Download every page from ExerciseDB's no-key hosted V1 API."""
    exercises: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        url = API_URL + f"?limit=25{f'&after={cursor}' if cursor else ''}"
        request = Request(url, headers={"User-Agent": "MacroCoach enrichment/1.0"})
        for attempt in range(6):
            try:
                with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed trusted host
                    payload = json.load(response)
                break
            except HTTPError as exc:
                if exc.code != 429 or attempt == 5:
                    raise
                retry_after = exc.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 2 ** attempt)
        exercises.extend(payload.get("data") or [])
        meta = payload.get("meta") or {}
        cursor = meta.get("nextCursor")
        if not meta.get("hasNextPage") or not cursor:
            return exercises
        time.sleep(0.1)


async def run() -> None:
    """Apply schema and enrich existing library rows without clearing data."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    source = await asyncio.to_thread(fetch_exercises)
    conn = await asyncpg.connect(database_url)
    matched = 0
    try:
        await conn.execute(SCHEMA.read_text(encoding="utf-8"))
        rows = await conn.fetch("SELECT name FROM workout_library ORDER BY lower(name)")
        for library_row in rows:
            match = match_exercise(str(library_row["name"]), source)
            if match is None:
                continue
            media_url = match.get("videoUrl") or match.get("gifUrl")
            instructions = match.get("instructions")
            instruction_text = "\n".join(str(step) for step in instructions) if instructions else None
            if not media_url and not instruction_text:
                continue
            await conn.execute(
                "UPDATE workout_library SET video_url=$1,instructions=$2 WHERE name=$3",
                media_url, instruction_text, library_row["name"],
            )
            matched += 1
    finally:
        await conn.close()
    print(f"Workout library enrichment: matched={matched} total={len(rows)} source={len(source)}")


if __name__ == "__main__":
    asyncio.run(run())
