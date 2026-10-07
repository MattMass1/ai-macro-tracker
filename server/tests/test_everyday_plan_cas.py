"""Preview context CAS regressions, with synthetic tenant-partitioned stores."""
from copy import deepcopy
from uuid import uuid4

import pytest

from auth import bind_user, reset_user
from test_today_workout_plan import CanvasDayStore, PULL, _service
import domain


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["routine", "completion", "sets"])
async def test_intervening_context_change_refuses_old_preview(change):
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        await service._propose_today_workout(session, deepcopy(PULL))
        if change == "routine":
            store.plan["days"]["Push"]["exercises"][0]["sets"] = 5
        elif change == "completion":
            store.state = {"rotation_index": 1, "done_date": domain.effective_date()}
        else:
            # Even adding sets to an exercise retained by the proposal changes
            # what was previewed; the new completed state must be shown first.
            store.workouts.append({"id": "new", "exercise": "Lat Pulldown",
                "date": domain.effective_date().isoformat(), "sets": [{"weight": 80, "reps": 8}]})
        with pytest.raises(ValueError, match="changed|fresh preview"):
            await service.action(session.canvas.session_id, {"action": "confirm", "reference": session.approval["id"]})
        assert store.day_plan_writes == []
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_store_rechecks_logging_day_after_waiting_for_tenant_lock(monkeypatch):
    import store as store_module
    from datetime import timedelta
    from workout_store_fixture import WorkoutTransactionPool
    day = domain.effective_date()
    events = []

    class Connection(WorkoutTransactionPool):
        async def execute(self, sql, *args):
            await super().execute(sql, *args)
            events.append("locked")
            monkeypatch.setattr(store_module, "effective_date", lambda: day + timedelta(days=1))

        async def fetchval(self, *args):
            raise AssertionError("expired preview must not snapshot or write")

        async def fetchrow(self, *args):
            raise AssertionError("expired preview must not write")

    scope = bind_user(uuid4())
    try:
        store = store_module.Store("postgresql://unused")
        assert await store.compare_and_swap_day_workout_plan(
            day, None, "Pull", [], "old-day", expected_context={}, connection=Connection()) is None
    finally:
        reset_user(scope)
    assert events == ["locked"]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_routine", [True, False])
async def test_empty_today_plan_can_preview_cancel_confirm_and_refresh(monkeypatch, has_routine):
    from test_today_workout_plan import ServerDayStore
    import server as srv
    store = CanvasDayStore()
    if not has_routine:
        store.plan = None
    reader = ServerDayStore(plan=store.plan)
    reader.day_plans = store.day_plans
    monkeypatch.setattr(srv, "_client", reader)
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        args = {"workout_type": "Pull", "exercises": []}
        result = await service._propose_today_workout(session, args)
        assert result["status"] == "awaiting_confirmation" and store.day_plan_writes == []
        assert "0 exercises" in result["detail"]
        preview = await service.snapshot(session.canvas.session_id)
        assert preview["surfaces"][0]["components"][0]["rows"] == []
        await service.action(session.canvas.session_id, {"action": "cancel", "reference": session.approval["id"]})
        assert store.day_plan_writes == []
        await service._propose_today_workout(session, args)
        await service.action(session.canvas.session_id, {"action": "confirm", "reference": session.approval["id"]})
        stored = await store.fetch_day_workout_plan(domain.effective_date())
        assert stored["exercises"] == []
        response = await service.action(session.canvas.session_id, {"action": "refresh"})
        assert response["workout"]["exercises"] == []
        assert response["workout"]["activeExerciseId"] is None
        assert store.workouts == [] and len(store.day_plan_writes) == 1
        assert response["workout"]["loggedSets"] == {}
        payload = await srv.workout_plan_payload()
        assert payload["has_plan"] is True
        assert payload["upcoming"][0]["exercises"] == []
        assert payload["upcoming"][0]["today_plan"] is True
        assert store.plan_writes == []
    finally:
        reset_user(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_rotation", [True, False])
async def test_legacy_list_day_label_can_stage_today_preview_without_invented_schedule(monkeypatch, explicit_rotation):
    from test_today_workout_plan import ServerDayStore
    import server as srv
    routine: dict = {"days": [{"type": "Upper A", "exercises": []}]}
    if explicit_rotation:
        routine["rotation"] = ["Upper A"]
    store = CanvasDayStore(plan=routine)
    server_store = ServerDayStore(plan=routine)
    monkeypatch.setattr(srv, "_client", server_store)
    service = _service(store, session=srv._coach_tool_handlers()["get_today_session"])
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        result = await service._propose_today_workout(session, {**deepcopy(PULL), "workout_type": "Upper A"})
        assert result["status"] == "awaiting_confirmation"
        assert session.approval["after"]["type"] == "Upper A"
        assert store.day_plan_writes == [] and store.plan == routine
        outlook = await srv._coach_tool_handlers(canvas=True)["get_workout_outlook"]({})
        assert outlook["today"]["type"] == "Upper A"
        assert all(day["type"] == "Upper A" for day in outlook["upcoming"])
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_today_tool_schema_accepts_empty_list():
    from jsonschema import validate
    from agent_canvas import CanvasService
    seen = {}
    store = CanvasDayStore()

    async def agent(**kwargs):
        seen["catalog"] = kwargs["tool_catalog"]
        return "ok", []

    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=_service(store).coach_factory, agent=agent)
    scope = bind_user(uuid4())
    try:
        await service.turn(str(uuid4()), str(uuid4()), "Make today's plan empty", adapter="voice")
    finally:
        reset_user(scope)
    tool = next(tool for tool in seen["catalog"] if tool["name"] == "propose_today_workout")
    validate({"workout_type": "Pull", "exercises": []}, tool["input_schema"])
