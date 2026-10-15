"""Canvas wiring for durable meal drafts (no database: engine is a stub)."""
from __future__ import annotations

from uuid import uuid4

import pytest

from agent_canvas import CANVAS_INSTRUCTIONS, CanvasService
from auth import bind_user, reset_user
from test_agent_canvas import MemoryStore


class StubDrafts:
    def __init__(self):
        self.calls = []

    async def context(self):
        return {"open_meals": [{"draft_ref": "d1", "items": [{"item_ref": "item1", "text": "3 oz 93/7 ground beef",
                                                           "needs": "preparation"}]}], "logged_meals": []}

    async def handle(self, args, *, session_ref=None):
        self.calls.append((args, session_ref))
        return {"status": "committed", "draft_ref": "d1", "logged": {"id": "row-1", "name": "3 oz beef"},
                "confirmation": "Logged 3 oz beef as Lunch."}


def _service(agent, drafts):
    async def factory():
        return drafts
    return CanvasService(store_factory=lambda: MemoryStore(),
                         food_factory=lambda: {"log_meal": None, "lookup_food": None},
                         coach_factory=lambda: {}, agent=agent,
                         drafts_factory=factory if drafts is not None else None)


@pytest.mark.asyncio
async def test_drafts_replace_log_meal_and_the_one_question_rules():
    drafts = StubDrafts()
    seen = {}

    async def agent(**kwargs):
        seen.update(kwargs)
        result = await kwargs["handlers"]["update_meal"]({"draft_ref": "d1", "edit": [
            {"item_ref": "item1", "preparation": "raw"}]})
        return result["confirmation"], []

    scope = bind_user(uuid4())
    try:
        session_id = str(uuid4())
        output = await _service(agent, drafts).turn(session_id, str(uuid4()), "raw", adapter="text")
    finally:
        reset_user(scope)
    names = {tool["name"] for tool in seen["tool_catalog"]}
    assert "update_meal" in names and "log_meal" not in names
    assert "log_meal" not in seen["handlers"]
    instructions = seen["instruction_override"]
    assert "FOOD IS ONE QUESTION AT MOST" not in instructions
    assert "update_meal" in instructions and '"draft_ref":"d1"' in instructions
    assert drafts.calls[0][1] == f"canvas:{session_id}"
    assert "MealReceipt" in str(output) and output["receipt"]["recordId"] == "row-1"


@pytest.mark.asyncio
async def test_switch_off_keeps_the_legacy_food_path():
    seen = {}

    async def agent(**kwargs):
        seen.update(kwargs)
        return "ok", []

    scope = bind_user(uuid4())
    try:
        await _service(agent, None).turn(str(uuid4()), str(uuid4()), "hi", adapter="text")
    finally:
        reset_user(scope)
    assert seen["instruction_override"] == CANVAS_INSTRUCTIONS
    assert "update_meal" not in seen["handlers"]


def test_gate_fails_closed(monkeypatch):
    import release_gates
    user = uuid4()
    scope = bind_user(user)
    try:
        monkeypatch.delenv("MEAL_DRAFTS_ENABLED", raising=False)
        assert not release_gates.meal_drafts_allowed()
        monkeypatch.setenv("MEAL_DRAFTS_ENABLED", "yes")
        assert not release_gates.meal_drafts_allowed()
        monkeypatch.setenv("MEAL_DRAFTS_ENABLED", "true")
        assert release_gates.meal_drafts_allowed()
        monkeypatch.setenv("MEAL_DRAFTS_USER_IDS", "not-a-uuid")
        assert not release_gates.meal_drafts_allowed()
        monkeypatch.setenv("MEAL_DRAFTS_USER_IDS", str(uuid4()))
        assert not release_gates.meal_drafts_allowed()
        monkeypatch.setenv("MEAL_DRAFTS_USER_IDS", str(user))
        assert release_gates.meal_drafts_allowed()
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_engine_off_when_schema_missing(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    monkeypatch.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import server as srv
    monkeypatch.setenv("MEAL_DRAFTS_ENABLED", "true")

    class NoSchema:
        async def meal_drafts_ready(self):
            return False
    monkeypatch.setattr(srv, "_client", NoSchema())
    scope = bind_user(uuid4())
    try:
        assert await srv._meal_drafts_engine() is None
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_same_new_meal_twice_in_one_turn_writes_once():
    drafts = StubDrafts()

    async def agent(**kwargs):
        args = {"add": [{"description": "banana", "quantity": 1}]}
        first = await kwargs["handlers"]["update_meal"](args)
        second = await kwargs["handlers"]["update_meal"](dict(args))
        assert second == first
        return first["confirmation"], []

    scope = bind_user(uuid4())
    try:
        await _service(agent, drafts).turn(str(uuid4()), str(uuid4()), "a banana", adapter="text")
    finally:
        reset_user(scope)
    assert len(drafts.calls) == 1


def test_drafts_instructions_drop_legacy_food_rules_cleanly():
    from agent_canvas import _DRAFTS_BASE_INSTRUCTIONS
    assert "log_meal" not in _DRAFTS_BASE_INSTRUCTIONS
    assert "confirmation above still applies" not in _DRAFTS_BASE_INSTRUCTIONS
    from meal_drafts import MEAL_DRAFT_INSTRUCTIONS
    assert "do NOT send add again" in MEAL_DRAFT_INSTRUCTIONS
