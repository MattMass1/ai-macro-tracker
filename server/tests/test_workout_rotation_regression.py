"""Regressions for logged-workout precedence in the stored rotation."""
from copy import deepcopy
from datetime import date
from uuid import uuid4

import pytest

import domain
from auth import bind_user, reset_user
import server as srv
from test_coach import rotation_plan
from test_today_workout_plan import ServerDayStore


TODAY = date(2026, 10, 11)


def workout(day, workout_type, exercise="Fixture"):
    return {"id": f"{day}-{workout_type}", "exercise": exercise,
            "workout_type": [workout_type], "muscle_group": [],
            "sets": [{"weight": 100, "reps": 8}], "date": day}


async def resolve(monkeypatch, store, today=TODAY):
    monkeypatch.setattr(domain, "effective_date", lambda: today)
    monkeypatch.setattr(srv, "_client", store)
    scope = bind_user(uuid4())
    try:
        return await srv._resolve_today_session()
    finally:
        reset_user(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("today", [date(2026, 10, 11), date(2026, 10, 12)])
async def test_newer_logged_pull_overrides_stale_push_completion_without_calendar_advancement(monkeypatch, today):
    store = ServerDayStore(plan=rotation_plan())
    store.session_state = {"rotation_index": 0, "done_date": "2026-10-09"}
    store.workouts = [workout("2026-10-09", "Push"), workout("2026-10-10", "Pull")]
    assert (await resolve(monkeypatch, store, today))[1] == "Legs"


@pytest.mark.asyncio
async def test_no_state_follows_latest_logged_rotation_workout(monkeypatch):
    store = ServerDayStore(plan=rotation_plan())
    store.workouts = [workout("2026-10-09", "Push"), workout("2026-10-10", "Pull")]
    assert (await resolve(monkeypatch, store))[1] == "Legs"


@pytest.mark.asyncio
async def test_same_day_completion_pins_done_session_over_history(monkeypatch):
    store = ServerDayStore(plan=rotation_plan())
    store.session_state = {"rotation_index": 0, "done_date": "2026-10-11"}
    store.workouts = [workout("2026-10-10", "Pull")]
    index, type_, _, done = await resolve(monkeypatch, store)
    assert (index, type_, done) == (0, "Push", True)


@pytest.mark.asyncio
async def test_todays_in_progress_log_pins_today(monkeypatch):
    store = ServerDayStore(plan=rotation_plan())
    store.session_state = {"rotation_index": 0, "done_date": "2026-10-09"}
    store.workouts = [workout("2026-10-10", "Pull"), workout("2026-10-11", "Push")]
    assert (await resolve(monkeypatch, store))[1] == "Push"


@pytest.mark.asyncio
async def test_null_done_date_explicit_queue_beats_older_logged_history(monkeypatch):
    store = ServerDayStore(plan=rotation_plan())
    store.session_state = {"rotation_index": 0, "done_date": None}
    store.workouts = [workout("2026-10-10", "Pull")]
    assert (await resolve(monkeypatch, store))[1] == "Push"


@pytest.mark.asyncio
async def test_custom_rotation_matches_logged_type_case_insensitively(monkeypatch):
    plan = {"version": 1, "rotation": ["Upper A", "Lower B"], "days": {
        "Upper A": {"exercises": [{"name": "Bench", "sets": 3, "reps": "8"}]},
        "Lower B": {"exercises": [{"name": "Squat", "sets": 3, "reps": "8"}]}}}
    store = ServerDayStore(plan=plan)
    store.session_state = {"rotation_index": 1, "done_date": "2026-10-09"}
    store.workouts = [workout("2026-10-10", "upper a")]
    assert (await resolve(monkeypatch, store))[1] == "Lower B"


@pytest.mark.asyncio
async def test_non_rotation_addons_after_completion_do_not_advance_or_reset(monkeypatch):
    store = ServerDayStore(plan=rotation_plan())
    store.session_state = {"rotation_index": 0, "done_date": "2026-10-09"}
    store.workouts = [workout("2026-10-10", "Abs"), workout("2026-10-10", "Cardio")]
    assert (await resolve(monkeypatch, store))[1] == "Pull"


@pytest.mark.asyncio
async def test_date_scoped_plan_overrides_only_today(monkeypatch):
    store = ServerDayStore(plan=rotation_plan())
    store.session_state = {"rotation_index": 0, "done_date": "2026-10-09"}
    store.workouts = [workout("2026-10-10", "Pull")]
    monkeypatch.setattr(domain, "effective_date", lambda: TODAY)
    scope = bind_user(uuid4())
    try:
        await store.compare_and_swap_day_workout_plan(
            TODAY, None, "Cardio", [{"name": "Bike", "sets": 1, "reps": "20 min"}], "op")
        monkeypatch.setattr(srv, "_client", store)
        assert (await srv._resolve_today_session())[1] == "Cardio"
        assert (await srv.workout_plan_payload())["upcoming"][1]["type"] == "Push"
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_oct_10_with_only_push_logged_is_pull(monkeypatch):
    store = ServerDayStore(plan=rotation_plan())
    store.workouts = [workout("2026-10-09", "Push")]
    assert (await resolve(monkeypatch, store, date(2026, 10, 10)))[1] == "Pull"


@pytest.mark.asyncio
async def test_all_workout_read_payloads_share_repaired_resolver(monkeypatch):
    store = ServerDayStore(plan=rotation_plan())
    store.session_state = {"rotation_index": 0, "done_date": "2026-10-09"}
    store.workouts = [workout("2026-10-10", "Pull")]
    monkeypatch.setattr(domain, "effective_date", lambda: TODAY)
    monkeypatch.setattr(srv, "_client", store)
    scope = bind_user(uuid4())
    try:
        tool = await srv._coach_tool_handlers()["get_today_session"]({})
        plan = await srv.workout_plan_payload()
        stats = await srv.workout_stats_payload()
        assert tool["today_type"] == plan["upcoming"][0]["type"] == stats["plan"]["today"]["type"] == "Legs"
    finally:
        reset_user(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("day", ["2026-10-10", "2026-10-11"])
@pytest.mark.parametrize("muscle_group", [[], ["Unmapped fixture"]])
async def test_unclassified_legacy_rows_are_skipped_by_every_workout_read(
        monkeypatch, day, muscle_group):
    store = ServerDayStore(plan=rotation_plan())
    store.workouts = [{"id": f"legacy-{day}-{len(muscle_group)}", "exercise": "Legacy",
                       "workout_type": [], "muscle_group": muscle_group,
                       "sets": [{"weight": 100, "reps": 8}], "date": day}]
    monkeypatch.setattr(domain, "effective_date", lambda: TODAY)
    monkeypatch.setattr(srv, "_client", store)
    scope = bind_user(uuid4())
    try:
        resolved = await srv._resolve_today_session()
        plan = await srv.workout_plan_payload()
        stats = await srv.workout_stats_payload()
        tool = await srv._coach_tool_handlers()["get_today_session"]({})
    finally:
        reset_user(scope)
    assert resolved[1] == "Push"
    assert plan["upcoming"][0]["type"] == "Push"
    assert stats["plan"]["today"]["type"] == "Push"
    assert tool["today_type"] == "Push"
