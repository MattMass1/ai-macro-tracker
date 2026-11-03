"""WP6: the food agent release gate, the server factory, and gate-off identity (AGENT.md section 5 item 10).

Offline. Synthetic user UUIDs only. No network: the old-path model is a scripted
``agent=`` and the food handlers are stubs.
"""

from __future__ import annotations

import itertools
import json
import sys
import types
from uuid import UUID, uuid4

import pytest

from auth import bind_user, reset_user
from test_agent_canvas import MemoryStore

FLAGS = ("FOOD_AGENT_ENABLED", "FOOD_AGENT_USER_IDS")


@pytest.fixture
def clean_env(monkeypatch):
    for flag in FLAGS:
        monkeypatch.delenv(flag, raising=False)
    return monkeypatch


class As:
    def __init__(self, user):
        self.user = user

    def __enter__(self):
        self.token = bind_user(self.user)
        return self

    def __exit__(self, *exc):
        reset_user(self.token)


# ---------------------------------------------------------------- the gate


def test_flag_names_are_exact():
    import release_gates
    assert release_gates.FOOD_AGENT_FLAG == "FOOD_AGENT_ENABLED"
    assert release_gates.FOOD_AGENT_USERS_FLAG == "FOOD_AGENT_USER_IDS"


@pytest.mark.parametrize("value", [None, "", "   ", "yes", "1", "on", "enabled", "truee", "t", "false", "tru e"])
def test_anything_but_true_keeps_the_gate_closed(clean_env, value):
    import release_gates
    if value is not None:
        clean_env.setenv("FOOD_AGENT_ENABLED", value)
    with As(uuid4()):
        assert release_gates.food_agent_allowed() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "True ", "  tRuE\n"])
def test_exact_true_any_case_after_strip_opens_for_everyone_without_a_list(clean_env, value):
    import release_gates
    clean_env.setenv("FOOD_AGENT_ENABLED", value)
    with As(uuid4()):
        assert release_gates.food_agent_allowed() is True
    # No canary list: an unbound caller is not asked for (same as meal drafts).
    assert release_gates.food_agent_allowed() is True


def test_canary_list_includes_and_excludes_the_bound_user(clean_env):
    import release_gates
    alice, bob, carol = uuid4(), uuid4(), uuid4()
    clean_env.setenv("FOOD_AGENT_ENABLED", "true")
    clean_env.setenv("FOOD_AGENT_USER_IDS", f" {alice} , {str(carol).upper()} ,")
    with As(alice):
        assert release_gates.food_agent_allowed() is True
    with As(carol):
        assert release_gates.food_agent_allowed() is True
    with As(bob):
        assert release_gates.food_agent_allowed() is False


@pytest.mark.parametrize("raw", ["not-a-uuid", "{user},nope", "{user};{other}", "12345"])
def test_malformed_canary_list_allows_nobody(clean_env, raw):
    import release_gates
    user, other = uuid4(), uuid4()
    clean_env.setenv("FOOD_AGENT_ENABLED", "true")
    clean_env.setenv("FOOD_AGENT_USER_IDS", raw.format(user=user, other=other))
    with As(user):
        assert release_gates.food_agent_allowed() is False


def test_unbound_user_with_a_list_is_closed(clean_env):
    import release_gates
    clean_env.setenv("FOOD_AGENT_ENABLED", "true")
    clean_env.setenv("FOOD_AGENT_USER_IDS", str(uuid4()))
    assert release_gates.food_agent_allowed() is False


def test_blank_list_means_every_user(clean_env):
    import release_gates
    clean_env.setenv("FOOD_AGENT_ENABLED", "true")
    clean_env.setenv("FOOD_AGENT_USER_IDS", "   ")
    with As(uuid4()):
        assert release_gates.food_agent_allowed() is True


def test_food_agent_gate_is_independent_of_the_existing_gates(clean_env):
    import release_gates
    clean_env.setenv("MEAL_DRAFTS_ENABLED", "true")
    clean_env.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
    with As(uuid4()):
        assert release_gates.food_agent_allowed() is False
        assert release_gates.meal_drafts_allowed() is True
    clean_env.delenv("MEAL_DRAFTS_ENABLED")
    clean_env.setenv("FOOD_AGENT_ENABLED", "true")
    with As(uuid4()):
        assert release_gates.food_agent_allowed() is True
        assert release_gates.meal_drafts_allowed() is False


# ---------------------------------------------------------------- the server factory


class Spy:
    """Counts constructions of anything it stands in for."""

    def __init__(self):
        self.calls = []

    def factory(self, name):
        spy = self

        class Fake:
            def __init__(self, *args, **kwargs):
                spy.calls.append((name, args, kwargs))
        return Fake


class ReadyStore:
    def __init__(self, ready=True):
        self.ready = ready
        self.checks = 0

    async def meal_drafts_ready(self):
        self.checks += 1
        return self.ready


