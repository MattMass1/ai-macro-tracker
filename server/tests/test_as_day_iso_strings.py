"""Production-shape regressions for `_as_day` and the workout read paths.

The real store (`store._dict`/`_value`) serialises every date to an ISO
string, so `fetch_session_day_state()` returns `done_date` as text. Older
fakes stored real `date` objects, which hid a str-vs-date TypeError that
500'd `/api/plan`, `/api/workout-stats` and the coach's today-workout load.
All data is synthetic and in-memory; no database or provider is used.
"""
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest

import domain
from auth import bind_user, reset_user
from test_coach import rotation_plan
from test_today_workout_plan import ServerDayStore
import server as srv


@pytest.mark.parametrize("value, expected", [
    ("2026-10-07", date(2026, 10, 7)),
    ("2026-10-07T23:15:00+00:00", date(2026, 10, 7)),
    (datetime(2026, 10, 7, 23, 15), date(2026, 10, 7)),
    (date(2026, 10, 7), date(2026, 10, 7)),
    (None, None),
    ("", None),
    ("not-a-date", None),
    ("2026-13-45", None),
])
def test_as_day_coerces_production_shapes(value, expected):
    assert srv._as_day(value) == expected


def _store_with_state(done_date_iso):
    store = ServerDayStore(plan=rotation_plan())
    # Production shape: store._dict() returns ISO strings, not date objects.
    store.session_state = {"rotation_index": 1, "done_date": done_date_iso,
                           "updated_at": datetime.now().isoformat()}
    return store


async def _with_user(monkeypatch, store, coro_fn):
    monkeypatch.setattr(srv, "_client", store)
    scope = bind_user(uuid4())
    try:
        return await coro_fn()
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_resolve_today_session_iso_string_done_today_marks_done(monkeypatch):
    store = _store_with_state(domain.effective_date().isoformat())
    index, today_type, _exercises, done = await _with_user(
        monkeypatch, store, srv._resolve_today_session)
    assert done is True
    assert index == 1
    assert today_type == "Pull"


@pytest.mark.asyncio
async def test_resolve_today_session_iso_string_done_yesterday_advances(monkeypatch):
    store = _store_with_state((domain.effective_date() - timedelta(days=1)).isoformat())
    index, today_type, _exercises, done = await _with_user(
        monkeypatch, store, srv._resolve_today_session)
    assert done is False
    assert index == 2
    assert today_type == "Legs"


@pytest.mark.asyncio
async def test_workout_plan_payload_with_iso_string_done_date(monkeypatch):
    store = _store_with_state(domain.effective_date().isoformat())
    payload = await _with_user(monkeypatch, store, srv.workout_plan_payload)
    assert payload["has_plan"] is True
    assert payload["upcoming"][0]["type"] == "Pull"
    assert payload["upcoming"][0]["done"] is True
    assert [day["type"] for day in payload["upcoming"]] == ["Pull", "Legs", "Push", "Pull", "Legs"]


@pytest.mark.asyncio
async def test_workout_stats_payload_with_iso_string_done_date(monkeypatch):
    store = _store_with_state((domain.effective_date() - timedelta(days=1)).isoformat())
    payload = await _with_user(monkeypatch, store, srv.workout_stats_payload)
    assert payload["plan"]["today"]["type"] == "Legs"
    assert payload["plan"]["today"]["done"] is False
    assert payload["today"]["date"] == domain.effective_date().isoformat()
