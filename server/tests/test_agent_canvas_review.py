"""Independent-review regressions; all stores/providers are isolated fixtures."""
from uuid import UUID, uuid4

import pytest

from agent_canvas import CanvasService
from auth import bind_user, reset_user
from test_agent_canvas import MemoryStore


@pytest.mark.asyncio
async def test_day_readiness_shared_native_fixture_matches_real_serializer(monkeypatch):
    import json
    from pathlib import Path
    from datetime import date
    import server as srv
    import domain
    cases = json.loads((Path(__file__).resolve().parents[2] / "docs/agent-canvas/fixtures/day-readiness.json").read_text())
    monkeypatch.setattr(domain, "effective_date", lambda: date(2026, 9, 16))
    monkeypatch.setattr(domain, "day_label", lambda _day: "Today")
    for case in cases:
        class Store:
            async def fetch_targets(self, _day): return case["source_targets"]
            async def fetch_meals(self, _day): return []
            async def fetch_meal_rollups(self, _day): return []
            async def fetch_day_rollups(self, *_): return []
        monkeypatch.setattr(srv, "store_client", Store)
        assert await srv.day_payload(date(2026, 9, 16)) == case["payload"]


@pytest.mark.asyncio
async def test_day_capability_negotiation_is_additive_for_legacy_clients(monkeypatch):
    import server as srv
    import domain
    from agent_canvas import PROTOCOL
    class Store:
        async def fetch_targets(self, _day): return None
        async def fetch_meals(self, _day): return []
        async def fetch_meal_rollups(self, _day): return []
        async def fetch_day_rollups(self, *_): return []
    monkeypatch.setattr(srv, 'store_client', Store)
    payload = await srv.day_payload(domain.effective_date())
    assert payload['canvas_protocol'] == PROTOCOL
    # Legacy clients ignore additive keys: original required values still exist.
    legacy = {key: payload[key] for key in ('date', 'day_label', 'totals', 'targets', 'remaining', 'meals')}
    assert legacy['targets'] == domain.DEFAULT_TARGETS
    assert legacy['meals'] == []
    assert payload['has_targets'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("calories,ready", [(None, False), (0, False), (2000, True)])
async def test_established_chat_readiness_uses_current_positive_targets(calories, ready):
    from decimal import Decimal
    from typing import Any
    from store import Store
    from domain import effective_date
    tenant = uuid4()
    class Pool:
        async def fetchval(self, _sql, *_args): return calories is not None
        async def fetchrow(self, sql, user, day):
            assert user == tenant and day == effective_date()
            assert "effective_date <= $2" in sql and "ORDER BY effective_date DESC" in sql
            return None if calories is None else {"calories": Decimal(calories)}
    class TestStore(Store):
        async def connect(self) -> Any: return Pool()
    scope = bind_user(tenant)
    try:
        assert await TestStore("postgresql://fixture.invalid/not-used").has_macro_targets() is ready
    finally:
        reset_user(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("row,ready", [(None, False), ({"calories": 0}, False), ({"calories": 2000}, True)])
async def test_day_payload_exposes_real_target_readiness_not_default_calories(monkeypatch, row, ready):
    import server as srv
    import domain
    class Store:
        async def fetch_targets(self, _day):
            return None if row is None else {**domain.DEFAULT_TARGETS, **row}
        async def fetch_meals(self, _day): return []
        async def fetch_meal_rollups(self, _day): return []
        async def fetch_day_rollups(self, *_): return []
    monkeypatch.setattr(srv, "store_client", Store)
    payload = await srv.day_payload(domain.effective_date())
    assert payload["has_targets"] is ready
    if row is None:
        assert payload["targets"]["calories"] == domain.DEFAULT_TARGETS["calories"]


@pytest.mark.asyncio
@pytest.mark.parametrize("instant,expected", [
    ("2026-03-08T07:59:00+00:00", "2026-03-07"), ("2026-03-08T08:00:00+00:00", "2026-03-08"),
    ("2026-11-01T08:59:00+00:00", "2026-10-31"), ("2026-11-01T09:00:00+00:00", "2026-11-01")])
async def test_day_payload_carries_authoritative_policy_and_effective_date(monkeypatch, instant, expected):
    from datetime import datetime, date
    from zoneinfo import ZoneInfo
    import server as srv
    import domain
    monkeypatch.setattr(domain, "LOCAL_TZ", ZoneInfo("America/New_York"))
    monkeypatch.setattr(domain, "DAY_ROLLOVER_HOUR", 4)
    effective_date = domain.effective_date
    monkeypatch.setattr(domain, "effective_date", lambda: effective_date(datetime.fromisoformat(instant)))
    class Store:
        async def fetch_targets(self, _day): return None
        async def fetch_meals(self, _day): return []
        async def fetch_meal_rollups(self, _day): return []
        async def fetch_day_rollups(self, *_): return []
    monkeypatch.setattr(srv, "store_client", Store)
    historical = date(2026, 1, 1)
    payload = await srv.day_payload(historical)
    assert payload["date"] == "2026-01-01", "Policy must not change the requested historical date"
    assert payload["day_timing"] == {"time_zone": "America/New_York", "rollover_hour": 4, "effective_date": expected}
    monkeypatch.setattr(domain, "LOCAL_TZ", ZoneInfo("Asia/Tokyo"))
    monkeypatch.setattr(domain, "DAY_ROLLOVER_HOUR", 0)
    changed = await srv.day_payload(historical)
    assert changed["day_timing"]["time_zone"] == "Asia/Tokyo"
    assert changed["day_timing"]["rollover_hour"] == 0
    assert changed["day_timing"]["effective_date"] == effective_date(datetime.fromisoformat(instant)).isoformat()


@pytest.mark.asyncio
async def test_recreated_session_has_new_incarnation_and_never_replays_approval():
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {}, coach_factory=lambda: {})
    scope = bind_user(uuid4())
    try:
        sid = str(uuid4())
        before = await service.snapshot(sid, create=True)
        await service.action(sid, {"action": "show_progress"})
        service.sessions.clear()  # process restart, not a production store
        after = await service.snapshot(sid, create=True)
        assert after["revision"] == 0
        assert after["instanceId"] != before["instanceId"]
        assert after["approval"] is None
        with pytest.raises(ValueError, match="Approval is no longer"):
            await service.action(sid, {"action": "confirm", "reference": "lost-approval"})
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_custom_labels_round_trip_from_supported_plan_and_reject_bad_labels():
    import json
    from pathlib import Path
    from domain import validate_workout_plan
    fixture = json.loads((Path(__file__).resolve().parents[2] / "docs/agent-canvas/fixtures/workout-labels.json").read_text())
    scope = bind_user(uuid4())
    try:
        for label in fixture["accepted"] + fixture["rejected"] + [None, 3, []]:
            async def today(_):
                return {"has_plan": True, "today_type": label, "done": False, "exercises": []}
            service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {},
                                    coach_factory=lambda: {"get_today_session": today})
            sid = str(uuid4())
            await service.snapshot(sid, create=True)
            if label in fixture["accepted"]:
                plan = validate_workout_plan({"version": 1, "rotation": [label],
                    "days": {label: {"label": label, "exercises": []}}})
                assert plan["rotation"] == [label]
                result = await service.action(sid, {"action": "start_workout"})
                assert json.loads(json.dumps(result))["workout"]["type"] == label
            else:
                with pytest.raises(ValueError, match="workout label"):
                    await service.action(sid, {"action": "start_workout"})
    finally:
        reset_user(scope)