@pytest.fixture
def srv(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    clean_env.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import server
    return server


@pytest.fixture
def spies(monkeypatch):
    """Replace every food agent class the factory builds; record constructions."""
    import food_agent.coordinator
    import food_agent.drafts
    import food_agent.find_food
    import food_agent.model_adapter
    spy = Spy()
    monkeypatch.setattr(food_agent.coordinator, "FoodAgent", spy.factory("FoodAgent"))
    monkeypatch.setattr(food_agent.drafts, "StoreDrafts", spy.factory("StoreDrafts"))
    monkeypatch.setattr(food_agent.find_food, "FoodFinder", spy.factory("FoodFinder"))
    monkeypatch.setattr(food_agent.model_adapter, "ModelInterpreter", spy.factory("ModelInterpreter"))
    recall = types.ModuleType("food_agent.recall")
    recall.HistoryRecaller = spy.factory("HistoryRecaller")
    monkeypatch.setitem(sys.modules, "food_agent.recall", recall)
    return spy


async def test_engine_is_none_and_builds_nothing_when_the_gate_is_unset(srv, spies, monkeypatch):
    store = ReadyStore()
    monkeypatch.setattr(srv, "_client", store)
    with As(uuid4()):
        assert await srv._food_agent_engine() is None
    assert spies.calls == [] and store.checks == 0  # not even the schema is asked


async def test_engine_is_none_when_the_user_is_not_on_the_canary_list(srv, spies, monkeypatch):
    store = ReadyStore()
    monkeypatch.setattr(srv, "_client", store)
    monkeypatch.setenv("FOOD_AGENT_ENABLED", "true")
    monkeypatch.setenv("FOOD_AGENT_USER_IDS", str(uuid4()))
    with As(uuid4()):
        assert await srv._food_agent_engine() is None
    assert spies.calls == [] and store.checks == 0


async def test_engine_is_none_when_the_004_schema_is_not_live(srv, spies, monkeypatch):
    store = ReadyStore(ready=False)
    monkeypatch.setattr(srv, "_client", store)
    monkeypatch.setenv("FOOD_AGENT_ENABLED", "true")
    with As(uuid4()):
        assert await srv._food_agent_engine() is None
    assert store.checks == 1 and spies.calls == []


async def test_engine_is_none_when_the_schema_check_fails(srv, spies, monkeypatch):
    class Broken:
        async def meal_drafts_ready(self):
            raise ConnectionError("database unavailable")
    monkeypatch.setattr(srv, "_client", Broken())
    monkeypatch.setenv("FOOD_AGENT_ENABLED", "true")
    with As(uuid4()):
        assert await srv._food_agent_engine() is None
    assert spies.calls == []


async def test_engine_is_none_when_the_recall_module_is_missing(srv, spies, monkeypatch):
    monkeypatch.setattr(srv, "_client", ReadyStore())
    monkeypatch.setenv("FOOD_AGENT_ENABLED", "true")
    monkeypatch.setitem(sys.modules, "food_agent.recall", None)  # import raises ImportError
    with As(uuid4()):
        assert await srv._food_agent_engine() is None
    assert not any(name == "FoodAgent" for name, _, _ in spies.calls)


async def test_engine_builds_the_agent_over_the_store_when_released(srv, spies, monkeypatch):
    store = ReadyStore()
    monkeypatch.setattr(srv, "_client", store)
    monkeypatch.setenv("FOOD_AGENT_ENABLED", "true")
    with As(uuid4()):
        agent = await srv._food_agent_engine()
    assert agent is not None
    names = [name for name, _, _ in spies.calls]
    assert names.count("FoodAgent") == 1
    assert {"StoreDrafts", "ModelInterpreter", "FoodFinder", "HistoryRecaller"} <= set(names)
    (_, drafts_args, _), = [c for c in spies.calls if c[0] == "StoreDrafts"]
    (_, recall_args, _), = [c for c in spies.calls if c[0] == "HistoryRecaller"]
    assert drafts_args[0] is store and recall_args[0] is store


def test_canvas_service_is_wired_to_the_food_agent_engine(srv, monkeypatch):
    monkeypatch.setattr(srv, "_canvas_service", None)
    service = srv.canvas_service()
    assert service.food_agent_factory is srv._food_agent_engine
    assert service.drafts_factory is srv._meal_drafts_engine


# ---------------------------------------------------------------- gate-off identity (item 10)


DIALOGUE = ["2 eggs and toast", "what's my workout today", "yes", "half a quarter pounder", "thanks"]


class OldPath:
    """The pre-existing canvas model, scripted: calls real handlers like the coach would."""

    def __init__(self):
        self.calls = []
        self.handler_calls = []

    async def __call__(self, **kwargs):
        message = kwargs["message"]
        self.calls.append({"message": message, "history": json.loads(json.dumps(kwargs["history"])),
                           "tools": sorted(t["name"] for t in kwargs["tool_catalog"]),
                           "instructions": kwargs["instruction_override"]})
        handlers = kwargs["handlers"]
        if "eggs" in message or "pounder" in message:
            result = await handlers["log_meal"]({"description": message, "meal_type": "Lunch"})
            return result["confirmation"], []
        if "workout" in message:
            today = await handlers["get_today"]({})
            return f"Today is {today['type']}.", []
        return "Got it.", []


def _food_handlers(log):
    async def log_meal(call_id, args):
        log.append(("log_meal", call_id, dict(args)))
        if "pounder" in args["description"]:
            return {"status": "needs_clarification", "question": "Which quarter pounder?",
                    "confirmation": "Which quarter pounder?"}
        return {"status": "committed", "operation_id": f"op-{len(log)}",
                "logged": {"id": f"row-{len(log)}", "name": args["description"]},
                "confirmation": f"Logged {args['description']}."}

    async def lookup_food(call_id, args):
        log.append(("lookup_food", call_id, dict(args)))
        return {"status": "found"}
    return {"log_meal": log_meal, "lookup_food": lookup_food}


def _coach(log):
    async def get_today(args):
        log.append(("get_today", dict(args)))
        return {"type": "Push"}
    return {"get_today": get_today}


async def _run_dialogue(monkeypatch, **service_kwargs):
    """Run DIALOGUE with deterministic ids and clock; return everything observable."""
    import time as time_module

    import agent_canvas
    from agent_canvas import CanvasService
    counter = itertools.count(1)
    monkeypatch.setattr(agent_canvas, "uuid4", lambda: UUID(int=next(counter)))
    monkeypatch.setattr(time_module, "time", lambda: 1_800_000_000.0)
    store, old, log = MemoryStore(), OldPath(), []
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: _food_handlers(log),
                            coach_factory=lambda: _coach(log), agent=old, **service_kwargs)
    session_id = "00000000-0000-4000-8000-000000000001"
    results = []
    with As(UUID("00000000-0000-4000-8000-0000000000aa")):
        for n, text in enumerate(DIALOGUE, start=1):
            turn_id = f"00000000-0000-4000-8000-{n:012d}"
            results.append(await service.turn(session_id, turn_id, text, adapter="text"))
        # A replayed turn id returns the stored result.
        results.append(await service.turn(session_id, "00000000-0000-4000-8000-000000000001",
                                          DIALOGUE[0], adapter="voice"))
        results.append(await service.snapshot(session_id))
    return {"results": results, "history": store.history, "model": old.calls, "handlers": log}


