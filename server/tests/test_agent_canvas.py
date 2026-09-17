"""Network-free Agent Canvas contract and operation tests."""
import json
import os
from uuid import uuid4

import pytest


def test_closed_surface_contract_accepts_native_catalog_and_rejects_unknowns():
    from agent_canvas import validate_surface

    surface = {
        "surfaceId": "task", "lifecycle": "task",
        "components": [{"id": "macros", "component": "MacroProgress"}],
    }
    assert validate_surface(surface) == surface
    for component in ("WebView", "HTML", "Swift", "Unknown"):
        bad = {**surface, "components": [{"id": "unsafe", "component": component}]}
        with pytest.raises(ValueError, match="component"):
            validate_surface(bad)
    bad = {**surface, "components": [{"id": "macros", "component": "MacroProgress",
                                      "actions": [{"action": "raw_post"}]}]}
    with pytest.raises(ValueError, match="action"):
        validate_surface(bad)
    with pytest.raises(ValueError):
        validate_surface({**surface, "url": "https://untrusted.invalid"})
    with pytest.raises(ValueError):
        validate_surface({**surface, "lifecycle": "pinned"})


def test_lifecycle_replaces_task_preserves_approval_and_expires_only_receipts():
    from agent_canvas import CanvasState

    state = CanvasState(str(uuid4()))
    state.present("task", "task", [{"id": "macros", "component": "MacroProgress"}])
    state.present("approval", "approval", [{"id": "confirm", "component": "ConfirmationCard"}])
    state.present("task", "task", [{"id": "workout", "component": "WorkoutOverview"}])
    state.present("receipt", "transient", [{"id": "saved", "component": "MealReceipt"}], now=10)
    assert len(state.snapshot(now=11)["surfaces"]) == 3
    assert len(state.snapshot(now=20)["surfaces"]) == 3
    assert state.snapshot(now=20)["serverTime"] == 20
    assert len(state.snapshot(now=40)["surfaces"]) == 2
    assert state.surfaces["task"]["components"][0]["component"] == "WorkoutOverview"
    with pytest.raises(ValueError, match="approval"):
        state.dismiss("approval")
    state.quiet()
    assert list(state.surfaces) == ["approval"]
    state.dismiss("approval", cancel_approval=True)
    assert state.snapshot()["surfaces"] == []


