"""Tomorrow-only schedule changes using synthetic tenant-scoped data."""
from copy import deepcopy
from datetime import date, timedelta
from uuid import uuid4
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://fixture.invalid/not-used")
os.environ.setdefault("APP_SHARED_TOKEN", "fixture-only")

import agent_canvas as canvas_module
import domain
import server as srv
from agent_canvas import CanvasService
from auth import bind_user, reset_user
from test_today_workout_plan import CanvasDayStore, ServerDayStore, _service


@pytest.mark.asyncio
async def test_tomorrow_preview_copies_saved_day_cancel_does_not_write():
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        result = await service._propose_tomorrow_workout(session, {"workout_type": "leg day"})
        draft = session.approval
        tomorrow = domain.effective_date() + timedelta(days=1)
        assert result["status"] == "awaiting_confirmation"
        assert draft["target_date"] == tomorrow.isoformat()
        assert tomorrow.isoformat() in draft["detail"]
        assert draft["after"] == {"type": "Legs", "exercises": store.plan["days"]["Legs"]["exercises"]}
        assert not store.day_plan_writes and not store.plan_writes
        await service.action(session.canvas.session_id, {"action": "cancel", "reference": draft["id"]})
        assert not store.day_plan_writes and session.approval is None
    finally:
        reset_user(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,rollover", [(1, False), (1, True), (3, False), (-1, False)])
async def test_store_checks_tomorrow_date_after_lock(monkeypatch, offset, rollover):
    import store as store_module
    from workout_store_fixture import WorkoutTransactionPool
    today = date(2026, 10, 9)
    monkeypatch.setattr(store_module, "effective_date", lambda: today)
    target = today + timedelta(days=offset)
    writes = []
    scope = bind_user(uuid4())
    from auth import current_user_id
    tenant = current_user_id()

    class Connection(WorkoutTransactionPool):
        async def execute(self, sql, *args):
            await super().execute(sql, *args)
            if rollover:
                monkeypatch.setattr(store_module, "effective_date", lambda: today + timedelta(days=1))

        async def fetchrow(self, sql, *args):
            assert args[:2] == (tenant, target)
            assert "ON CONFLICT (user_id, day) DO NOTHING" in sql
            writes.append(args)
            return {"day": target, "workout_type": "Pull", "exercises": [],
                    "revision": 1, "operation_id": "tomorrow"}

    try:
        store = store_module.Store("postgresql://unused")
        async def context(day, connection=None):
            assert day == target and offset == 1 and not rollover
            return {}
        store.fetch_workout_plan_context = context
        result = await store.compare_and_swap_day_workout_plan(
            target, None, "Pull", [], "tomorrow", expected_context={},
            expected_today=today, connection=Connection())
        assert bool(result) == (offset == 1 and not rollover)
        assert len(writes) == int(offset == 1 and not rollover)
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_tomorrow_missing_or_ambiguous_routine_asks_without_preview():
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        store.plan = None
        result = await service._propose_tomorrow_workout(session, {"workout_type": "Pull"})
        assert result["status"] == "needs_clarification" and session.approval is None
        store.plan = {"days": [{"type": "Upper A", "exercises": []}, {"type": "Upper B", "exercises": []}]}
        result = await service._propose_tomorrow_workout(session, {"workout_type": "Upper"})
        assert result["status"] == "needs_clarification" and session.approval is None
        result = await service._propose_tomorrow_workout(session, {"workout_type": "Upper A"})
        assert result["status"] == "awaiting_confirmation"
        assert session.approval["after"] == {"type": "Upper A", "exercises": []}
        assert not store.day_plan_writes
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_tomorrow_plan_serialization_matches_shared_swift_fixture(monkeypatch):
    import json
    from pathlib import Path
    store = ServerDayStore(plan=CanvasDayStore().plan)
    monkeypatch.setattr(srv, "_client", store)
    monkeypatch.setattr(domain, "effective_date", lambda: date(2026, 10, 9))
    scope = bind_user(uuid4())
    try:
        await store.compare_and_swap_day_workout_plan(
            date(2026, 10, 10), None, "Legs", [{"name": "Squat", "sets": 3, "reps": "8", "rest_sec": 120}], "fixture")
        payload = await srv.workout_plan_payload()
        fixture = Path(__file__).resolve().parents[2] / "docs/agent-canvas/fixtures/tomorrow-workout.json"
        assert json.loads(json.dumps(payload)) == json.loads(fixture.read_text())
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_tomorrow_confirm_changes_only_target_day_and_activates_next_day(monkeypatch):
    store = CanvasDayStore()
    routine = deepcopy(store.plan)
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        today = domain.effective_date()
        tomorrow = today + timedelta(days=1)
        session = service.session(str(uuid4()), create=True)
        await service._load_workout(session)
        current = deepcopy(session.workout)
        await service._propose_tomorrow_workout(session, {"workout_type": "Legs"})
        draft = session.approval
        saved = await service.action(session.canvas.session_id, {"action": "confirm", "reference": draft["id"]})
        assert saved["approval"] is None
        assert session.workout == current
        assert await store.fetch_day_workout_plan(today) is None
        assert (await store.fetch_day_workout_plan(tomorrow))["type"] == "Legs"
        assert len(store.day_plan_writes) == 1 and store.workouts == []
        assert store.plan == routine and store.plan_writes == []
        with pytest.raises(ValueError, match="no longer available"):
            await service.action(session.canvas.session_id, {"action": "confirm", "reference": draft["id"]})
        reader = ServerDayStore(plan=routine)
        reader.day_plans = store.day_plans
        monkeypatch.setattr(srv, "_client", reader)
        outlook = await srv._coach_tool_handlers(canvas=True)["get_workout_outlook"]({})
        assert outlook["today"]["type"] == "Push"
        assert outlook["tomorrow"]["date"] == tomorrow.isoformat()
        assert outlook["tomorrow"]["type"] == "Legs"
        monkeypatch.setattr(domain, "effective_date", lambda: tomorrow)
        index, type_, exercises, done = await srv._resolve_today_session()
        assert type_ == "Legs" and exercises == routine["days"]["Legs"]["exercises"]
        assert not done and index == 0
    finally:
        reset_user(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["routine", "tomorrow", "rollover"])
async def test_tomorrow_confirm_refuses_stale_preview(monkeypatch, change):
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        today = domain.effective_date()
        session = service.session(str(uuid4()), create=True)
        await service._propose_tomorrow_workout(session, {"workout_type": "Pull"})
        draft = session.approval
        if change == "routine":
            store.plan["days"]["Pull"]["exercises"][0]["sets"] = 4
        elif change == "tomorrow":
            await store.compare_and_swap_day_workout_plan(today + timedelta(days=1), None, "Legs", [], "another-device")
        else:
            monkeypatch.setattr(canvas_module, "effective_date", lambda: today + timedelta(days=1))
        writes = len(store.day_plan_writes)
        with pytest.raises(ValueError, match="changed|earlier day"):
            await service.action(session.canvas.session_id, {"action": "confirm", "reference": draft["id"]})
        assert len(store.day_plan_writes) == writes
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_tomorrow_lost_response_reconciles_without_second_write():
    store = CanvasDayStore()
    original = store.compare_and_swap_day_workout_plan
    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise ConnectionError("lost after commit")
    store.compare_and_swap_day_workout_plan = lost
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        await service._propose_tomorrow_workout(session, {"workout_type": "Pull"})
        with pytest.raises(ConnectionError):
            await service.action(session.canvas.session_id, {"action": "confirm", "reference": session.approval["id"]})
        await service.action(session.canvas.session_id, {"action": "refresh"})
        assert session.approval is None and len(store.day_plan_writes) == 1
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_tomorrow_plan_belongs_to_speaker_and_unknown_days_do_not_write():
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        result = await service._propose_tomorrow_workout(session, {"workout_type": "XYZ"})
        assert result["status"] == "needs_clarification" and session.approval is None
        with pytest.raises(ValueError):
            await service._propose_tomorrow_workout(session, {"workout_type": "Pull", "date": "2030-01-01"})
        await service._propose_tomorrow_workout(session, {"workout_type": "Pull"})
        await service.action(session.canvas.session_id, {"action": "confirm", "reference": session.approval["id"]})
        other = bind_user(uuid4())
        try:
            assert await store.fetch_day_workout_plan(domain.effective_date() + timedelta(days=1)) is None
        finally:
            reset_user(other)
    finally:
        reset_user(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["voice", "text"])
async def test_tomorrow_tool_is_reachable_from_both_coach_adapters(adapter):
    store = CanvasDayStore()
    async def agent(**kwargs):
        tool = next(t for t in kwargs["tool_catalog"] if t["name"] == "propose_tomorrow_workout")
        assert tool["input_schema"]["required"] == ["workout_type"]
        result = await kwargs["handlers"]["propose_tomorrow_workout"]({"workout_type": "Pull"})
        return result["detail"], []
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=_service(store).coach_factory, agent=agent)
    scope = bind_user(uuid4())
    try:
        result = await service.turn(str(uuid4()), str(uuid4()), "Change tomorrow to Pull day", adapter=adapter)
        assert result["approval"]["status"] == "pending"
        assert not store.day_plan_writes
    finally:
        reset_user(scope)