@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["text", "voice"])
async def test_canvas_records_real_coach_loop_usage_once_per_provider_call(adapter):
    import httpx
    from coach import run_agent
    store = MemoryStore()
    usage = []
    async def record(*values):
        usage.append(values)
    store.insert_coach_usage = record
    async def post(_token, payload):
        return httpx.Response(200, request=httpx.Request("POST", "https://fixture.invalid"), json={
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            "choices": [{"message": {"content": "Ready"}}]})
    async def agent(**kwargs):
        return await run_agent(**kwargs, post=post)
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {}, coach_factory=lambda: {}, agent=agent)
    scope = bind_user(uuid4())
    try:
        sid, tid = str(uuid4()), str(uuid4())
        await service.turn(sid, tid, "hello", adapter=adapter)
        assert len(usage) == 1
        assert usage[0][1:] == (12, 7)
        assert isinstance(usage[0][0], str)
        await service.turn(sid, tid, "hello", adapter=adapter)
        assert len(usage) == 1, "A cached turn must not bill the same provider call twice"
    finally:
        reset_user(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_type,row_type,counts", [
    ("Push", "Push", True), ("Push", "PushPull", False), ("Push", None, False),
    ("Push", ["Push"], True), ("Push", ["PushPull"], False),
    ("Upper", "Upper", True), ("Upper", "Push", False), ("Lower", ["Lower"], True)])
