"""WP6: whole food-agent dialogues through ``CanvasService.turn``.

The model is a scripted interpreter returning typed ``AgentTurn`` objects. Food
lookups go through the real :class:`food_agent.find_food.FoodFinder` over a fake
resolver (FatSecret-shaped rows). Drafts are ``MemoryDrafts`` offline and
``StoreDrafts`` on a throwaway PostgreSQL (skipped without
MMMACROS_TEST_PG_ADMIN_URL). No network. Synthetic user UUIDs only.

This proves the canvas does the right thing GIVEN a correct interpretation.
It does not prove the model interprets correctly.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from auth import bind_user, reset_user
from food_agent.coordinator import FoodAgent
from food_agent.drafts import MemoryDrafts
from food_agent.find_food import FoodFinder
from test_agent_canvas import MemoryStore
from test_food_agent_find_food import CHOC12, QPC, Resolver, _fatsecret
from test_food_agent_flow import Clock, Script, _ident, _portion, add, revise

QP = _fatsecret("qp", "Quarter Pounder", "McDonald's", "1 sandwich", 200, 420, 26, 40, 20, 2)
RESTAURANT_ROWS = {"McDonald's Quarter Pounder": QP, "McDonald's Quarter Pounder with Cheese": QPC}

HALF_QP = add((_ident("Quarter Pounder", "McDonald's", "restaurant"),
               _portion("serving_fraction", 0.5, text="half of a")))
WITH_CHEESE = revise("i1", identity={"name": "Quarter Pounder with Cheese", "brand": None, "variant": None,
                                     "kind": None, "preparation": None})
ONE_CHOCO = add((_ident("Choco Laka smoothie", "Smoothie King", "restaurant"),
                 _portion("count", 1, text="one")))
SIX_OZ = revise("i1", portion=_portion("mass", 6, "oz", "6 oz instead of 12"))


class OldPath:
    """The pre-existing canvas model, scripted. Records every call."""

    def __init__(self, reply="Today is Push: bench, rows and dips."):
        self.reply = reply
        self.calls = []
        self.tools = []
        self.instructions = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs["message"])
        self.tools.append(sorted(set(kwargs["handlers"]) | {t["name"] for t in kwargs["tool_catalog"]}))
        self.instructions.append(kwargs["instruction_override"])
        return self.reply, []


FOOD_WRITE_TOOLS = {"log_meal", "update_meal"}


class Rig:
    def __init__(self, drafts=None, resolver=None, store=None, chat=None):
        self.clock = Clock()
        self.drafts = drafts if drafts is not None else MemoryDrafts(clock=self.clock)
        self.resolver = resolver or Resolver(RESTAURANT_ROWS)
        self.script = Script()
        self.finder = FoodFinder(self.resolver, suggest=self._no_options, is_ambiguous=lambda _q: False)
        self.agent = FoodAgent(self.drafts, self.script, self.finder, None, self.clock)
        self.chat = chat if chat is not None else MemoryStore()
        self.old = OldPath()
        self.food_calls = []
        self.factory_calls = 0
        self.session_id = str(uuid4())
        from agent_canvas import CanvasService
        self.service = CanvasService(store_factory=lambda: self.chat, food_factory=self._foods,
                                     coach_factory=lambda: {}, agent=self.old,
                                     food_agent_factory=self._factory)

    @staticmethod
    async def _no_options(_query):
        return []

    async def _factory(self):
        self.factory_calls += 1
        return self.agent

    def _foods(self):
        async def log_meal(call_id, args):
            self.food_calls.append(("log_meal", args))
            raise AssertionError("the old food write path must not run for a food-agent turn")

        async def lookup_food(call_id, args):
            self.food_calls.append(("lookup_food", args))
            raise AssertionError("the old lookup must not run for a food-agent turn")
        return {"log_meal": log_meal, "lookup_food": lookup_food}

    async def say(self, text, turn_id=None):
        return await self.service.turn(self.session_id, turn_id or str(uuid4()), text, adapter="text")


@pytest.fixture
def user():
    scope = bind_user(uuid4())
    yield
    reset_user(scope)


def _components(result):
    return {item["component"]: item for surface in result["surfaces"] for item in surface["components"]}


def _assistant_texts(chat):
    return [m["content"] for m in chat.history if m["role"] == "assistant"]


SNAPSHOT_KEYS = {"protocol", "catalog", "sessionId", "instanceId", "revision", "serverTime", "surfaces",
                 "receipt", "workout", "approval", "reply"}


# ---------------------------------------------------------------- real failure 2


async def test_half_quarter_pounder_with_cheese_through_the_canvas(user):
    rig = Rig()
    rig.script.push(HALF_QP)
    first = await rig.say("Half of a McDonald's quarter pounder")
    assert set(first) == SNAPSHOT_KEYS | {"quickReplies"}
    assert first["quickReplies"] == ["Log it", "Cancel"]
    assert rig.drafts.entries == {} and rig.drafts.commit_calls == []
    assert first["receipt"] is None and "MealReceipt" not in _components(first)
    assert "210 kcal" in first["reply"] and first["reply"].endswith("Log it?")
    assert _components(first)["FoodClarification"]["text"] == first["reply"]

    rig.script.push(WITH_CHEESE)
    second = await rig.say("quarter pounder with cheese")
    assert rig.resolver.queries[-1] == "McDonald's Quarter Pounder with Cheese"
    assert "half" not in " ".join(rig.resolver.queries).casefold()
    assert "260 kcal" in second["reply"] and "15 g protein" in second["reply"]
    assert second["reply"].endswith("Log it?") and rig.drafts.entries == {}
    (row,) = rig.drafts.drafts.values()
    state = row["items"][0]["resolution"]["state"]
    assert state["multiplier"] == 0.5 and state["portion"]["amount"] == 0.5

    model_calls = len(rig.script.calls)
    done = await rig.say("yes")
    assert len(rig.script.calls) == model_calls  # a plain yes needs no model call
    assert len(rig.drafts.commit_calls) == 1 and len(rig.drafts.entries) == 1
    (entry_id, entry), = rig.drafts.entries.items()
    assert entry["calories"] == 260.0 and entry["protein"] == 15.0  # from the 520/30 panel, applied once
    assert "Quarter Pounder with Cheese" in entry["name"]
    receipt = _components(done)["MealReceipt"]
    for text in (done["reply"], receipt["text"]):
        assert "260 kcal" in text and "15 g protein" in text and text.startswith("Logged ")
    assert done["receipt"]["recordId"] == entry_id
    assert "FoodClarification" not in _components(done)  # the question is closed
    assert _assistant_texts(rig.chat) == [first["reply"], second["reply"], done["reply"]]
    assert [m["content"] for m in rig.chat.history if m["role"] == "user"] == [
        "Half of a McDonald's quarter pounder", "quarter pounder with cheese", "yes"]
    assert rig.old.calls == [] and rig.food_calls == []


# ---------------------------------------------------------------- real failure 3


async def test_choco_laka_six_oz_instead_of_twelve_through_the_canvas(user):
    rig = Rig(resolver=Resolver(default=CHOC12))
    rig.script.push(ONE_CHOCO)
    first = await rig.say("One Choco Laka smoothie from Smoothie King")
    assert "matched to smoothie king choc-a-laka" in first["reply"].casefold()
    assert "352 kcal" in first["reply"] and first["reply"].endswith("Log it?")
    (row,) = rig.drafts.drafts.values()
    food = row["items"][0]["resolution"]["state"]["resolved"]
    assert (food["serving_basis"]["amount"], food["serving_basis"]["unit"]) == (12.0, "fl_oz")
    assert len(rig.resolver.queries) == 1

    rig.script.push(SIX_OZ)
    second = await rig.say("yes but 6 oz instead of 12")
    assert rig.drafts.commit_calls == [] and rig.drafts.entries == {}
    assert len(rig.resolver.queries) == 2  # no-cache evidence refreshed, identity remains unchanged
    (row,) = rig.drafts.drafts.values()
    state = row["items"][0]["resolution"]["state"]
    assert state["resolved"]["food_id"] == food["food_id"] and state["multiplier"] == 0.5
    assert "176 kcal" in second["reply"] and second["reply"].endswith("Log it?")
    assert second["reply"].count("?") == 1  # a preview, not a question about the amount

    done = await rig.say("yes")
    assert len(rig.drafts.commit_calls) == 1
    (entry_id, entry), = rig.drafts.entries.items()
    assert entry["calories"] == 176.0 and entry["protein"] == 6.5
    assert entry["component_metadata"]["items"][0]["food_id"] == food["food_id"]
    assert "176 kcal" in _components(done)["MealReceipt"]["text"]
    assert done["receipt"]["recordId"] == entry_id
    replies = _assistant_texts(rig.chat)
    assert sum("Log it?" in reply for reply in replies) == 2 and len(replies) == 3
    assert rig.old.calls == []


# ---------------------------------------------------------------- hand-off


async def test_non_food_message_is_handed_to_the_old_path_unchanged(user, monkeypatch):
    import itertools
    import time as time_module
    from uuid import UUID

    import agent_canvas
    from agent_canvas import CanvasService

    async def run(with_agent):
        counter = itertools.count(1)
        monkeypatch.setattr(agent_canvas, "uuid4", lambda: UUID(int=next(counter)))
        monkeypatch.setattr(time_module, "time", lambda: 1_800_000_000.0)
        rig = Rig()
        rig.script.push({"type": "not_food"})
        if not with_agent:
            rig.service = CanvasService(store_factory=lambda: rig.chat, food_factory=rig._foods,
                                        coach_factory=lambda: {}, agent=rig.old)
        rig.session_id = "00000000-0000-4000-8000-000000000001"
        result = await rig.say("what's my workout today", "00000000-0000-4000-8000-000000000002")
        return rig, result

    hooked, with_hook = await run(True)
    plain, without_hook = await run(False)
    assert hooked.old.calls == ["what's my workout today"]
    # Same visible result; the only difference is that food writes stay with the food agent.
    assert "log_meal" in plain.old.tools[0] and not FOOD_WRITE_TOOLS & set(hooked.old.tools[0])
    assert "log_meal" not in hooked.old.instructions[0]
    assert json.dumps(with_hook, sort_keys=True) == json.dumps(without_hook, sort_keys=True)
    assert hooked.chat.history == plain.chat.history
    assert with_hook["reply"] == "Today is Push: bench, rows and dips."
    assert "AgentMessage" in _components(with_hook)
    assert hooked.drafts.drafts == {} and hooked.drafts.entries == {}


async def test_handed_off_turn_shows_the_reconcile_notice_first(user):
    rig = Rig()
    rig.script.push(HALF_QP)
    await rig.say("half a quarter pounder")
    rig.drafts.fail_after_commit = 1  # saved, response lost
    lost = await rig.say("yes")
    assert "could not verify" in lost["reply"].casefold() and lost["receipt"] is None
    assert "FoodClarification" in _components(lost)
    rig.script.push({"type": "not_food"})
    later = await rig.say("what's my workout today")
    assert rig.old.calls == ["what's my workout today"]
    assert later["reply"].startswith("Logged McDonald's Quarter Pounder (half)")
    assert later["reply"].endswith("Today is Push: bench, rows and dips.")
    assert len(rig.drafts.entries) == 1 and _assistant_texts(rig.chat)[-1] == later["reply"]
    # Review S6: the verified save is shown as the existing receipt; the stale card is gone.
    assert later["receipt"] is not None and later["receipt"]["recordId"] in rig.drafts.entries
    receipt = _components(later)["MealReceipt"]
    assert receipt["text"].startswith("Logged McDonald's Quarter Pounder (half)")
    assert "could not verify" not in json.dumps(later).casefold()
    assert "FoodClarification" not in _components(later)


class GreedyOldModel:
    """Review probe C1: an old-path model that logs food whenever it has a tool for it."""

    def __init__(self):
        self.tools = []

    async def __call__(self, **kwargs):
        handlers = kwargs["handlers"]
        self.tools.append(sorted(set(handlers) | {t["name"] for t in kwargs["tool_catalog"]}))
        if "log_meal" in handlers:
            out = await handlers["log_meal"]({"description": "half quarter pounder", "meal_type": "Dinner"})
            return out["confirmation"], []
        if "update_meal" in handlers:
            out = await handlers["update_meal"]({"add": [{"description": "half quarter pounder"}]})
            return out.get("confirmation") or "ok", []
        return "I can't log food from here. Tell me what you ate and I'll show it first.", []


def _legacy_writers(rig, writes):
    def foods():
        async def log_meal(call_id, args):
            writes.append(("log_meal", dict(args)))
            return {"status": "committed", "operation_id": "op-legacy",
                    "logged": {"id": "legacy-row", "name": args["description"]},
                    "confirmation": f"Logged {args['description']}."}

        async def lookup_food(call_id, args):
            return {"status": "found"}
        return {"log_meal": log_meal, "lookup_food": lookup_food}

    class AutoCommitDrafts:
        asked = 0

        async def context(self):
            return {"open_meals": [], "logged_meals": []}

        async def handle(self, args, **_kw):
            writes.append(("update_meal", dict(args)))
            return {"status": "committed", "draft_ref": str(uuid4()), "logged": {"id": "legacy-draft-row",
                    "name": "half quarter pounder"}, "confirmation": "Logged half quarter pounder."}

    async def drafts_factory():
        AutoCommitDrafts.asked += 1
        return AutoCommitDrafts()
    rig.service.food_factory = foods
    return drafts_factory, AutoCommitDrafts


@pytest.mark.parametrize("meal_drafts_on", [False, True])
async def test_misread_food_handed_off_cannot_write_and_one_yes_gives_one_row(user, meal_drafts_on):
    """Review B2 / probe C1: with the food agent active, a handed-off turn has no food write tool."""
    rig = Rig()
    writes = []
    drafts_factory, engine = _legacy_writers(rig, writes)
    if meal_drafts_on:
        rig.service.drafts_factory = drafts_factory
    rig.service.agent = greedy = GreedyOldModel()
    rig.script.push(HALF_QP)
    first = await rig.say("half a mcdonald's quarter pounder")
    assert first["reply"].endswith("Log it?")
    rig.script.push({"type": "not_food"})  # the food model misreads an edit as not food
    handed = await rig.say("actually that was my dinner, log it as dinner")
    assert not FOOD_WRITE_TOOLS & set(greedy.tools[0])
    assert writes == [] and engine.asked == 0 and rig.drafts.entries == {}
    assert handed["receipt"] is None and "MealReceipt" not in _components(handed)
    # B1: the handed-off reply did not re-show the preview, so it is unbound.
    # The next "yes" goes to the model, whose confirm only re-shows it.
    (draft_id, row), = rig.drafts.drafts.items()
    rig.script.push({"type": "confirm", "draft_ref": draft_id, "shown_revision": row["revision"]})
    reshown = await rig.say("yes")
    assert writes == [] and rig.drafts.entries == {} and rig.drafts.commit_calls == []
    assert reshown["reply"].endswith("Log it?") and reshown["receipt"] is None
    assert "MealReceipt" not in _components(reshown)
    done = await rig.say("yes")
    assert writes == [] and len(rig.drafts.entries) == 1  # one meal, one row
    assert len(rig.drafts.commit_calls) == 1 and done["receipt"] is not None
    assert "MealReceipt" in _components(done)


async def test_without_the_food_agent_the_old_model_keeps_its_food_tools(user):
    """The B2 restriction applies only when the food agent is active for the user."""
    rig = Rig()
    writes = []
    _legacy_writers(rig, writes)
    rig.service.food_agent_factory = None
    rig.service.agent = greedy = GreedyOldModel()
    result = await rig.say("half a quarter pounder")
    assert "log_meal" in greedy.tools[0] and len(writes) == 1
    assert "MealReceipt" in _components(result)


# ---------------------------------------------------------------- replay


async def test_turn_replay_returns_the_stored_result_and_never_commits_twice(user):
    rig = Rig()
    rig.script.push(HALF_QP)
    await rig.say("half a quarter pounder")
    turn_id = str(uuid4())
    done = await rig.say("yes", turn_id)
    calls, history = len(rig.drafts.commit_calls), list(rig.chat.history)
    asked = rig.factory_calls
    again = await rig.service.turn(rig.session_id, turn_id, "yes", adapter="voice")
    assert again == done
    assert len(rig.drafts.commit_calls) == calls == 1 and len(rig.drafts.entries) == 1
    assert rig.chat.history == history and rig.factory_calls == asked
    with pytest.raises(ValueError, match="reused"):
        await rig.service.turn(rig.session_id, turn_id, "no", adapter="text")


async def test_cancel_closes_the_question_with_a_plain_message(user):
    rig = Rig()
    rig.script.push(HALF_QP)
    await rig.say("half a quarter pounder")
    result = await rig.say("no")
    assert result["reply"] == "OK, not logged."
    assert "FoodClarification" not in _components(result)
    assert _components(result)["AgentMessage"]["text"] == "OK, not logged."
    assert rig.drafts.entries == {}


async def test_an_agent_crash_is_reported_honestly_and_never_falls_back_to_the_old_path(user):
    rig = Rig()

    async def broken(message, *, session_ref=None, turn_ref=None):
        raise RuntimeError("bug")
    rig.agent.turn = broken
    result = await rig.say("half a quarter pounder")
    assert rig.old.calls == [] and rig.food_calls == []
    assert "logged" not in result["reply"].casefold() or "check" in result["reply"].casefold()
    assert result["receipt"] is None and "FoodClarification" in _components(result)
    assert _assistant_texts(rig.chat) == [result["reply"]]


async def test_food_agent_session_ref_and_turn_ref_come_from_the_canvas(user):
    rig = Rig()
    seen = []
    real = rig.agent.turn

    async def spy(message, *, session_ref=None, turn_ref=None):
        seen.append((message, session_ref, turn_ref))
        return await real(message, session_ref=session_ref, turn_ref=turn_ref)
    rig.agent.turn = spy
    rig.script.push(HALF_QP)
    turn_id = str(uuid4())
    await rig.say("half a quarter pounder", turn_id)
    assert seen == [("half a quarter pounder", f"canvas:{rig.session_id}", turn_id)]


# ---------------------------------------------------------------- old path never touches agent drafts


class DraftRowStore:
    """Just enough of ``Store`` for MealDrafts.context()/handle() on fixed rows. Records writes."""

    def __init__(self, rows):
        self.rows = {str(r["id"]): r for r in rows}
        self.writes = []

    async def fetch_meal_draft_context(self, *, open_since, committed_days, limit=4):
        return {"open": [r for r in self.rows.values() if r["state"] != "committed"],
                "committed": [r for r in self.rows.values() if r["state"] == "committed"]}

    async def fetch_meal_draft(self, draft_id):
        return self.rows.get(str(draft_id))

    def __getattr__(self, name):  # any write or other read is recorded and refused
        async def write(*args, **kwargs):
            self.writes.append(name)
            raise AssertionError(f"MealDrafts must not call {name} for a food-agent draft")
        return write


async def _agent_rows():
    """One open food-agent draft (bound preview) and one committed one, as the store returns them."""
    rig = Rig()
    rig.script.push(HALF_QP)
    await rig.say("half a quarter pounder")
    (row,) = rig.drafts.drafts.values()
    open_row = {**row, "items": [{**item, "position": n} for n, item in enumerate(row["items"])]}
    committed = {**open_row, "id": str(uuid4()), "state": "committed",
                 "committed_entry_id": str(uuid4()), "committed_entry_revision": 1}
    return open_row, committed


def _legacy_row():
    from datetime import date
    return {"id": str(uuid4()), "intent": "consume", "state": "needs_detail", "log_date": date(2026, 11, 2),
            "meal_type": None, "meal_type_origin": None, "usual_label": None, "session_ref": "canvas:x",
            "pending_question": "Raw or cooked?", "parent_draft_id": None, "amends_entry_id": None,
            "amends_entry_revision": None, "revision": 2,
            "items": [{"id": str(uuid4()), "position": 0, "kind": "food", "status": "included",
                       "description": "ground beef",
                       "quantity": 3, "unit": "oz", "preparation": None, "size": None,
                       "field_origins": {"asked": "preparation"}, "needs": "preparation", "question": None,
                       "resolution": None, "estimate_consent": None, "template_item_id": None}]}


def _meal_drafts(store):
    from meal_drafts import MealDrafts

    async def resolve(_component):
        raise AssertionError("no lookup for a food-agent draft")
    return MealDrafts(store=store, resolve=resolve, validate_values=lambda **k: k)


async def test_old_meal_context_never_shows_food_agent_drafts(user):
    open_row, committed = await _agent_rows()
    legacy = _legacy_row()
    context = await _meal_drafts(DraftRowStore([open_row, committed, legacy])).context()
    refs = [m["draft_ref"] for m in context["open_meals"] + context["logged_meals"]]
    assert refs == [legacy["id"]]


@pytest.mark.parametrize("key", ["draft_ref", "amend_ref", "repeat_ref"])
async def test_old_update_meal_refuses_food_agent_drafts_without_writing(user, key):
    open_row, committed = await _agent_rows()
    for row in (open_row, committed):
        store = DraftRowStore([row])
        result = await _meal_drafts(store).handle({key: row["id"], "edit": [
            {"item_ref": "item1", "quantity": 1}]}, turn_ref=str(uuid4()), settle=True)
        assert store.writes == []
        assert result["status"] == "conflict" and "draft_ref" not in result
        assert "logged" not in result["confirmation"].casefold().replace("not logged", "")


async def test_old_settle_leaves_food_agent_drafts_alone(user):
    open_row, _ = await _agent_rows()
    store = DraftRowStore([open_row])
    assert await _meal_drafts(store).settle(open_row["id"], turn_ref=str(uuid4())) is None
    assert store.writes == []


# ---------------------------------------------------------------- real PostgreSQL

ADMIN_URL = os.environ.get("MMMACROS_TEST_PG_ADMIN_URL", "")
pg = pytest.mark.skipif(not ADMIN_URL, reason="set MMMACROS_TEST_PG_ADMIN_URL to a throwaway local PostgreSQL")


def _db_url(name: str) -> str:
    base, _, query = ADMIN_URL.partition("?")
    root = base.rsplit("/", 1)[0]
    return f"{root}/{name}" + (f"?{query}" if query else "")


@pytest.fixture
async def env(monkeypatch):
    import asyncpg
    for flag in ("CALORIE_ONLY_WRITES_ENABLED", "CALORIE_ONLY_WRITES_USER_IDS"):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    monkeypatch.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import migrations
    from store import Store
    name = "mmm_food_canvas_" + uuid4().hex[:12]
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    url = _db_url(name)
    await migrations.apply_migrations(url)  # 000-002; 003 and 004 are held
    conn = await asyncpg.connect(url)
    for held in ("003_calorie_only_unknown_macros.sql", "004_meal_drafts.sql"):
        await conn.execute((migrations.MIGRATIONS_DIR / held).read_text())
    users = [uuid4(), uuid4()]
    for synthetic in users:
        await conn.execute("INSERT INTO users(id, display_name) VALUES($1, 'Synthetic test user')", synthetic)
    await conn.close()

    async def connect(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(url, min_size=1, max_size=6)
        return self.pool
    monkeypatch.setattr(Store, "connect", connect)
    store = Store(url)
    yield {"store": store, "users": users, "url": url}
    await store.aclose()
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()


async def _rows(url, sql, *args):
    import asyncpg
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def _pg_rig(env, resolver=None):
    from datetime import datetime, timezone

    from food_agent.drafts import StoreDrafts
    rig = Rig(drafts=StoreDrafts(env["store"]), resolver=resolver, chat=env["store"])
    rig.agent.clock = lambda: datetime.now(timezone.utc)  # the store stamps updated_at with now()
    return rig


async def _assistant_rows(url, user_id):
    rows = await _rows(url, "SELECT role, content FROM chat_messages WHERE user_id=$1 ORDER BY created_at, id",
                       user_id)
    return [(r["role"], r["content"]) for r in rows]


@pg
async def test_pg_half_quarter_pounder_with_cheese_saves_one_row(env):
    rig = _pg_rig(env)
    user_id = env["users"][0]
    scope = bind_user(user_id)
    try:
        rig.script.push(HALF_QP)
        first = await rig.say("Half of a McDonald's quarter pounder")
        assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []  # zero rows on a first message
        assert await _rows(env["url"], "SELECT 1 FROM request_operations") == []
        rig.script.push(WITH_CHEESE)
        second = await rig.say("quarter pounder with cheese")
        assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []
        turn_id = str(uuid4())
        done = await rig.say("yes", turn_id)
        replay = await rig.service.turn(rig.session_id, turn_id, "yes", adapter="voice")
    finally:
        reset_user(scope)
    assert replay == done
    (row,) = await _rows(env["url"], "SELECT * FROM nutrition_entries")
    assert row["user_id"] == user_id
    assert float(row["calories"]) == 260.0 and float(row["protein"]) == 15.0
    meta = json.loads(row["component_metadata"])
    assert meta["items"][0]["multiplier"] == 0.5 and meta["items"][0]["portion"]["amount"] == 0.5
    assert len(await _rows(env["url"], "SELECT 1 FROM request_operations "
                                       "WHERE idempotency_key LIKE 'meal-draft:%'")) == 1
    assert done["receipt"]["recordId"] == str(row["id"])
    assert "260 kcal" in _components(done)["MealReceipt"]["text"] and "15 g protein" in done["reply"]
    chat = await _assistant_rows(env["url"], user_id)
    assert chat == [("user", "Half of a McDonald's quarter pounder"), ("assistant", first["reply"]),
                    ("user", "quarter pounder with cheese"), ("assistant", second["reply"]),
                    ("user", "yes"), ("assistant", done["reply"])]


@pg
async def test_pg_choco_laka_six_oz_saves_half_the_panel_once(env):
    rig = _pg_rig(env, resolver=Resolver(default=CHOC12))
    scope = bind_user(env["users"][0])
    try:
        rig.script.push(ONE_CHOCO)
        first = await rig.say("One Choco Laka smoothie from Smoothie King")
        assert "matched to smoothie king choc-a-laka" in first["reply"].casefold()
        assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []
        rig.script.push(SIX_OZ)
        second = await rig.say("yes but 6 oz instead of 12")
        assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []
        assert len(rig.resolver.queries) == 2 and "176 kcal" in second["reply"]  # refresh restricted panel
        done = await rig.say("yes")
    finally:
        reset_user(scope)
    (row,) = await _rows(env["url"], "SELECT * FROM nutrition_entries")
    assert float(row["calories"]) == 176.0 and float(row["protein"]) == 6.5
    assert json.loads(row["component_metadata"])["items"][0]["food_id"] == "FatSecret:cal12"
    assert done["receipt"]["recordId"] == str(row["id"])


@pg
async def test_pg_non_food_is_handed_off_and_writes_no_draft(env):
    rig = _pg_rig(env)
    scope = bind_user(env["users"][0])
    try:
        rig.script.push({"type": "not_food"})
        result = await rig.say("what's my workout today")
    finally:
        reset_user(scope)
    assert rig.old.calls == ["what's my workout today"]
    assert result["reply"] == "Today is Push: bench, rows and dips."
    assert await _rows(env["url"], "SELECT 1 FROM meal_drafts") == []
    assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []
