"""Everyday schedule regressions: synthetic stores, no provider/network."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest

import domain
from auth import bind_user, reset_user
from test_coach import rotation_plan
from test_today_workout_plan import ServerDayStore
import server as srv


@pytest.mark.asyncio
async def test_fresh_account_has_no_invented_schedule(monkeypatch):
    store = ServerDayStore(plan=None)
    monkeypatch.setattr(srv, "_client", store)
    scope = bind_user(uuid4())
    try:
        payload = await srv.workout_plan_payload()
        outlook = await srv._coach_tool_handlers(canvas=True)["get_workout_outlook"]({})
    finally:
        reset_user(scope)
    assert payload["upcoming"] == []
    assert payload["rotation"] == []
    assert payload["has_plan"] is False
    assert outlook["upcoming"] == []
    assert outlook["today"]["type"] is None


@pytest.mark.asyncio
async def test_daily_only_plan_unlocks_logger_without_inventing_future(monkeypatch):
    store = ServerDayStore(plan=None)
    monkeypatch.setattr(srv, "_client", store)
    scope = bind_user(uuid4())
    try:
        await store.compare_and_swap_day_workout_plan(
            domain.effective_date(), None, "Pull",
            [{"name": "Lat Pulldown", "sets": 3, "reps": "8-12"}], "fixture")
        payload = await srv.workout_plan_payload()
    finally:
        reset_user(scope)
    assert payload["has_plan"] is True
    assert payload["rotation"] == []
    assert payload["upcoming"] == [{"type": "Pull", "exercises": [{"name": "Lat Pulldown"}],
                                     "today_plan": True, "done": False}]


@pytest.mark.asyncio
async def test_custom_routine_and_completed_day_state_drive_outlook(monkeypatch):
    plan = {"rotation": ["Full Body", "Rest"], "days": {
        "Full Body": {"exercises": [{"name": "Squat", "sets": 3, "reps": "8"}]},
        "Rest": {"exercises": []}}}
    store = ServerDayStore(plan=deepcopy(plan))
    store.session_state = {"rotation_index": 0, "done_date": domain.effective_date() - timedelta(days=1)}
    monkeypatch.setattr(srv, "_client", store)
    scope = bind_user(uuid4())
    try:
        payload = await srv.workout_plan_payload()
        outlook = await srv._coach_tool_handlers(canvas=True)["get_workout_outlook"]({})
    finally:
        reset_user(scope)
    assert payload["rotation"] == ["Full Body", "Rest"]
    assert [day["type"] for day in payload["upcoming"]] == ["Rest", "Full Body", "Rest", "Full Body", "Rest"]
    assert outlook["today"]["type"] == "Rest"
    assert outlook["upcoming"][0] == {"type": "Full Body", "exercises": ["Squat"]}
    assert store.plan == plan


@pytest.mark.asyncio
async def test_today_override_does_not_change_future_routine_slot(monkeypatch):
    store = ServerDayStore(plan=rotation_plan())
    monkeypatch.setattr(srv, "_client", store)
    scope = bind_user(uuid4())
    try:
        await store.compare_and_swap_day_workout_plan(domain.effective_date(), None, "Legs",
            [{"name": "Squat", "sets": 3, "reps": "8"}], "fixture")
        payload = await srv.workout_plan_payload()
    finally:
        reset_user(scope)
    assert [day["type"] for day in payload["upcoming"]][:3] == ["Legs", "Pull", "Legs"]
    assert store.plan == rotation_plan()