async def test_workout_scalar_or_legacy_type_only_counts_exact_membership(plan_type, row_type, counts):
    from domain import effective_date
    store = MemoryStore()
    async def rows(day):
        assert day == effective_date()
        return [{"id": "saved-row", "exercise": "Fixture press", "workout_type": row_type,
                 "sets": [{"weight": 10, "reps": 8}]}]
    store.fetch_workouts = rows
    async def today(_):
        return {"has_plan": True, "today_type": plan_type, "done": False,
                "exercises": [{"name": "Fixture press", "sets": 3, "reps": "8", "rest_sec": 90}]}
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": today})
    scope = bind_user(uuid4())
    try:
        sid = str(uuid4())
        await service.snapshot(sid, create=True)
        await service.action(sid, {"action": "start_workout"})
        result = await service.action(sid, {"action": "exercise_logged", "reference": "saved-row"})
        workout = result["workout"]
        exercise_id = workout["exercises"][0]["id"]
        assert workout["loggedSets"].get(exercise_id, 0) == int(counts)
        assert (workout["restEndsAt"] is not None) == counts
        assert "saved-row" in service.session(sid).acknowledged_rows
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_assigned_custom_label_survives_real_workout_writer_and_receives_credit(monkeypatch):
    import server as srv
    import domain
    from store import Store as RealStore
    from decimal import Decimal
    from datetime import datetime, timezone
    from auth import current_user_id
    saved = []
    class Pool:
        async def fetchrow(self, sql, row_id, user_id, exercise, types, muscles, *values):
            assert 'INSERT INTO fitness_tracker' in sql
            assert user_id == current_user_id()
            row = {'id': row_id, 'user_id': user_id, 'exercise_name': exercise,
                   'workout_type': list(types), 'muscle_group': list(muscles),
                   'day': values[-1], 'created_at': datetime.now(timezone.utc)}
            for i in range(4):
                weight, reps = values[2*i:2*i+2]
                row[f'weight_{i+1}'] = None if weight is None else Decimal(str(weight))
                row[f'reps_{i+1}'] = None if reps is None else Decimal(str(reps))
            saved.append(row)
            return row
        async def fetch(self, sql, user_id, *args):
            assert 'user_id=$1' in sql and user_id == current_user_id()
            return [row for row in saved if row['user_id'] == user_id]
    class Store(MemoryStore):
        async def fetch_workout_plan(self):
            return {"rotation": ["Upper"], "days": {"Upper": {"exercises": []}}}
        async def connect(self): return Pool()
        insert_workout = RealStore.insert_workout
        fetch_workouts = RealStore.fetch_workouts
    store = Store()
    monkeypatch.setattr(srv, "store_client", lambda: store)
    async def today(_):
        return {"has_plan": True, "today_type": "Upper", "done": False,
                "exercises": [{"name": "Fixture press", "sets": 3, "reps": "8", "rest_sec": 90}]}
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": today})
    scope = bind_user(uuid4())
    try:
        row = await srv.write_workout("Fixture press", [{"weight": 10, "reps": 8}], "Upper", None)
        assert row["workout_type"] == ["Upper"]
        assert saved[0]["workout_type"] == ["Upper"], "Production TEXT[] shape, not scalar"
        assert saved[0]["muscle_group"] == [], "Never mislabel a custom day as Push"
        readback = await srv._coach_tool_handlers()["get_recent_workouts"]({"n": 5})
        assert readback["workouts"][0]["workout_type"] == ["Upper"]
        assert readback["workouts"][0]["sets"] == [{"weight": 10, "reps": 8}]
        coverage = domain.workout_week_stats(readback["workouts"], domain.effective_date())["coverage"]
        assert coverage["workout_types"]["Upper"] == 1
        assert coverage["workout_types"]["Push"] == 0
        assert coverage["untouched"] == []
        assert coverage["muscle_coverage_complete"] is False
        assert coverage["unclassified_days"] == [domain.effective_date().isoformat()]
        assert "unknown" in coverage["note"].lower()
        sid = str(uuid4())
        await service.snapshot(sid, create=True)
        await service.action(sid, {"action": "start_workout"})
        result = await service.action(sid, {"action": "exercise_logged", "reference": row["id"]})
        ex = result["workout"]["activeExerciseId"]
        assert result["workout"]["loggedSets"] == {ex: 1}
        assert result["workout"]["restEndsAt"] is not None
        assert "outside the assigned" not in str(result["surfaces"])
        replay = await service.action(sid, {"action": "exercise_logged", "reference": row["id"]})
        assert replay["workout"]["loggedSets"] == {ex: 1}
        for invalid in ["Unassigned", "<Upper>", "Upper\n", "x" * 81]:
            with pytest.raises(ValueError):
                await srv.write_workout("Fixture press", [{"weight": 10, "reps": 8}], invalid, None)
        assert len(saved) == 1
    finally:
        reset_user(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("label,legacy_null,expected_complete,expected_push", [
    ("Cardio", False, True, 1), ("Full Body", False, True, 1),
    ("Push", True, True, 1), ("Upper", False, False, 1),
])
async def test_coverage_classification_through_real_writer_store_coach(
        monkeypatch, label, legacy_null, expected_complete, expected_push):
    import server as srv
    import domain
    from store import Store as RealStore
    from decimal import Decimal
    from datetime import datetime, timezone
    from auth import current_user_id
    saved = []

    class Pool:
        async def fetchrow(self, sql, row_id, user_id, exercise, types, muscles, *values):
            assert 'INSERT INTO fitness_tracker' in sql
            assert user_id == current_user_id()
            row = {'id': row_id, 'user_id': user_id, 'exercise_name': exercise,
                   'workout_type': list(types), 'muscle_group': list(muscles),
                   'day': values[-1], 'created_at': datetime.now(timezone.utc)}
            for i in range(4):
                weight, reps = values[2*i:2*i+2]
                row[f'weight_{i+1}'] = None if weight is None else Decimal(str(weight))
                row[f'reps_{i+1}'] = None if reps is None else Decimal(str(reps))
            saved.append(row)
            return row

        async def fetch(self, sql, user_id, *args):
            assert 'user_id=$1' in sql and user_id == current_user_id()
            return [row for row in saved if row['user_id'] == user_id]

    class Store(MemoryStore):
        async def fetch_workout_plan(self):
            return {"rotation": ["Upper"], "days": {"Upper": {"exercises": []}}}
        async def connect(self): return Pool()
        insert_workout = RealStore.insert_workout
        fetch_workouts = RealStore.fetch_workouts

    store = Store()
    monkeypatch.setattr(srv, 'store_client', lambda: store)
    scope = bind_user(uuid4())
    try:
        # Standard Push plus each standard/custom case, using the REAL writer.
        await srv.write_workout('Fixture press', [{'weight': 10, 'reps': 8}], 'Push', None)
        await srv.write_workout('Fixture second', [{'weight': 5, 'reps': 6}], label, None)
        if legacy_null:
            for row in saved:
                row['muscle_group'] = None  # actual legacy DB shape at read boundary
        assert all(isinstance(row['workout_type'], list) for row in saved)
        assert all(isinstance(row['weight_1'], Decimal) for row in saved)
        result = await srv._coach_tool_handlers()['get_recent_workouts']({'n': 5})
        assert len(result['workouts']) == 2
        coverage = domain.workout_week_stats(result['workouts'], domain.effective_date())['coverage']
        assert coverage['muscle_coverage_complete'] is expected_complete
        assert coverage['muscle_groups']['Push'] == expected_push
        assert coverage['workout_types'][label] == 1
        assert ('Pull' in coverage['untouched']) is expected_complete
        assert bool(coverage['unclassified_days']) is not expected_complete
        assert (coverage['note'] is None) is expected_complete
    finally:
        reset_user(scope)


@pytest.mark.parametrize("custom_type", [["Upper"], "Upper"])
def test_custom_coverage_counts_distinct_days_without_inventing_muscles(custom_type):
    import domain
    from datetime import date
    rows = [
        {"date": "2026-09-16", "workout_type": custom_type, "muscle_group": [], "sets": [{"weight": 10, "reps": 8}]},
        {"date": "2026-09-16", "workout_type": custom_type, "muscle_group": [], "sets": [{"weight": 10, "reps": 8}]},
        {"date": "2026-09-15", "workout_type": ["Push"], "muscle_group": ["Push"], "sets": []},
        {"date": "2026-09-01", "workout_type": ["Lower"], "muscle_group": [], "sets": []},
    ]
    result = domain.workout_week_stats(rows, date(2026, 9, 16))
    assert result["week"]["days_logged"] == 2
    assert result["week"]["total_sets"] == 2
    assert result["week"]["total_volume"] == 160
    coverage = result["coverage"]
    assert coverage["workout_types"]["Upper"] == 1
    assert coverage["workout_types"]["Push"] == 1
    assert "Lower" not in coverage["workout_types"]
    assert coverage["muscle_groups"]["Push"] == 1
    assert coverage["unclassified_days"] == ["2026-09-16"]
    assert coverage["untouched"] == []
    # Removing the unclassified rows restores the old negative claims; a Rest
    # marker itself does not make muscle coverage incomplete.
    known = domain.workout_week_stats(rows[2:] + [{"date": "2026-09-16", "workout_type": ["Rest"], "muscle_group": []}], date(2026, 9, 16))["coverage"]
    assert known["muscle_coverage_complete"] is True
    assert known["note"] is None
    assert "Push" not in known["untouched"]
    assert "Pull" in known["untouched"]


@pytest.mark.asyncio
async def test_off_plan_custom_logger_readback_does_not_block_finish():
    store = MemoryStore()
    done = False
    async def rows(_):
        return [{"id": "off-plan", "exercise": "Custom accessory", "workout_type": "Lower",
                 "sets": [{"weight": 10, "reps": 8}]}]
    store.fetch_workouts = rows
    async def today(_):
        return {"has_plan": True, "today_type": "Upper", "done": done,
                "exercises": [{"name": "Fixture press", "sets": 3, "reps": "8", "rest_sec": 90}]}
    async def complete(_):
        nonlocal done
        done = True
        return {"done": True}
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
        coach_factory=lambda: {"get_today_session": today, "complete_today_session": complete})
    scope = bind_user(uuid4())
    try:
        sid = str(uuid4())
        await service.snapshot(sid, create=True)
        await service.action(sid, {"action": "start_workout"})
        result = await service.action(sid, {"action": "exercise_logged", "reference": "off-plan"})
        assert result["workout"]["loggedSets"] == {}
        assert result["workout"]["restEndsAt"] is None
        assert "outside the assigned" in str(result["surfaces"])
        replay = await service.action(sid, {"action": "exercise_logged", "reference": "off-plan"})
        assert replay["workout"] == result["workout"]
        with pytest.raises(ValueError, match="readback"):
            await service.action(sid, {"action": "exercise_logged", "reference": "other-tenant-or-day"})
        result = await service.action(sid, {"action": "complete_workout"})
        assert result["workout"]["done"] is True
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_session_replay_budget_rejects_new_turn_without_losing_old_identity(monkeypatch):
    import agent_canvas
    monkeypatch.setattr(agent_canvas, "SESSION_TURN_LIMIT", 2)
    calls = []
    async def agent(**kwargs):
        calls.append(kwargs["message"])
        return "Ready", []
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {}, coach_factory=lambda: {}, agent=agent)
    scope = bind_user(uuid4())
    try:
        sid, first = str(uuid4()), str(uuid4())
        original = await service.turn(sid, first, "one", adapter="text")
        await service.turn(sid, str(uuid4()), "two", adapter="text")
        with pytest.raises(ValueError, match="history limit"):
            await service.turn(sid, str(uuid4()), "three", adapter="text")
        assert await service.turn(sid, first, "one", adapter="text") == original
        assert calls == ["one", "two"]
        assert len(service.session(sid).turns) == 2
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_replay_byte_budget_preserves_existing_turn(monkeypatch):
    import agent_canvas
    monkeypatch.setattr(agent_canvas, "SESSION_REPLAY_BYTE_LIMIT", 1)
    async def agent(**_): return "Ready", []
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {}, coach_factory=lambda: {}, agent=agent)
    scope = bind_user(uuid4())
    try:
        sid, tid = str(uuid4()), str(uuid4())
        first = await service.turn(sid, tid, "one", adapter="text")
        with pytest.raises(ValueError, match="history limit"):
            await service.turn(sid, str(uuid4()), "two", adapter="text")
        assert await service.turn(sid, tid, "one", adapter="text") == first
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_waiter_is_protected_during_unlocked_handoff(monkeypatch):
    import asyncio
    import agent_canvas
    monkeypatch.setattr(agent_canvas, "GLOBAL_SESSION_LIMIT", 1)
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {}, coach_factory=lambda: {})
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        entered = asyncio.Event()
        async def wait():
            async with service.lease(session): entered.set()
        async with service.lease(session):
            waiter = asyncio.create_task(wait())
            await asyncio.sleep(0)
            assert session.leases == 2
        # No event-loop yield here: the lock is free, but a waiter owns a lease.
        assert not session.lock.locked()
        assert session.leases == 1
        with pytest.raises(ValueError, match="active"):
            service.session(str(uuid4()), create=True)
        await waiter
        assert entered.is_set() and session.leases == 0
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_tenant_active_limit_protects_other_tenants_and_cleans_up():
    import agent_canvas
    from contextlib import AsyncExitStack
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {}, coach_factory=lambda: {})
    tenant = uuid4()
    scope = bind_user(tenant)
    try:
        async with AsyncExitStack() as stack:
            for _ in range(agent_canvas.TENANT_ACTIVE_LIMIT):
                await stack.enter_async_context(service.lease(service.session(str(uuid4()), create=True)))
            with pytest.raises(ValueError, match="active Canvas"):
                async with service.lease(service.session(str(uuid4()), create=True)):
                    pytest.fail("Must not admit")
            other = bind_user(uuid4())
            try:
                await service.snapshot(str(uuid4()), create=True)
            finally:
                reset_user(other)
        assert service.active_by_tenant == {}
        assert all(s.leases == 0 for s in service.sessions.values())
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_global_pressure_revokes_idle_previews_but_never_writes_or_blocks_new_tenant():
    import agent_canvas
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {}, coach_factory=lambda: {})
    # Adversarial tenants fill every slot with an idle pending/uncertain preview.
    old = []
    for index in range(agent_canvas.GLOBAL_SESSION_LIMIT):
        tenant = UUID(int=1 + index // agent_canvas.TENANT_SESSION_LIMIT)
        scope = bind_user(tenant)
        try:
            sid = str(uuid4())
            session = service.session(sid, create=True)
            session.approval = {"id": "preview", "status": "uncertain" if index % 2 else "pending"}
            session.canvas.present("approval", "approval", [{"id": "confirm", "component": "ConfirmationCard"}])
            old.append((tenant, sid, session))
        finally:
            reset_user(scope)
    scope = bind_user(uuid4())
    try:
        result = await service.snapshot(str(uuid4()), create=True)
        assert result["approval"] is None
        assert len(service.sessions) == agent_canvas.GLOBAL_SESSION_LIMIT
    finally:
        reset_user(scope)
    tenant, sid, previous = next(item for item in old if item[:2] not in service.sessions)
    scope = bind_user(tenant)
    try:
        with pytest.raises(ValueError, match="expired"):
            await service.action(sid, {"action": "confirm", "reference": "preview"})
        fresh = await service.snapshot(sid, create=True)
        assert fresh["instanceId"] != previous.canvas.instance_id
        assert fresh["approval"] is None
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_all_pinned_same_tenant_fails_closed_but_other_tenant_recovers():
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {}, coach_factory=lambda: {})
    scope = bind_user(uuid4())
    try:
        for _ in range(128):
            entry = service.session(str(uuid4()), create=True)
            entry.canvas.present("approval", "approval", [{"id": "confirm", "component": "ConfirmationCard"}])
        with pytest.raises(ValueError, match="session limit"):
            service.session(str(uuid4()), create=True)
    finally:
        reset_user(scope)
    scope = bind_user(uuid4())
    try:
        assert (await service.snapshot(str(uuid4()), create=True))["revision"] == 0
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_running_and_waiting_leases_are_never_evicted(monkeypatch):
    import agent_canvas
    monkeypatch.setattr(agent_canvas, "GLOBAL_SESSION_LIMIT", 2)
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {}, coach_factory=lambda: {})
    scope = bind_user(uuid4())
    try:
        a = service.session(str(uuid4()), create=True)
        b = service.session(str(uuid4()), create=True)
        async with service.lease(a):
            async with service.lease(b):
                other_scope = bind_user(uuid4())
                try:
                    with pytest.raises(ValueError, match="active"):
                        service.session(str(uuid4()), create=True)
                    assert len(service.sessions) == 2
                finally:
                    reset_user(other_scope)
        assert service.session(str(uuid4()), create=True)
        assert len(service.sessions) == 2
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_128_sessions_from_one_tenant_do_not_exhaust_another():
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {}, coach_factory=lambda: {})
    scope = bind_user(uuid4())
    try:
        for _ in range(128):
            await service.snapshot(str(uuid4()), create=True)
    finally:
        reset_user(scope)
    scope = bind_user(uuid4())
    try:
        sid = str(uuid4())
        await service.snapshot(sid, create=True)
        result = await service.action(sid, {"action": "show_progress"})
        assert result["surfaces"][0]["components"][0]["component"] == "WeeklyTrend"
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_tenant_cap_evicts_oldest_idle_nonapproval_and_expires_idle(monkeypatch):
    import agent_canvas
    clock = [100.0]
    monkeypatch.setattr(agent_canvas.time, "monotonic", lambda: clock[0])
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {}, coach_factory=lambda: {})
    scope = bind_user(uuid4())
    try:
        sid = str(uuid4())
        first = service.session(sid, create=True)
        first.canvas.present("approval", "approval", [{"id": "preview", "component": "ConfirmationCard"}])
        ordinary_ids = []
        for _ in range(127):
            clock[0] += 1
            ordinary_ids.append(str(uuid4()))
            service.session(ordinary_ids[-1], create=True)
        recovered = service.session(str(uuid4()), create=True)
        assert recovered.canvas.revision == 0
        assert len(service.sessions) == 128
        with pytest.raises(ValueError, match="expired"):
            service.session(ordinary_ids[0])
        assert service.session(sid) is first
        assert "approval" in first.canvas.surfaces
        # A running turn must retain the same lock/state beyond the idle TTL.
        await first.lock.acquire()
        clock[0] += 3601
        service.session(str(uuid4()), create=True)
        assert len(service.sessions) == 2
        assert service.session(sid) is first
        first.lock.release()
        clock[0] += 3601
        service.session(str(uuid4()), create=True)
        assert len(service.sessions) == 1
        with pytest.raises(ValueError, match="expired"):
            service.session(sid)
    finally:
        reset_user(scope)
