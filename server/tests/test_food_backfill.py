from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from contextlib import asynccontextmanager

import pytest


def load_script(name):
    path = Path(__file__).parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def row(row_id, name="Banana", calories=105):
    return {"id":row_id,"name":name,"macro_source":"family log","serving_text":None,
        "calories":calories,"protein":1,"carbs":27,"fat":0,"fiber":3,"created_at":None}


def test_backfill_signature_dedupes_entries_and_presets_but_preserves_links():
    backfill = load_script("backfill_food_catalog")
    rows = [("nutrition_entries", row("entry-1")), ("nutrition_entries", row("entry-2")),
            ("meal_presets", row("preset-1"))]
    assert len({backfill.stable_signature(value) for _, value in rows}) == 1
    assert backfill.projected(rows) == {"links":3,"food_items":1,"aliases":1,
        "observations":1,"source_records":1}
    assert backfill.stable_signature(row("x", calories=106)) != backfill.stable_signature(row("y"))


def test_blank_legacy_names_use_one_deterministic_unverified_placeholder():
    backfill = load_script("backfill_food_catalog")
    rows = [("nutrition_entries", row("entry", name="")),
            ("meal_presets", row("preset", name="  \t"))]
    assert backfill.projected(rows) == {"links": 2, "food_items": 1, "aliases": 1,
        "observations": 1, "source_records": 1}
    assert backfill.stable_signature(rows[0][1]) == backfill.stable_signature(rows[1][1])
    normalized, display, identity = backfill.legacy_food_identity(" \t")
    assert normalized and display == "Unnamed legacy food (unverified)"
    assert identity == "legacy:blank-name"


def test_verified_seed_covers_all_curated_items_and_known_foods_with_stable_ids():
    seed = load_script("seed_verified_foods")
    values = list(seed.records())
    assert len(values) == sum(len(items) for items in seed.RESTAURANT_MENU.values()) + len(seed.KNOWN)
    assert len({item["external_id"] for item in values}) == len(values)
    assert all(item["source_reference"] for item in values)


def test_verified_seed_uses_approved_macros_and_excludes_incomplete_foods():
    seed = load_script("seed_verified_foods")
    known = {record[0]: record[1:5] for record in seed.KNOWN}
    assert known["Chipotle bowl"] == (625, 75, 38, 22)
    assert known["Chipotle burrito"] == (945, 83, 88, 31)
    assert known["CFA 8ct grilled nuggets + grilled club + sauce"] == (710, 62, 61, 25)
    assert "93/7 ground beef" not in known
    assert "Sweet potato" not in known


def test_backfill_main_propagates_failure_for_nonzero_process_exit(monkeypatch):
    backfill = load_script("backfill_food_catalog")
    async def fail(_url, _apply):
        raise RuntimeError("backfill failed")
    monkeypatch.setattr(backfill, "run", fail)
    monkeypatch.setattr(sys, "argv", ["backfill_food_catalog.py", "--database-url", "postgresql://clone"])
    with pytest.raises(RuntimeError, match="backfill failed"):
        backfill.main()


async def test_blank_legacy_row_backfills_through_run_without_rewriting_legacy(monkeypatch):
    backfill = load_script("backfill_food_catalog")
    legacy = row("entry-blank", name="")
    legacy.update(food_item_id=None, food_observation_id=None)

    class Conn:
        @asynccontextmanager
        async def transaction(self): yield
        async def fetchrow(self, sql, *args):
            if "count(*)" in sql:
                linked = legacy["food_item_id"] is not None
                return {"entries":1,"unlinked_entries":0 if linked else 1,"presets":0,
                    "unlinked_presets":0,"food_items":1 if linked else 0,
                    "aliases":1 if linked else 0,"observations":1 if linked else 0}
            raise AssertionError(sql)
        async def fetch(self, sql, *args):
            if "FROM nutrition_entries" in sql:
                return [] if legacy["food_item_id"] else [dict(legacy)]
            if "FROM meal_presets" in sql: return []
            raise AssertionError(sql)
        async def fetchval(self, sql, *args):
            if "food_items" in sql: return "blank-item"
            if "food_source_records" in sql: return "blank-source"
            return "blank-observation"
        async def execute(self, sql, *args):
            if sql.startswith("UPDATE nutrition_entries"):
                legacy["food_item_id"], legacy["food_observation_id"] = args[:2]
                return "UPDATE 1"
            return "INSERT 0 1"
        async def close(self): pass

    conn = Conn()
    async def connect(_url): return conn
    monkeypatch.setattr(backfill.asyncpg, "connect", connect)
    result = await backfill.run("postgresql://isolated/test", apply=True)
    assert result["logical_rows_changed"] == 1
    assert legacy["name"] == ""
    assert legacy["macro_source"] == "family log"
    second = await backfill.run("postgresql://isolated/test", apply=True)
    assert second["logical_rows_changed"] == 0
