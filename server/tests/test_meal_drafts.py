"""Durable conversational meal drafts against a real (throwaway) PostgreSQL.

Runs only when MMMACROS_TEST_PG_ADMIN_URL points at a disposable local server.
Synthetic users only; every food resolves from source-pinned local fixtures
(the curated generic table), so no provider/network call is made.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from uuid import UUID, uuid4

import pytest

ADMIN_URL = os.environ.get("MMMACROS_TEST_PG_ADMIN_URL", "")
pg = pytest.mark.skipif(not ADMIN_URL, reason="set MMMACROS_TEST_PG_ADMIN_URL to a throwaway local PostgreSQL")

LUNCHTIME = datetime(2026, 10, 15, 12, 30)


def _db_url(name: str) -> str:
    base, _, query = ADMIN_URL.partition("?")
    root = base.rsplit("/", 1)[0]
    return f"{root}/{name}" + (f"?{query}" if query else "")


@pytest.fixture
async def env(monkeypatch):
    import asyncpg
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    monkeypatch.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import domain
    import migrations
    import server as srv
    from store import Store
    name = "mmm_drafts_" + uuid4().hex[:12]
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    url = _db_url(name)
    digest = migrations.checksum(migrations.MIGRATIONS_DIR / "004_meal_drafts.sql")
    monkeypatch.setenv("APPROVED_MIGRATIONS", f"004_meal_drafts.sql:{digest}")
    # 003 stays held, so 004 is held behind it: apply 004 explicitly after 000-002.
    await migrations.apply_migrations(url)
    conn = await asyncpg.connect(url)
    await conn.execute((migrations.MIGRATIONS_DIR / "004_meal_drafts.sql").read_text())
    users = [uuid4(), uuid4()]
    for user in users:
        await conn.execute("INSERT INTO users(id, display_name) VALUES($1, 'Synthetic test user')", user)
    await conn.close()

    async def connect(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
        return self.pool
    monkeypatch.setattr(Store, "connect", connect)
    store = Store(url)

    async def no_provider(*_a, **_k):
        return None
    monkeypatch.setattr(srv.food_lookup.cofid_lookup, "lookup", lambda _q: asyncio.sleep(0, result=None))
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    monkeypatch.setattr(srv, "_closest_food_options", lambda _d: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(srv, "_client", store)
    srv._resolved_food_cache.clear()
    calls: list[str] = []
    real_resolve = srv._resolve_draft_component

    async def counting(component):
        calls.append(component["description"])
        return await real_resolve(component)

    from meal_drafts import MealDrafts
    clock = {"now": LUNCHTIME.replace(tzinfo=domain.LOCAL_TZ)}
    engine = MealDrafts(store=store, resolve=counting, unresolved_question=srv._draft_unresolved_question,
                        validate_values=srv._validated_meal_values, clock=lambda: clock["now"])
    yield {"engine": engine, "store": store, "users": users, "calls": calls, "url": url, "clock": clock,
           "srv": srv}
    await store.aclose()
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()


def _as(user):
    from auth import bind_user
    return bind_user(user)


async def _rows(url, sql, *args):
    import asyncpg
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


BEEF_APPLE = {"add": [{"description": "93/7 ground beef", "quantity": 3, "unit": "oz"},
                      {"description": "green apple", "quantity": 1}]}


@pg
async def test_beef_and_apple_asks_only_raw_or_cooked_then_logs_once(env):
    from auth import reset_user
    engine, url = env["engine"], env["url"]
    token = _as(env["users"][0])
    try:
        first = await engine.handle(BEEF_APPLE)
        assert first["status"] == "needs_detail"
        assert first["question"] == "Was that 3 oz of 93/7 ground beef weighed raw or cooked?"
        assert [i["text"] for i in first["items"]] == ["3 oz 93/7 ground beef", "1 green apple"]
        assert env["calls"] == ["green apple"]  # the apple resolved; beef waits, nothing guessed
        assert await _rows(url, "SELECT 1 FROM nutrition_entries") == []
        second = await engine.handle({"draft_ref": first["draft_ref"],
                                      "edit": [{"item_ref": "item1", "preparation": "raw"}]})
    finally:
        reset_user(token)
    assert second["status"] == "committed", second
    # The apple is NOT looked up again after the answer.
    assert env["calls"] == ["green apple", "raw 93/7 ground beef"]
    rows = await _rows(url, "SELECT * FROM nutrition_entries")
    assert len(rows) == 1
    row = rows[0]
    # 3 oz raw = 85.0485 g x 152/100 + 182 g apple x 51/100 (curated panels).
    assert float(row["calories"]) == round(85.0485 * 1.52, 2) + round(182 * 0.51, 2)
    assert row["meal"] == "Lunch" and row["entry_revision"] == 1
    assert row["food_item_id"] is None  # no shared-catalog snapshot of a private meal
    assert "weighed raw" in row["name"] and "93/7" in row["name"]
    assert "standard 182 g serving" in second["confirmation"]  # apple size assumption disclosed


@pg
async def test_cooked_weight_uses_cooked_panel_not_raw_math(env):
    from auth import reset_user
    token = _as(env["users"][0])
    try:
        result = await env["engine"].handle({"add": [
            {"description": "93/7 ground beef", "quantity": 100, "unit": "g", "preparation": "cooked"}]})
    finally:
        reset_user(token)
    assert result["status"] == "committed", result
    assert result["logged"]["calories"] == 209 and "FDC 174755" in result["logged"]["macro_source"]


@pg
async def test_confirmed_preparation_default_is_reused_and_disclosed(env):
    from auth import reset_user
    engine = env["engine"]
    token = _as(env["users"][0])
    try:
        first = await engine.handle({"add": [{"description": "93/7 ground beef", "quantity": 4, "unit": "oz"}]})
        done = await engine.handle({"draft_ref": first["draft_ref"],
                                    "remember_preparation": [{"item_ref": "item1", "preparation": "cooked"}]})
        assert done["status"] == "committed"
        later = await engine.handle({"add": [{"description": "93/7 ground beef", "quantity": 4, "unit": "oz"}]})
    finally:
        reset_user(token)
    assert later["status"] == "committed"  # no repeat question
    assert "used your usual cooked weight" in later["confirmation"].casefold()


@pg
async def test_restart_keeps_the_pending_meal(env):
    from auth import reset_user
    from meal_drafts import MealDrafts
    srv = env["srv"]
    token = _as(env["users"][0])
    try:
        first = await env["engine"].handle(BEEF_APPLE)
        # A fresh engine (new process) sees the same open meal in its context.
        fresh = MealDrafts(store=env["store"], resolve=srv._resolve_draft_component,
                           unresolved_question=srv._draft_unresolved_question,
                           validate_values=srv._validated_meal_values, clock=lambda: env["clock"]["now"])
        context = await fresh.context()
        assert context["open_meals"][0]["draft_ref"] == first["draft_ref"]
        assert context["open_meals"][0]["items"][0]["needs"] == "preparation"
        done = await fresh.handle({"draft_ref": first["draft_ref"],
                                   "edit": [{"item_ref": "item1", "preparation": "cooked"}]})
    finally:
        reset_user(token)
    assert done["status"] == "committed"


@pg
async def test_amend_changes_that_entry_and_never_adds_a_second(env):
    from auth import reset_user
    engine, url = env["engine"], env["url"]
    token = _as(env["users"][0])
    try:
        logged = await engine.handle({"add": [{"description": "banana", "quantity": 1}]})
        assert logged["status"] == "committed"
        amended = await engine.handle({"amend_ref": logged["draft_ref"],
                                       "edit": [{"item_ref": "item1", "quantity": 2}]})
        # The old frozen version can't be amended again (stale head).
        stale = await engine.handle({"amend_ref": logged["draft_ref"],
                                     "edit": [{"item_ref": "item1", "quantity": 3}]})
    finally:
        reset_user(token)
    assert amended["status"] == "amended" and amended["entry_id"] == logged["entry_id"]
    assert amended["entry_revision"] == 2
    rows = await _rows(url, "SELECT calories, entry_revision FROM nutrition_entries")
    assert len(rows) == 1 and float(rows[0]["calories"]) == round(2 * 118 * 0.81, 2)
    day = await _rows(url, "SELECT calories FROM days")
    assert float(day[0]["calories"]) == float(rows[0]["calories"])
    assert stale["status"] == "conflict"


@pg
async def test_repeat_creates_a_new_entry_and_retry_replays(env):
    from auth import reset_user
    engine, url = env["engine"], env["url"]
    token = _as(env["users"][0])
    try:
        logged = await engine.handle({"add": [{"description": "banana", "quantity": 1}]})
        again = await engine.handle({"repeat_ref": logged["draft_ref"]})
        assert again["status"] == "committed" and again["entry_id"] != logged["entry_id"]
        # Re-sending the logged meal's ref returns its receipt; nothing new is written.
        replay = await engine.handle({"draft_ref": again["draft_ref"]})
    finally:
        reset_user(token)
    assert replay["status"] == "replayed" and replay["entry_id"] == again["entry_id"]
    assert len(await _rows(url, "SELECT 1 FROM nutrition_entries")) == 2


@pg
async def test_uncertain_commit_retry_replays_instead_of_writing_twice(env, monkeypatch):
    from auth import reset_user
    from store import Store
    engine, url = env["engine"], env["url"]
    real = Store.commit_meal_draft
    state = {"fail": True}

    async def flaky(self, *args, **kwargs):
        result = await real(self, *args, **kwargs)
        if state["fail"]:
            state["fail"] = False
            raise ConnectionError("response lost after commit")
        return result
    monkeypatch.setattr(Store, "commit_meal_draft", flaky)
    token = _as(env["users"][0])
    try:
        first = await engine.handle({"add": [{"description": "banana", "quantity": 1}]})
        assert first["status"] == "unknown"
        # The write did land; asking again recovers its receipt, never a second row.
        retry = await engine.handle({"draft_ref": first["draft_ref"]})
    finally:
        reset_user(token)
    assert retry["status"] == "replayed" and retry["entry_id"]
    assert len(await _rows(url, "SELECT 1 FROM nutrition_entries")) == 1


@pg
async def test_unknown_usual_asks_for_contents_then_saves_only_on_request(env):
    from auth import reset_user
    engine, url = env["engine"], env["url"]
    token = _as(env["users"][0])
    try:
        first = await engine.handle({"add": [{"description": "chick fil a", "usual": True}]})
        assert first["status"] == "needs_detail"
        assert "I don't have your usual chick fil a saved yet" in first["question"]
        logged = await engine.handle({"draft_ref": first["draft_ref"], "add": [
            {"description": "banana", "quantity": 1}, {"description": "apple", "size": "large"}]})
        assert logged["status"] == "committed" and "offer" in logged
        assert await _rows(url, "SELECT 1 FROM meal_presets") == []  # not saved without a yes
        saved = await engine.handle({"save_usual": {"draft_ref": logged["draft_ref"]}})
        assert saved["status"] == "saved_usual" and "didn't log anything" in saved["confirmation"]
        assert len(await _rows(url, "SELECT 1 FROM nutrition_entries")) == 1
        env["calls"].clear()
        usual = await engine.handle({"add": [{"description": "my usual chick fil a", "usual": True}]})
    finally:
        reset_user(token)
    assert usual["status"] == "committed", usual
    assert usual["logged"]["name"] == "Usual chick fil a"
    assert len(await _rows(url, "SELECT 1 FROM nutrition_entries")) == 2
    # Saved items, not a restaurant default, were expanded.
    assert sorted(env["calls"]) == ["apple", "banana"]


@pg
async def test_removing_an_item_from_a_pending_meal(env):
    from auth import reset_user
    token = _as(env["users"][0])
    try:
        first = await env["engine"].handle(BEEF_APPLE)
        done = await env["engine"].handle({"draft_ref": first["draft_ref"], "remove": ["item1"]})
    finally:
        reset_user(token)
    assert done["status"] == "committed" and "beef" not in done["logged"]["name"]


@pg
async def test_planned_food_never_logs(env):
    from auth import reset_user
    token = _as(env["users"][0])
    try:
        planned = await env["engine"].handle({"intent": "plan", "add": [{"description": "banana", "quantity": 1}]})
    finally:
        reset_user(token)
    assert planned["status"] == "planned"
    assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []


@pg
async def test_other_users_cannot_touch_a_draft(env):
    from auth import reset_user
    engine = env["engine"]
    token = _as(env["users"][0])
    try:
        mine = await engine.handle(BEEF_APPLE)
    finally:
        reset_user(token)
    token = _as(env["users"][1])
    try:
        stolen = await engine.handle({"draft_ref": mine["draft_ref"],
                                      "edit": [{"item_ref": "item1", "preparation": "raw"}]})
        context = await engine.context()
    finally:
        reset_user(token)
    assert stolen["status"] == "not_found"
    assert context == {"open_meals": [], "logged_meals": []}
    assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []


@pg
async def test_database_refuses_cross_tenant_child_rows(env):
    import asyncpg
    from auth import reset_user
    token = _as(env["users"][0])
    try:
        mine = await env["engine"].handle(BEEF_APPLE)
    finally:
        reset_user(token)
    conn = await asyncpg.connect(env["url"])
    try:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO meal_draft_items(id,user_id,draft_id,position,description) VALUES($1,$2,$3,9,'x')",
                uuid4(), env["users"][1], UUID(mine["draft_ref"]))
    finally:
        await conn.close()


@pg
async def test_logging_day_is_pinned_across_4am(env):
    from auth import reset_user
    import domain
    engine, clock = env["engine"], env["clock"]
    clock["now"] = datetime(2026, 10, 15, 23, 50, tzinfo=domain.LOCAL_TZ)
    token = _as(env["users"][0])
    try:
        first = await engine.handle(BEEF_APPLE)
        clock["now"] = datetime(2026, 10, 16, 4, 30, tzinfo=domain.LOCAL_TZ)
        done = await engine.handle({"draft_ref": first["draft_ref"],
                                    "edit": [{"item_ref": "item1", "preparation": "raw"}]})
    finally:
        reset_user(token)
    assert done["logged"]["date"] == "2026-10-15"


# --------------------------------------------------------------------------- #
# Review fixes (ce93c26 review: B1, S1, S2, S3, S5, S6)
# --------------------------------------------------------------------------- #

@pg
async def test_amend_keeps_the_logged_slot_when_time_moves_on(env):
    import domain
    from auth import reset_user
    engine, url, clock = env["engine"], env["url"], env["clock"]
    token = _as(env["users"][0])
    try:
        logged = await engine.handle({"add": [{"description": "banana", "quantity": 1}]})
        clock["now"] = datetime(2026, 10, 15, 19, 0, tzinfo=domain.LOCAL_TZ)
        amended = await engine.handle({"amend_ref": logged["draft_ref"],
                                       "edit": [{"item_ref": "item1", "quantity": 2}]})
        moved = await engine.handle({"amend_ref": amended["draft_ref"], "meal_type": "Snack"})
    finally:
        reset_user(token)
    assert amended["status"] == "amended" and amended["logged"]["meal"] == "Lunch"
    assert moved["status"] == "amended" and moved["logged"]["meal"] == "Snack"  # only when asked
    rows = await _rows(url, "SELECT meal_type FROM meals")
    assert [r["meal_type"] for r in rows] == ["Snack"]  # emptied Lunch rollup removed


@pg
async def test_late_night_defaults_to_snack_on_the_previous_logging_day(env):
    import domain
    from auth import reset_user
    env["clock"]["now"] = datetime(2026, 10, 16, 1, 30, tzinfo=domain.LOCAL_TZ)
    token = _as(env["users"][0])
    try:
        result = await env["engine"].handle({"add": [{"description": "banana", "quantity": 1}]})
    finally:
        reset_user(token)
    assert result["logged"]["meal"] == "Snack" and result["logged"]["date"] == "2026-10-15"


@pg
async def test_grilled_is_already_cooked_and_raw_weight_never_uses_a_cooked_panel(env):
    from auth import reset_user
    engine, url = env["engine"], env["url"]
    token = _as(env["users"][0])
    try:
        grilled = await engine.handle({"add": [{"description": "grilled chicken", "quantity": 100, "unit": "g"}]})
        raw = await engine.handle({"add": [
            {"description": "grilled chicken", "quantity": 100, "unit": "g", "preparation": "raw"}]})
    finally:
        reset_user(token)
    assert grilled["status"] == "committed"  # no raw/cooked question for grilled
    assert raw["status"] == "needs_detail"  # cooked panel refused for a raw weight
    assert len(await _rows(url, "SELECT 1 FROM nutrition_entries")) == 1


@pg
async def test_editing_a_usual_placeholder_names_its_food(env):
    from auth import reset_user
    engine = env["engine"]
    token = _as(env["users"][0])
    try:
        first = await engine.handle({"add": [{"description": "chick fil a", "usual": True}]})
        second = await engine.handle({"draft_ref": first["draft_ref"],
                                      "edit": [{"item_ref": "item1", "description": "banana"}]})
    finally:
        reset_user(token)
    assert second["status"] == "committed", second


@pg
async def test_legacy_preset_quantity_amend_scales_its_panel(env):
    from auth import reset_user
    engine, store = env["engine"], env["store"]
    token = _as(env["users"][0])
    try:
        await store.save_preset({"name": "protein shake", "emoji": "x", "calories": 200, "protein": 30,
                                 "carbs": 5, "fat": 3, "fiber": 1, "meal": "Snack",
                                 "macro_source": "Label: shake"})
        logged = await engine.handle({"add": [{"description": "protein shake", "usual": True}]})
        amended = await engine.handle({"amend_ref": logged["draft_ref"],
                                       "edit": [{"item_ref": "item1", "quantity": 2}]})
    finally:
        reset_user(token)
    assert logged["logged"]["calories"] == 200
    assert amended["status"] == "amended" and amended["logged"]["calories"] == 400
    assert amended["logged"]["name"] == "Usual protein shake x2"


@pg
async def test_bad_edit_leaves_no_stray_draft(env):
    from auth import reset_user
    from meal_drafts import DraftInputError
    engine = env["engine"]
    token = _as(env["users"][0])
    try:
        logged = await engine.handle({"add": [{"description": "banana", "quantity": 1}]})
        with pytest.raises(DraftInputError):
            await engine.handle({"repeat_ref": logged["draft_ref"], "edit": [{"item_ref": "item9", "quantity": 2}]})
        with pytest.raises(DraftInputError):
            await engine.handle({"add": [{"description": "banana", "unit": "!!"}]})
    finally:
        reset_user(token)
    assert await _rows(env["url"], "SELECT 1 FROM meal_drafts WHERE state <> 'committed'") == []


@pg
async def test_repeat_asks_again_for_a_web_estimate(env):
    from auth import reset_user
    engine = env["engine"]
    estimate = {"name": "fixture stew", "macro_source": "WEB ESTIMATE (unverified): example.test",
                "calories": 300, "protein": 20, "carbs": 30, "fat": 10, "fiber": 2,
                "estimate_provenance": "server_web_estimate", "attribution": {"source_host": "example.test"}}
    real = engine.resolve

    async def resolve(component):
        return dict(estimate) if "stew" in component["description"] else await real(component)
    engine.resolve = resolve
    token = _as(env["users"][0])
    try:
        first = await engine.handle({"add": [{"description": "fixture stew", "quantity": 1}]})
        assert first["status"] == "needs_detail" and "web estimate" in first["question"]
        logged = await engine.handle({"draft_ref": first["draft_ref"], "consent_estimate": ["item1"]})
        assert logged["status"] == "committed", logged
        again = await engine.handle({"repeat_ref": logged["draft_ref"]})
    finally:
        reset_user(token)
    assert again["status"] == "needs_detail" and "web estimate" in again["question"]



# --------------------------------------------------------------------------- #
# Ahi tuna loop (reported 2026-10-16): raw/cooked answered, then "how much?"
# asked again and again after the user had already said 3 oz.
# --------------------------------------------------------------------------- #

@pg
async def test_ahi_tuna_by_weight_logs_after_raw_or_cooked(env):
    from auth import reset_user
    token = _as(env["users"][0])
    try:
        first = await env["engine"].handle({"add": [{"description": "ahi tuna", "quantity": 3, "unit": "oz"}]})
        assert first["question"] == "Was that 3 oz of ahi tuna weighed raw or cooked?"
        raw = await env["engine"].handle({"draft_ref": first["draft_ref"],
                                          "edit": [{"item_ref": "item1", "preparation": "raw"}]})
        cooked = await env["engine"].handle({"add": [
            {"description": "ahi tuna", "quantity": 3, "unit": "oz", "preparation": "cooked"}]})
    finally:
        reset_user(token)
    assert raw["status"] == "committed", raw
    assert raw["logged"]["calories"] == round(85.0485 * 1.09, 2) and "FDC 175159" in raw["logged"]["macro_source"]
    assert cooked["status"] == "committed" and "FDC 172006" in cooked["logged"]["macro_source"]


@pg
async def test_never_asks_how_much_again_after_an_amount_was_given(env):
    from auth import reset_user
    engine = env["engine"]

    async def unweighable(component):  # a provider panel with no weight
        return {"portion_error": True, "name": component["description"]}
    engine.resolve = unweighable
    token = _as(env["users"][0])
    try:
        first = await engine.handle({"add": [{"description": "fixture poke bowl", "quantity": 3, "unit": "oz"}]})
        again = await engine.handle({"draft_ref": first["draft_ref"],
                                     "edit": [{"item_ref": "item1", "quantity": 3, "unit": "oz"}]})
        unstated = await engine.handle({"add": [{"description": "fixture poke bowl"}]})
    finally:
        reset_user(token)
    for result in (first, again):
        assert result["status"] == "needs_detail"
        assert "How much" not in result["question"]
        assert "can't be measured as 3 oz" in result["question"]
    assert unstated["question"] == "How much fixture poke bowl was it?"  # only when no amount was given
