#!/usr/bin/env python3
"""Daily macro + workout summary for Telegram, read straight from Notion.

Prints a compact evening summary: today's calories/macros vs targets,
remaining, meals logged, and the next Push/Pull/Legs workout (derived from
the most recent workout in the Exercise Max Reps log).

Delivered by cron to the origin chat. Always prints (it is the daily digest).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notion import (  # noqa: E402
    NotionClient,
    read_date,
    read_number,
    read_title,
)

FITNESS_DS_ID = os.environ.get("FITNESS_DS_ID", "7a35aac7-7d31-82cd-9f2f-072f91d9f61f")
NUTRITION_DS_ID = os.environ.get("NUTRITION_DS_ID", "a9165caa-f1ba-4a3d-9d9b-c850bd1b4c4c")
TARGETS_DS_ID = os.environ.get("TARGETS_DS_ID", "28558993-430c-46f8-a3b8-de298564ecf7")
MAXREPS_DS_ID = os.environ.get("MAXREPS_DS_ID", "52a10855-2304-4b71-ac09-ab39772268a4")

P_NAME = "Name"
P_CALORIES = "Calories"
P_PROTEIN = "Protein (g)"  # Nutrition Entries naming
P_CARBS = "Carbs (g)"
P_FAT = "Fat (g)"
P_DATE = "Date"
P_MEAL = "Meal"
P_EFFECTIVE_DATE = "Effective Date"
# Macro Targets uses unsuffixed macro names.
T_CALORIES = "Calories"
T_PROTEIN = "Protein"
T_CARBS = "Carbs"
T_FAT = "Fat"
P_EXERCISE = "Exercise"
P_MAX_WEIGHT = "Max weight"
P_WORKOUT_TYPE = "Workout type"
P_DATE_ACHIEVED = "Date achieved"

ROTATION = ["Push", "Pull", "Legs"]


def _load_env() -> None:
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


def _multi(page: Mapping[str, Any], name: str) -> list[str]:
    prop = (page.get("properties") or {}).get(name) or {}
    return [o.get("name") for o in prop.get("multi_select", []) if o.get("name")]


def _macro(page: Mapping[str, Any], name: str) -> float:
    return read_number(page, name)


async def run() -> int:
    _load_env()
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        print("ERROR: NOTION_TOKEN not set")
        return 1

    client = NotionClient(token)
    try:
        today = datetime.now().astimezone().date().isoformat()

        meals = await client.query_data_source(
            NUTRITION_DS_ID,
            filter={"property": P_DATE, "date": {"equals": today}},
        )
        targets_pages = await client.query_data_source(
            TARGETS_DS_ID,
            filter={
                "property": P_EFFECTIVE_DATE,
                "date": {"on_or_before": today},
            },
            sorts=[{"property": P_EFFECTIVE_DATE, "direction": "descending"}],
        )

        totals = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
        meal_lines: list[str] = []
        for page in meals:
            name = read_title(page, P_NAME) or "?"
            meal_slot = (
                (page.get("properties", {}).get(P_MEAL) or {}).get("select") or {}
            ).get("name") or "Snack"
            cal = _macro(page, P_CALORIES)
            totals["calories"] += cal
            totals["protein"] += _macro(page, P_PROTEIN)
            totals["carbs"] += _macro(page, P_CARBS)
            totals["fat"] += _macro(page, P_FAT)
            meal_lines.append(f"  🍽 {name} ({meal_slot}) — {cal:g} kcal")

        targets = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
        if targets_pages:
            row = targets_pages[0]
            targets["calories"] = read_number(row, T_CALORIES)
            targets["protein"] = read_number(row, T_PROTEIN)
            targets["carbs"] = read_number(row, T_CARBS)
            targets["fat"] = read_number(row, T_FAT)

        # Next workout: latest Date achieved in Exercise Max Reps -> rotation.
        prs = await client.query_data_source(
            MAXREPS_DS_ID,
            sorts=[{"property": P_DATE_ACHIEVED, "direction": "descending"}],
        )
        last_type: str | None = None
        next_type: str | None = None
        if prs:
            types = _multi(prs[0], P_WORKOUT_TYPE)
            last_type = next((t for t in ROTATION if t in types), None)
            if last_type:
                next_type = ROTATION[(ROTATION.index(last_type) + 1) % len(ROTATION)]

        lines: list[str] = []
        lines.append(f"📊 *Macros — {today}*")
        lines.append(
            f"  🔥 {totals['calories']:g}/{targets['calories']:g} kcal"
            f" ({targets['calories'] - totals['calories']:+.0f})"
        )
        lines.append(
            f"  🥩 {totals['protein']:g}/{targets['protein']:g}g protein"
            f" ({targets['protein'] - totals['protein']:+.0f})"
        )
        lines.append(
            f"  🌾 {totals['carbs']:g}/{targets['carbs']:g}g carbs"
            f" ({targets['carbs'] - totals['carbs']:+.0f})"
        )
        lines.append(
            f"  🧈 {totals['fat']:g}/{targets['fat']:g}g fat"
            f" ({targets['fat'] - totals['fat']:+.0f})"
        )
        if meal_lines:
            lines.append(f"\n_Meals ({len(meal_lines)}):_")
            lines.extend(meal_lines)
        else:
            lines.append("\n_No meals logged yet today._")

        if next_type:
            lines.append(f"\n💪 *Next workout: {next_type} day*")
        elif last_type:
            lines.append(f"\n💪 Last logged: {last_type} day (no rotation info)")
        else:
            lines.append("\n💪 No workout data yet — log one to start PR tracking.")

        print("\n".join(lines))
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
