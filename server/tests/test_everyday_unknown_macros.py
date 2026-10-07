"""Unknown nutrients are nullable facts, not measured zeros. Synthetic stores only."""
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from auth import bind_user, reset_user
from test_live_coach import FakeVoiceStore, _import_server
from store import Store, meal


async def serialize_calorie_only_day(monkeypatch):
    srv = _import_server(monkeypatch)
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)
    monkeypatch.setattr(srv.domain, "effective_date", lambda: date(2026, 10, 7))
    scope = bind_user(uuid4())
    try:
        await srv._voice_tool_handlers()["log_meal"]("fixture-only", {"description": "200 calories"})
        return await srv.day_payload(date(2026, 10, 7))
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_canonical_calorie_only_native_fixture(monkeypatch):
    import json
    from pathlib import Path
    fixture = Path(__file__).resolve().parents[2] / "docs/agent-canvas/fixtures/calorie-only-day.json"
    assert await serialize_calorie_only_day(monkeypatch) == json.loads(fixture.read_text())


@pytest.mark.asyncio
async def test_calories_only_replay_readback_and_tenant_isolation(monkeypatch):
    srv = _import_server(monkeypatch)
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)
    args = {"description": "Add a 200-calorie snack", "meal_type": "Snack"}
    users = [uuid4(), uuid4()]
    for user in users:
        scope = bind_user(user)
        try:
            first = await srv._voice_tool_handlers()["log_meal"]("same-id", args)
            replay = await srv._voice_tool_handlers()["log_meal"]("same-id", args)
            payload = await srv.day_payload(srv.domain.effective_date())
        finally:
            reset_user(scope)
        assert first["status"] == "committed"
        assert replay["status"] == "replayed"
        assert first["logged"]["id"] == replay["logged"]["id"]
        assert payload["totals"]["calories"] == 200
        assert payload["macros_complete"] is False
        assert first["day_total"]["macros_complete"] is False
        assert len(payload["meals"]) == 1
        assert all(payload["meals"][0][key] is None for key in ("protein", "carbs", "fat", "fiber"))
    assert fake.insert_count == 2
    assert fake.meals[users[0]][0]["id"] != fake.meals[users[1]][0]["id"]


@pytest.mark.asyncio
async def test_calories_only_uncertain_readback_reconciles_without_new_write(monkeypatch):
    srv = _import_server(monkeypatch)
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)
    original = fake.fetch_meals
    calls = 0

    async def fail_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("synthetic read loss")
        return await original(*args)

    fake.fetch_meals = fail_once
    scope = bind_user(uuid4())
    try:
        args = {"description": "200 calories"}
        first = await srv._voice_tool_handlers()["log_meal"]("lost", args)
        second = await srv._voice_tool_handlers()["log_meal"]("reconnected", args)
    finally:
        reset_user(scope)
    assert first["status"] == "unknown"
    assert second["status"] == "replayed"
    assert "unknown" in second["confirmation"]
    assert fake.insert_count == 1


def test_stored_meal_preserves_nulls():
    row = {"id": "synthetic", "name": "Calorie-only snack", "meal": "Snack",
           "calories": Decimal("200"), "protein": None, "carbs": None, "fat": None,
           "fiber": None, "day": date(2026, 10, 7), "created_at": datetime(2026, 10, 7, tzinfo=timezone.utc)}
    result = meal(row)
    assert result["protein"] is None and result["calories"] == 200


@pytest.mark.asyncio
async def test_unknown_nutrients_never_become_shared_catalog_evidence():
    class NoQuery:
        def __getattr__(self, name):
            raise AssertionError("must not write unknown nutrients to shared catalog")
    assert await Store._catalog_snapshot(NoQuery(), name="Calorie-only snack",
        macros={"calories": 200, "protein": None, "carbs": None, "fat": None, "fiber": None},
        macro_source="User supplied calories; other nutrients unknown") == (None, None)


@pytest.mark.asyncio
async def test_receipt_rejects_wrong_stored_nutrients(monkeypatch):
    srv = _import_server(monkeypatch)
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)
    original = fake.fetch_meals

    async def corrupt(*args):
        return [{**row, "protein": 0} for row in await original(*args)]
    fake.fetch_meals = corrupt
    scope = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("bad-readback", {"description": "200 calories"})
    finally:
        reset_user(scope)
    assert result["status"] == "unknown"
    assert "Logged" not in result["confirmation"]


@pytest.mark.asyncio
async def test_day_rollover_uses_effective_day_for_calories_only(monkeypatch):
    srv = _import_server(monkeypatch)
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)
    monkeypatch.setattr(srv.domain, "effective_date", lambda: date(2026, 10, 6))
    scope = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("before-four", {"description": "200 calories"})
    finally:
        reset_user(scope)
    assert result["logged"]["date"] == "2026-10-06"


@pytest.mark.asyncio
async def test_live_context_keeps_unknown_nutrients_unknown(monkeypatch):
    from live_coach import build_live_context
    srv = _import_server(monkeypatch)
    class ContextStore(FakeVoiceStore):
        async def fetch_workout_plan(self): return None
        async def fetch_workouts(self): return []
        async def fetch_prs(self): return []
        async def fetch_workout_library(self): return []
    fake = ContextStore()
    monkeypatch.setattr(srv, "_client", fake)
    scope = bind_user(uuid4())
    try:
        await srv._voice_tool_handlers()["log_meal"]("context", {"description": "200 calories"})
        context = await build_live_context(fake, today=srv.domain.effective_date())
    finally:
        reset_user(scope)
    assert context["today"]["nutrition"]["calories"] == 200
    assert all(context["today"]["nutrition"][key] is None for key in ("protein", "carbs", "fat", "fiber"))
