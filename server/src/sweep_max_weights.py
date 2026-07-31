#!/usr/bin/env python3
"""Nightly max-weight sweep: Fitness Tracker -> Exercise Max Reps (absolute PRs).

Reads every row of the `Fitness Tracker` data source, finds the heaviest set
ever logged per exercise (max across Weight 1-4 and across all entries), and
makes `Exercise Max Reps` match reality:

  * creates a row for exercises not yet tracked,
  * updates a row when the stored max was beaten (new weight, date achieved,
    source entry, workout type),
  * backfills the `Workout type` multi-select when it is empty but the source
    entry has a type.

Idempotent: running it twice changes nothing the second time. Never lowers a
stored max. Designed for a nightly cron:

    .venv/bin/python src/sweep_max_weights.py            # apply changes
    .venv/bin/python src/sweep_max_weights.py --dry-run  # preview only

Prints nothing when nothing changed (watchdog pattern for cron: empty stdout =
silent delivery).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notion import (  # noqa: E402
    NotionClient,
    date_prop,
    number_prop,
    read_date,
    read_number,
    read_title,
    title_prop,
)

#: Source of truth: every logged workout set.
FITNESS_DS_ID = os.environ.get("FITNESS_DS_ID", "7a35aac7-7d31-82cd-9f2f-072f91d9f61f")
#: The PR log the sweep maintains.
MAXREPS_DS_ID = os.environ.get("MAXREPS_DS_ID", "52a10855-2304-4b71-ac09-ab39772268a4")

P_EXERCISE = "Exercise Name"  # Fitness Tracker title
P_MAXREPS_EXERCISE = "Exercise"  # Max Reps title
P_MAX_WEIGHT = "Max weight"
P_DATE_ACHIEVED = "Date achieved"
P_SOURCE_ENTRY = "Source entry"
P_WORKOUT_TYPE = "Workout type"
P_MUSCLE_GROUP = "Muscle Group"
P_DATE_INPUT = "Date (user input)"

WEIGHT_COLS = [f"Weight {i}" for i in range(1, 5)]

#: `Muscle Group` values map onto the `Workout type` taxonomy. The Fitness
#: Tracker rows only populate Muscle Group; the Max Reps log carries Workout
#: type so the PWA can group exercises into Push/Pull/Legs days.
MUSCLE_TO_WORKOUT_TYPE = {
    "Push": "Push",
    "Pull": "Pull",
    "Abs": "Abs",
    "Quads": "Legs",
    "Hams": "Legs",
    "Legs": "Legs",
    "Cardio": "Cardio",
    "Full Body": "Full Body",
}


def _load_env() -> None:
    """Load server/.env if present — the same file the service reads."""
    for candidate in (
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
    ):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return


def _multi_select(page: Mapping[str, Any], name: str) -> list[str]:
    prop = (page.get("properties") or {}).get(name) or {}
    return [
        option.get("name")
        for option in prop.get("multi_select", [])
        if option.get("name")
    ]


def _relation_id(page: Mapping[str, Any], name: str) -> str:
    prop = (page.get("properties") or {}).get(name) or {}
    relations = prop.get("relation", [])
    return relations[0].get("id", "") if relations else ""


def _source_date(page: Mapping[str, Any]) -> str:
    """Best date for a PR: the user-entered date, else the row's created time."""
    entered = read_date(page, P_DATE_INPUT)
    if entered:
        return entered
    created = page.get("created_time") or ""
    return created[:10]


def _workout_types(page: Mapping[str, Any]) -> list[str]:
    """Workout type tags for a row, derived from Muscle Group when unset."""
    types = _multi_select(page, P_WORKOUT_TYPE)
    if types:
        return types
    muscles = _multi_select(page, P_MUSCLE_GROUP)
    return sorted(
        {
            mapped
            for muscle in muscles
            if (mapped := MUSCLE_TO_WORKOUT_TYPE.get(muscle))
        }
    )


def _page_max_weight(page: Mapping[str, Any]) -> float:
    """Heaviest set on one Fitness Tracker row."""
    return max((read_number(page, col) for col in WEIGHT_COLS), default=0.0)