@pytest.mark.asyncio
async def test_canvas_can_supply_closed_tools_without_changing_model_route(monkeypatch):
    import httpx
    from coach import run_agent
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "fixture-only")
    seen = []

    async def post(_token, payload):
        seen.append(payload)
        return httpx.Response(200, request=httpx.Request("POST", "https://fixture.invalid"),
                              json={"choices": [{"message": {"content": "Ready"}}]})

    tools = [{"name": "present_surface", "description": "Native presentation only",
              "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}}]
    await run_agent(history=[], message="show macros", onboarding=False,
                    handlers={"present_surface": lambda args: None}, post=post,
                    tool_catalog=tools, instruction_override="Closed native canvas contract")
    assert [t["function"]["name"] for t in seen[0]["tools"]] == ["present_surface"]
    assert seen[0]["messages"][0]["content"] == "Closed native canvas contract"
    assert seen[0]["model"] == os.environ.get("COACH_MODEL", "gpt-5.6-luna")


class MemoryStore:
    def __init__(self):
        self.history = []

    async def fetch_chat_messages_since(self, *_):
        return list(self.history)

    async def insert_user_chat_message(self, text, *, daily_cap):
        self.history.append({"role": "user", "content": text})

    async def insert_chat_message(self, role, text, *_):
        self.history.append({"role": role, "content": text})

    async def insert_coach_usage(self, *_):
        pass


@pytest.mark.parametrize("bad", [None, [], {}, 3])
def test_malformed_action_and_component_types_are_validation_errors(bad):
    from agent_canvas import validate_action, validate_surface
    with pytest.raises(ValueError):
        validate_action({"action": bad})
    with pytest.raises(ValueError):
        validate_surface({"surfaceId": "task", "lifecycle": "task", "components": [{"id": "item", "component": bad}]})


@pytest.mark.asyncio
async def test_typed_and_voice_share_one_operation_and_replay_never_writes_twice():
    from agent_canvas import CanvasService
    from auth import bind_user, reset_user
    store = MemoryStore()
    calls = []

    async def controlled(call_id, args):
        calls.append((call_id, args))
        return {"status": "committed", "operation_id": "op-fixture",
                "logged": {"id": "meal-fixture", "name": "fixture meal"},
                "confirmation": "Saved verified meal."}

    async def agent(**kwargs):
        await kwargs["handlers"]["log_meal"]({"description": kwargs["message"], "meal_type": "Lunch"})
        return "Saved verified meal.", []

    service = CanvasService(store_factory=lambda: store,
                            food_factory=lambda: {"log_meal": controlled},
                            coach_factory=lambda: {}, agent=agent)
    session_id, turn_id = str(uuid4()), str(uuid4())
    scope = bind_user(uuid4())
    try:
        first = await service.turn(session_id, turn_id, "fixture meal", adapter="text")
        replay = await service.turn(session_id, turn_id, "fixture meal", adapter="voice")
        assert first == replay
        assert len(calls) == 1
        assert "MealReceipt" in json.dumps(first)
        assert len(store.history) == 2
        with pytest.raises(ValueError, match="reused"):
            await service.turn(session_id, turn_id, "different meal", adapter="text")
        with pytest.raises(ValueError):
            await service.action(session_id, {"action": "raw_post"})
    finally:
        reset_user(scope)
    with pytest.raises(Exception):
        await service.turn(session_id, str(uuid4()), "no auth", adapter="text")


@pytest.mark.asyncio
async def test_workout_actions_bind_real_exercise_ids_and_only_ack_readback():
    from agent_canvas import CanvasService
    from auth import bind_user, reset_user
    store = MemoryStore()
    rows = []

    async def workouts(*_):
        return rows
    store.fetch_workouts = workouts

    async def today(_):
        return {"today_type": "Push", "done": False, "has_plan": True,
                "exercises": [{"name": "Fixture press", "sets": 3, "reps": "8-10", "rest_sec": 90},
                              {"name": "Fixture fly", "sets": 2, "reps": "12", "rest_sec": 60}]}

    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": today})
    session_id = str(uuid4())
    scope = bind_user(uuid4())
    try:
        await service.snapshot(session_id, create=True)
        result = await service.action(session_id, {"action": "start_workout"})
        workout = result["workout"]
        first, second = workout["exercises"]
        assert workout["activeExerciseId"] == first["id"]
        with pytest.raises(ValueError):
            await service.action(session_id, {"action": "open_set_logger", "reference": "forged"})
        with pytest.raises(ValueError, match="readback"):
            await service.action(session_id, {"action": "exercise_logged", "reference": "unknown-row"})
        rows.append({"id": "verified-row", "exercise": first["name"], "workout_type": ["Push"],
                     "sets": [{"weight": 10, "reps": 8}], "day": result["workout"]["date"]})
        saved = await service.action(session_id, {"action": "exercise_logged", "reference": "verified-row"})
        assert saved["workout"]["loggedSets"][first["id"]] == 1
        assert saved["workout"]["restEndsAt"] > 0
        saved = await service.action(session_id, {"action": "exercise_logged", "reference": "verified-row"})
        assert saved["workout"]["loggedSets"][first["id"]] == 1
        next_ = await service.action(session_id, {"action": "next_exercise"})
        assert next_["workout"]["activeExerciseId"] == second["id"]
        quiet = await service.action(session_id, {"action": "quiet"})
        assert quiet["surfaces"] == []
        assert quiet["workout"]["activeExerciseId"] == second["id"]
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_presentation_tool_and_taps_share_gateway_without_unverified_data():
    from agent_canvas import CanvasService
    from auth import bind_user, reset_user
    store = MemoryStore()
    reads = []
    async def today(_):
        reads.append("today")
        return {"remaining": {"protein": 37}}
    async def agent(**kwargs):
        assert "set_workout_plan" not in kwargs["handlers"]
        assert "log_workout" not in kwargs["handlers"]  # generated UI cannot supply sets
        await kwargs["handlers"]["present_surface"]({"view": "macros"})
        return "Your remaining protein is shown.", []
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today": today}, agent=agent)
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        response = await service.turn(sid, str(uuid4()), "protein left?", adapter="voice")
        assert reads == ["today"]
        assert [x["component"] for x in response["surfaces"][0]["components"]] == ["MacroProgress"]
        assert "message" not in [x["surfaceId"] for x in response["surfaces"]]
        response = await service.action(sid, {"action": "show_progress"})
        assert response["surfaces"][0]["components"][0]["component"] == "WeeklyTrend"
        with pytest.raises(ValueError):
            await service.action(sid, {"action": "show_macros", "calories": 500})
        await service.action(sid, {"action": "quiet"})
        assert (await service.snapshot(sid))["surfaces"] == []
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_receipt_expiry_starts_after_slow_agent_acknowledgement(monkeypatch):
    import agent_canvas
    from auth import bind_user, reset_user
    clock = [100.0]
    monkeypatch.setattr(agent_canvas.time, "time", lambda: clock[0])
    async def food(*_):
        return {"status": "committed", "operation_id": "op-fixture", "logged": {"id": "meal-fixture", "name": "Fixture"}, "confirmation": "Verified"}
    async def agent(**kwargs):
        await kwargs['handlers']['log_meal']({})
        clock[0] += 10  # Provider is slow after the verified write.
        return 'Verified', []
    service = agent_canvas.CanvasService(store_factory=MemoryStore,
        food_factory=lambda: {'log_meal': food}, coach_factory=lambda: {}, agent=agent)
    scope = bind_user(uuid4())
    try:
        result = await service.turn(str(uuid4()), str(uuid4()), 'Log fixture', adapter='text')
        assert result['surfaces'][0]['expiresAt'] == 140
        assert result['serverTime'] == 110
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_completing_workout_requires_readback_and_removes_task():
    from agent_canvas import CanvasService
    from auth import bind_user, reset_user
    done = False
    writes = []
    async def today(_):
        return {"has_plan": True, "today_type": "Push", "done": done, "exercises": []}
    async def complete(_):
        nonlocal done
        writes.append(1)
        done = True
        return {"done": True}
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": today, "complete_today_session": complete})
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        await service.snapshot(sid, create=True)
        await service.action(sid, {"action": "start_workout"})
        result = await service.action(sid, {"action": "complete_workout"})
        assert result["workout"]["done"] is True
        assert all(s["surfaceId"] != "task" for s in result["surfaces"])
        assert result["surfaces"][0]["lifecycle"] == "transient"
        await service.action(sid, {"action": "complete_workout"})
        assert len(writes) == 1
        with pytest.raises(ValueError, match="complete"):
            await service.action(sid, {"action": "next_exercise"})
    finally:
        reset_user(scope)
