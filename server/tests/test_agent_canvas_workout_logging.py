"""Phase A2: voice/text log, undo and complete today's workout through the shared agent turn.

Completed sets save immediately (like meals) and are verified by readback before the
canvas credits them. Plan changes keep their native Confirm; nothing here stages one.
All stores are in-memory tenant doubles; no database or provider is used.
"""
import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agent_canvas import CANVAS_INSTRUCTIONS, CanvasService
from auth import bind_user, reset_user
from domain import effective_date
from test_agent_canvas import MemoryStore


class WorkoutStore(MemoryStore):
    """Newest-first workout rows with a tenant-bound delete, like store.Store."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self.deleted = []
        self.readback_hidden = set()

    async def fetch_workouts(self, start=None, end=None, exercise=None):
        return [deepcopy(r) for r in self.rows if r["id"] not in self.readback_hidden]

    async def delete(self, table, row_id):
        assert table == "fitness_tracker"
        self.deleted.append(row_id)
        self.rows = [r for r in self.rows if r["id"] != row_id]


def coach_for(store, *, today_type="Push", exercises=None, done=False):
    state = {"done": done, "writes": []}
    planned = exercises if exercises is not None else [
        {"name": "Bench Press", "sets": 3, "reps": "8-10", "rest_sec": 90},
        {"name": "Incline Dumbbell Press", "sets": 3, "reps": "10", "rest_sec": 60},
    ]

    async def get_today_session(_args):
        return {"today_type": today_type, "exercises": planned, "done": state["done"], "has_plan": True}

    async def log_workout(args):
        state["writes"].append(deepcopy(args))
        row = {"id": f"row-{len(state['writes'])}", "exercise": args["exercise"],
               "workout_type": [args["workout_type"]], "muscle_group": [],
               "sets": [dict(s) for s in args["sets"]],
               # The server's logging day (4 am rollover), not the UTC calendar date.
               "date": effective_date().isoformat(),
               "created_time": datetime.now(timezone.utc).isoformat() + f"+{len(state['writes']):03d}"}
        store.rows.insert(0, row)
        return deepcopy(row)

    async def complete_today_session(_args):
        state["done"] = True
        return {"done": True}

    coach = {"get_today_session": get_today_session, "log_workout": log_workout,
             "complete_today_session": complete_today_session}
    return coach, state


def service_with(store, coach, agent):
    return CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                         coach_factory=lambda: coach, agent=agent)


def kinds(result):
    return [c["component"] for s in result["surfaces"] for c in s["components"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["voice", "text"])
async def test_log_workout_saves_immediately_and_refreshes_the_card(adapter):
    store = WorkoutStore()
    coach, state = coach_for(store)
    seen = {}

    async def agent(**kwargs):
        seen["catalog"] = {t["name"]: t for t in kwargs["tool_catalog"]}
        first = await kwargs["handlers"]["log_workout"]({
            "exercise": "Bench Press", "sets": [{"weight": 185, "reps": 8}, {"weight": 185, "reps": 8}]})
        # Provider retry inside the same turn: identical entry is replayed, not re-written.
        again = await kwargs["handlers"]["log_workout"]({
            "exercise": "Bench Press", "sets": [{"weight": 185, "reps": 8}, {"weight": 185, "reps": 8}]})
        assert again == first
        seen["result"] = first
        return "Logged two sets of bench.", []

    service = service_with(store, coach, agent)
    sid = str(uuid4())
    scope = bind_user(uuid4())
    try:
        if adapter == "voice":
            result = await service.voice_handler(sid)("live-call-1", {"message": "I did two sets of bench at 185 for 8"})
        else:
            result = await service.turn(sid, str(uuid4()), "I did two sets of bench at 185 for 8", adapter="text")
    finally:
        reset_user(scope)
    assert len(state["writes"]) == 1, "One verified write per distinct entry"
    assert state["writes"][0]["workout_type"] == "Push", "Defaults to today's session type"
    assert seen["result"]["status"] == "committed" and seen["result"]["sets_saved"] == 2
    assert seen["result"]["sets_verified_this_session"] == 2
    bench = next(ex for ex in result["workout"]["exercises"] if ex["name"] == "Bench Press")
    assert result["workout"]["loggedSets"][bench["id"]] == 2, "The card credits readback, not the model's claim"
    assert result["workout"]["restEndsAt"] is not None
    assert "WorkoutOverview" in kinds(result) and "SetLogger" in kinds(result)
    assert result["approval"] is None, "Logging sets never stages an approval"
    assert "AgentMessage" in kinds(result) and "Logged Bench Press: 2 sets" in json.dumps(result)
    schema = seen["catalog"]["log_workout"]["input_schema"]
    assert schema["required"] == ["exercise", "sets"] and schema["properties"]["sets"]["maxItems"] == 4
    assert not {"anyOf", "oneOf", "allOf", "enum", "not"} & set(schema)
    assert "log_workout" in CANVAS_INSTRUCTIONS and "never log planned sets as done" in CANVAS_INSTRUCTIONS


@pytest.mark.asyncio
async def test_log_workout_requires_readback_and_rejects_bad_shapes():
    store = WorkoutStore()
    coach, state = coach_for(store)
    outcomes = {}

    async def agent(**kwargs):
        handler = kwargs["handlers"]["log_workout"]
        for bad in ({"exercise": "Bench Press"}, {"exercise": "Bench Press", "sets": []},
                    {"exercise": "Bench Press", "sets": [{"weight": -5, "reps": 8}]},
                    {"exercise": "Bench Press", "sets": [{"weight": 100, "reps": 8, "rpe": 9}]},
                    {"exercise": "Bench Press", "sets": [{"weight": 100, "reps": 8}] * 5},
                    {"exercise": "Bench Press", "sets": [{"weight": 1, "reps": 1}], "plan": {}}):
            with pytest.raises(ValueError):
                await handler(bad)
        assert state["writes"] == []
        # A write whose row cannot be read back is reported as unverified, never credited.
        store.readback_hidden.add("row-1")
        with pytest.raises(ValueError, match="readback"):
            await handler({"exercise": "Bench Press", "sets": [{"weight": 100, "reps": 8}]})
        outcomes["writes_after_hidden"] = len(state["writes"])
        return "Could not verify.", []

    service = service_with(store, coach, agent)
    scope = bind_user(uuid4())
    try:
        result = await service.turn(str(uuid4()), str(uuid4()), "log bench", adapter="voice")
    finally:
        reset_user(scope)
    assert outcomes["writes_after_hidden"] == 1
    assert result["workout"] is None or result["workout"]["loggedSets"] == {}


@pytest.mark.asyncio
async def test_undo_last_set_removes_only_the_newest_entry_and_uncredits():
    store = WorkoutStore()
    coach, state = coach_for(store)

    async def agent(**kwargs):
        if kwargs["message"].startswith("log"):
            await kwargs["handlers"]["log_workout"]({"exercise": "Bench Press", "sets": [{"weight": 185, "reps": 8}]})
            await kwargs["handlers"]["log_workout"]({"exercise": "Incline Dumbbell Press", "sets": [{"weight": 60, "reps": 10}, {"weight": 60, "reps": 10}]})
            return "Logged.", []
        result = await kwargs["handlers"]["undo_last_set"]({})
        assert result["status"] == "removed" and result["exercise"] == "Incline Dumbbell Press"
        with pytest.raises(ValueError):
            await kwargs["handlers"]["undo_last_set"]({"id": "row-1"})
        return "Removed.", []

    service = service_with(store, coach, agent)
    sid = str(uuid4())
    scope = bind_user(uuid4())
    try:
        logged = await service.turn(sid, str(uuid4()), "log both", adapter="voice")
        bench = next(ex for ex in logged["workout"]["exercises"] if ex["name"] == "Bench Press")
        incline = next(ex for ex in logged["workout"]["exercises"] if ex["name"] == "Incline Dumbbell Press")
        assert logged["workout"]["loggedSets"] == {bench["id"]: 1, incline["id"]: 2}
        undone = await service.turn(sid, str(uuid4()), "undo my last set", adapter="voice")
    finally:
        reset_user(scope)
    assert store.deleted == ["row-2"]
    assert [r["exercise"] for r in store.rows] == ["Bench Press"], "Only the newest entry is removed"
    assert undone["workout"]["loggedSets"] == {bench["id"]: 1}
    assert "Removed Incline Dumbbell Press (2 sets)" in json.dumps(undone)
    assert undone["approval"] is None


@pytest.mark.asyncio
async def test_undo_with_nothing_logged_asks_instead_of_deleting():
    store = WorkoutStore()
    coach, _state = coach_for(store)

    async def agent(**kwargs):
        result = await kwargs["handlers"]["undo_last_set"]({})
        assert result["status"] == "needs_clarification"
        return result["question"], []

    service = service_with(store, coach, agent)
    scope = bind_user(uuid4())
    try:
        result = await service.turn(str(uuid4()), str(uuid4()), "undo that", adapter="text")
    finally:
        reset_user(scope)
    assert store.deleted == []
    assert "nothing logged today" in result["reply"].casefold()


@pytest.mark.asyncio
async def test_complete_today_session_through_the_turn_requires_readback():
    store = WorkoutStore()
    coach, state = coach_for(store)

    async def agent(**kwargs):
        result = await kwargs["handlers"]["complete_today_session"]({})
        assert result["status"] == "completed" and result["type"] == "Push"
        with pytest.raises(ValueError):
            await kwargs["handlers"]["complete_today_session"]({"force": True})
        return "Marked done.", []

    service = service_with(store, coach, agent)
    sid = str(uuid4())
    scope = bind_user(uuid4())
    try:
        result = await service.turn(sid, str(uuid4()), "I'm done with my workout", adapter="voice")
        # A spoken "yes"/repeat never re-writes: completion is idempotent on the server read.
        async def again(**kwargs):
            assert (await kwargs["handlers"]["complete_today_session"]({}))["status"] == "completed"
            return "Already done.", []
        service.agent = again
        repeat = await service.turn(sid, str(uuid4()), "yes finish it", adapter="voice")
    finally:
        reset_user(scope)
    assert state["done"] is True
    assert result["workout"]["done"] is True and repeat["workout"]["done"] is True
    assert all(s["surfaceId"] != "task" for s in result["surfaces"])
    assert "Workout complete" in json.dumps(result)


@pytest.mark.asyncio
async def test_workout_write_tools_follow_the_coach_capability():
    store = WorkoutStore()
    coach, _state = coach_for(store)
    read_only = {"get_today_session": coach["get_today_session"]}
    seen = {}

    async def agent(**kwargs):
        seen[kwargs["message"]] = ([t["name"] for t in kwargs["tool_catalog"]], set(kwargs["handlers"]))
        return "ok", []

    scope = bind_user(uuid4())
    try:
        await service_with(store, coach, agent).turn(str(uuid4()), str(uuid4()), "full", adapter="voice")
        await service_with(store, read_only, agent).turn(str(uuid4()), str(uuid4()), "read-only", adapter="voice")
    finally:
        reset_user(scope)
    names, handlers = seen["full"]
    assert {"log_workout", "undo_last_set", "complete_today_session"} <= set(names)
    assert {"log_workout", "undo_last_set", "complete_today_session"} <= handlers
    assert "confirm" not in names, "Approval stays a native tap"
    names, handlers = seen["read-only"]
    assert not {"log_workout", "undo_last_set", "complete_today_session"} & set(names)
    assert not {"log_workout", "undo_last_set", "complete_today_session"} & handlers


@pytest.mark.asyncio
async def test_live_adapter_schema_still_exposes_only_agent_turn():
    """GPT Live reaches the new tools only through agent_turn; the legacy voice route is unchanged."""
    from agent_canvas import canvas_live_start
    from live_coach import build_session_start
    event = canvas_live_start({})
    assert [t["name"] for t in event["session"]["delegation"]["responses"]["tools"]] == ["agent_turn"]
    legacy = build_session_start({})
    assert [t["name"] for t in legacy["session"]["delegation"]["responses"]["tools"]] == [
        "get_today", "lookup_food", "log_meal", "undo_last_meal"]