async def test_gate_off_dialogue_is_byte_identical_to_a_service_without_the_hook(srv, monkeypatch):
    baseline = await _run_dialogue(monkeypatch)

    asked = []
    real_engine = srv._food_agent_engine

    async def spy_engine():
        asked.append(1)
        return await real_engine()

    class NeverReady:
        async def meal_drafts_ready(self):
            raise AssertionError("the schema must not be checked with the gate off")
    monkeypatch.setattr(srv, "_client", NeverReady())
    import food_agent.coordinator

    class NoAgent:
        def __init__(self, *a, **k):
            raise AssertionError("FoodAgent must not be constructed with the gate off")
    monkeypatch.setattr(food_agent.coordinator, "FoodAgent", NoAgent)

    hooked = await _run_dialogue(monkeypatch, food_agent_factory=spy_engine)
    assert len(asked) == len(DIALOGUE)  # asked once per new turn, never on a replay
    dump = lambda value: json.dumps(value, sort_keys=True, default=str)  # noqa: E731
    assert dump(hooked["results"]) == dump(baseline["results"])
    assert dump(hooked["history"]) == dump(baseline["history"])
    assert dump(hooked["model"]) == dump(baseline["model"])
    assert dump(hooked["handlers"]) == dump(baseline["handlers"])
    # The dialogue really exercised the old path (writes, questions, a read tool).
    assert [c[0] for c in baseline["handlers"]].count("log_meal") == 2
    assert ("get_today", {}) in baseline["handlers"]
    assert "MealReceipt" in dump(baseline["results"][0])
    assert "FoodClarification" in dump(baseline["results"][3])


async def test_a_factory_returning_none_is_the_old_path(monkeypatch):
    baseline = await _run_dialogue(monkeypatch)

    def none_factory():
        return None
    hooked = await _run_dialogue(monkeypatch, food_agent_factory=none_factory)
    dump = lambda value: json.dumps(value, sort_keys=True, default=str)  # noqa: E731
    assert dump(hooked) == dump(baseline)


async def test_a_failing_factory_falls_back_to_the_old_path(monkeypatch):
    baseline = await _run_dialogue(monkeypatch)

    async def broken_factory():
        raise RuntimeError("factory exploded")
    hooked = await _run_dialogue(monkeypatch, food_agent_factory=broken_factory)
    dump = lambda value: json.dumps(value, sort_keys=True, default=str)  # noqa: E731
    assert dump(hooked) == dump(baseline)
