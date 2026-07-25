#!/usr/bin/env python3
"""One-time (and re-runnable) Notion setup for the macro tracker.

What it does, idempotently:

1. Adds a `Snack` option to the existing `Meal` select on `Nutrition Entries`,
   sending the complete option list so the existing three survive.
2. Creates `Macro Targets` and `Meal Presets` under the `Daily Food Log` page,
   locating them by title first so a second run creates nothing.
3. Seeds one `Macro Targets` row dated 2026-01-01 if the database is empty.
4. Prints the resulting data source IDs in .env format.

Usage:  NOTION_TOKEN=secret_... python setup.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Mapping

SRC = Path(__file__).resolve().parent / "server" / "src"
sys.path.insert(0, str(SRC))

from config import (  # noqa: E402
    DEFAULT_NUTRITION_DB_ID,
    DEFAULT_NUTRITION_DS_ID,
    DEFAULT_PARENT_PAGE_ID,
    _load_dotenv,
)
import notion as notion_api  # noqa: E402
from notion import NotionClient, NotionError  # noqa: E402

TARGETS_DB_TITLE = "Macro Targets"
PRESETS_DB_TITLE = "Meal Presets"

MEAL_OPTIONS = ["Breakfast", "Lunch", "Dinner", "Snack"]

TARGETS_SCHEMA: dict[str, Any] = {
    "Name": {"title": {}},
    "Effective Date": {"date": {}},
    "Calories": {"number": {}},
    "Protein": {"number": {}},
    "Carbs": {"number": {}},
    "Fat": {"number": {}},
}

PRESETS_SCHEMA: dict[str, Any] = {
    "Name": {"title": {}},
    "Emoji": {"rich_text": {}},
    "Calories": {"number": {}},
    "Protein": {"number": {}},
    "Carbs": {"number": {}},
    "Fat": {"number": {}},
    "Meal": {"select": {"options": [{"name": name} for name in MEAL_OPTIONS]}},
    "Sort Order": {"number": {}},
    "Active": {"checkbox": {}},
}

SEED_TARGETS = {
    "name": "Initial",
    "effective_date": "2026-01-01",
    "calories": 2400,
    "protein": 215,
    "carbs": 200,
    "fat": 70,
}


def info(message: str) -> None:
    print(f"  {message}")


def step(message: str) -> None:
    print(f"\n> {message}")


async def ensure_snack_option(client: NotionClient, data_source_id: str) -> None:
    """Add `Snack` to the `Meal` select without disturbing the existing options."""
    step("Checking the `Meal` select on `Nutrition Entries`")
    data_source = await client.get_data_source(data_source_id)
    properties: Mapping[str, Any] = data_source.get("properties") or {}
    meal = properties.get(notion_api.P_MEAL)
    if meal is None:
        raise SystemExit(
            "  ! `Nutrition Entries` has no `Meal` property. Nothing was changed — "
            "check that NUTRITION_DS_ID points at the right data source."
        )
    if meal.get("type") != "select":
        raise SystemExit(f"  ! `Meal` is a {meal.get('type')}, expected a select.")

    existing = list((meal.get("select") or {}).get("options") or [])
    names = [option.get("name") for option in existing]
    info(f"existing options: {', '.join(name for name in names if name) or '(none)'}")

    if "Snack" in names:
        info("`Snack` already exists — nothing to do.")
        return

    # Notion replaces the option set wholesale: resend every existing option.
    options = [
        {key: option[key] for key in ("id", "name", "color") if key in option}
        for option in existing
    ]
    options.append({"name": "Snack"})
    await client.update_data_source(
        data_source_id, {notion_api.P_MEAL: {"select": {"options": options}}}
    )
    info("added `Snack` (existing options preserved).")


async def find_child_database(client: NotionClient, page_id: str, title: str) -> str:
    """Database id of a child database of `page_id` with this exact title, or ''."""
    for block in await client.child_databases(page_id):
        if (block.get("title") or "").strip().lower() == title.lower():
            return block["id"]
    return ""


async def ensure_database(
    client: NotionClient, parent_page_id: str, title: str, schema: Mapping[str, Any]
) -> str:
    """Return the data source id for `title`, creating the database if absent."""
    step(f"Checking database `{title}`")
    database_id = await find_child_database(client, parent_page_id, title)

    if database_id:
        info(f"found existing database {database_id}")
        data_source_id = await client.primary_data_source_id(database_id)
        data_source = await client.get_data_source(data_source_id)
        existing = data_source.get("properties") or {}
        missing = {
            name: definition
            for name, definition in schema.items()
            if name not in existing
        }
        if missing:
            await client.update_data_source(data_source_id, missing)
            info(f"added missing properties: {', '.join(missing)}")
        else:
            info("schema already complete.")
        return data_source_id

    info("not found — creating it.")
    database = await client.create_database(parent_page_id, title, schema)
    database_id = database["id"]
    sources = database.get("data_sources") or []
    data_source_id = (
        sources[0]["id"] if sources else await client.primary_data_source_id(database_id)
    )
    info(f"created database {database_id}")
    return data_source_id


async def seed_targets(client: NotionClient, data_source_id: str) -> None:
    step("Checking `Macro Targets` seed row")
    rows = await client.query_data_source(data_source_id)
    if rows:
        info(f"{len(rows)} row(s) already present — leaving them alone.")
        return
    await client.create_page(
        data_source_id,
        {
            notion_api.P_NAME: notion_api.title_prop(SEED_TARGETS["name"]),
            notion_api.P_EFFECTIVE_DATE: notion_api.date_prop(
                SEED_TARGETS["effective_date"]
            ),
            notion_api.P_CALORIES: notion_api.number_prop(SEED_TARGETS["calories"]),
            notion_api.P_PROTEIN: notion_api.number_prop(SEED_TARGETS["protein"]),
            notion_api.P_CARBS: notion_api.number_prop(SEED_TARGETS["carbs"]),
            notion_api.P_FAT: notion_api.number_prop(SEED_TARGETS["fat"]),
        },
    )
    info(
        "seeded `Initial` dated 2026-01-01 — "
        f"{SEED_TARGETS['calories']} kcal / {SEED_TARGETS['protein']}p / "
        f"{SEED_TARGETS['carbs']}c / {SEED_TARGETS['fat']}f. Edit it in Notion."
    )


async def run() -> int:
    _load_dotenv()
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        print(
            "NOTION_TOKEN is not set.\n"
            "Create an internal integration at https://www.notion.so/my-integrations, "
            "copy the secret, then run:\n\n"
            "    NOTION_TOKEN=secret_... python setup.py\n"
        )
        return 1

    parent_page_id = os.environ.get("PARENT_PAGE_ID", DEFAULT_PARENT_PAGE_ID).strip()
    nutrition_ds_id = os.environ.get("NUTRITION_DS_ID", "").strip()

    print("Macro tracker — Notion setup")
    print(f"Parent page: {parent_page_id}")

    async with NotionClient(token) as client:
        if not nutrition_ds_id:
            step("Resolving the `Nutrition Entries` data source id")
            nutrition_ds_id = await client.primary_data_source_id(
                DEFAULT_NUTRITION_DB_ID
            )
            info(nutrition_ds_id)
        if nutrition_ds_id != DEFAULT_NUTRITION_DS_ID:
            info(f"note: using NUTRITION_DS_ID={nutrition_ds_id}")

        await ensure_snack_option(client, nutrition_ds_id)

        targets_ds_id = await ensure_database(
            client, parent_page_id, TARGETS_DB_TITLE, TARGETS_SCHEMA
        )
        presets_ds_id = await ensure_database(
            client, parent_page_id, PRESETS_DB_TITLE, PRESETS_SCHEMA
        )
        await seed_targets(client, targets_ds_id)

    print("\nDone. Paste these into server/.env (and Render's environment):\n")
    print(f"NUTRITION_DS_ID={nutrition_ds_id}")
    print(f"TARGETS_DS_ID={targets_ds_id}")
    print(f"PRESETS_DS_ID={presets_ds_id}")
    print(f"PARENT_PAGE_ID={parent_page_id}")
    print("\nRunning this again is safe — it creates nothing that already exists.")
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except NotionError as exc:
        print(f"\nNotion rejected a call: {exc}\n")
        if exc.status == 404:
            print(
                "A 404 almost always means the integration has not been shared with "
                "the page. Open `Daily Food Log` in Notion -> ... -> Connections -> "
                "add your integration, then run this again."
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