def _key(name: str) -> str:
    return name.strip().casefold()


def compute_prs(fitness_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Highest weight ever logged per exercise, with its source entry."""
    prs: dict[str, dict[str, Any]] = {}
    for page in fitness_rows:
        name = read_title(page, P_EXERCISE).strip()
        if not name:
            continue
        weight = _page_max_weight(page)
        if weight <= 0:
            continue
        key = _key(name)
        current = prs.get(key)
        if current is None or weight > current["weight"]:
            prs[key] = {
                "name": name,
                "weight": weight,
                "page_id": page.get("id", ""),
                "date": _source_date(page),
                "workout_type": _workout_types(page),
            }
    return prs


def load_max_reps(maxreps_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Existing Exercise Max Reps rows keyed by normalized exercise name."""
    rows: dict[str, dict[str, Any]] = {}
    for page in maxreps_rows:
        name = read_title(page, P_MAXREPS_EXERCISE).strip()
        if not name:
            continue
        rows[_key(name)] = {
            "page_id": page.get("id", ""),
            "max_weight": read_number(page, P_MAX_WEIGHT),
            "workout_type": _multi_select(page, P_WORKOUT_TYPE),
        }
    return rows


async def run(dry_run: bool) -> int:
    _load_env()
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        print("ERROR: NOTION_TOKEN not set (copy server/.env.example to server/.env)")
        return 1

    client = NotionClient(token)
    changes: list[str] = []
    try:
        fitness_rows = await client.query_data_source(FITNESS_DS_ID)
        maxreps_rows = await client.query_data_source(MAXREPS_DS_ID)

        prs = compute_prs(fitness_rows)
        existing = load_max_reps(maxreps_rows)

        for key, pr in sorted(prs.items()):
            row = existing.get(key)
            if row is None:
                if dry_run:
                    changes.append(f"NEW: {pr['name']} -> {pr['weight']:g}")
                    continue
                await client.create_page(
                    MAXREPS_DS_ID,
                    {
                        P_MAXREPS_EXERCISE: title_prop(pr["name"]),
                        P_MAX_WEIGHT: number_prop(pr["weight"]),
                        P_DATE_ACHIEVED: date_prop(pr["date"]),
                        P_SOURCE_ENTRY: {"relation": [{"id": pr["page_id"]}]},
                        P_WORKOUT_TYPE: {
                            "multi_select": [{"name": t} for t in pr["workout_type"]]
                        },
                    },
                )
                changes.append(f"NEW: {pr['name']} -> {pr['weight']:g}")
                continue

            if pr["weight"] > row["max_weight"]:
                if dry_run:
                    changes.append(
                        f"PR: {pr['name']} {row['max_weight']:g} -> {pr['weight']:g}"
                    )
                    continue
                await client.update_page(
                    row["page_id"],
                    {
                        P_MAX_WEIGHT: number_prop(pr["weight"]),
                        P_DATE_ACHIEVED: date_prop(pr["date"]),
                        P_SOURCE_ENTRY: {"relation": [{"id": pr["page_id"]}]},
                        P_WORKOUT_TYPE: {
                            "multi_select": [{"name": t} for t in pr["workout_type"]]
                        },
                    },
                )
                changes.append(
                    f"PR: {pr['name']} {row['max_weight']:g} -> {pr['weight']:g}"
                )
                continue

            if not row["workout_type"] and pr["workout_type"]:
                if dry_run:
                    changes.append(
                        f"TAG: {pr['name']} += {', '.join(pr['workout_type'])}"
                    )
                    continue
                await client.update_page(
                    row["page_id"],
                    {
                        P_WORKOUT_TYPE: {
                            "multi_select": [{"name": t} for t in pr["workout_type"]]
                        }
                    },
                )
                changes.append(
                    f"TAG: {pr['name']} += {', '.join(pr['workout_type'])}"
                )
    finally:
        await client.aclose()

    if changes:
        prefix = "[dry-run] " if dry_run else ""
        print(f"{prefix}Max weight sweep — {len(changes)} change(s):")
        for line in changes:
            print(f"  {line}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="preview changes without writing"
    )
    args = parser.parse_args()
    return asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
