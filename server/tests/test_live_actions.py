"""Voice actions exercise the real application handlers and delegated protocol."""
import asyncio
import copy
import json
import os
from datetime import date
from uuid import uuid4

import pytest

os.environ.setdefault("APP_SHARED_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
import server as srv
import live_coach
from auth import bind_user, reset_user, current_user_id
from pathlib import Path
from test_coach import FakeStore as CoachStore, rotation_plan
from test_live_coach import FakeClientWebSocket
from store import Store
from test_server import FakeStore as MealStore
from jsonschema import Draft202012Validator
from live_tools import live_tool_definitions


def test_voice_workout_schema_requires_splitting_five_sets_into_valid_calls():
    schema = next(tool["parameters"] for tool in live_tool_definitions() if tool["name"] == "log_workout")
    validator = Draft202012Validator(schema)
    args = {"exercise": "Bench Press", "workout_type": "Push", "weight_unit": "lb", "sets": [{"weight": 120, "reps": 10}] * 4 + [{"weight": 130, "reps": 8}]}
    assert not validator.is_valid(args)
    assert validator.is_valid({**args, "sets": args["sets"][:4]})
    assert validator.is_valid({**args, "sets": args["sets"][4:]})


@pytest.mark.asyncio
async def test_delegated_completed_calls_execute_once_in_tenant_and_continue_after_results():
    tenant = uuid4()
    writes, sent, changes = [], [], []
    async def log(args):
        writes.append((current_user_id(), args))
        return {"id": "saved", "sets": args["sets"]}
    async def send(event): sent.append(event)
    async def changed(): changes.append(True)
    runner = live_coach.LiveToolSession(user_id=tenant, handlers={"log_workout": log}, send=send, changed=changed)
    def envelope(event): return {"type": "response.event", "delegation_id": "d1", "event": event}
    await runner.handle(envelope({"type": "response.created", "response": {"id": "r1"}}))
    call = {"type": "function_call", "call_id": "c1", "name": "log_workout", "arguments": json.dumps({"sets": [{"weight": 120, "reps": 10}]})}
    for _ in range(2):
        await runner.handle(envelope({"type": "response.output_item.done", "item": call}))
    assert writes == []  # only complete responses may commit, never fragments
    await runner.handle(envelope({"type": "response.completed", "response": {"id": "r1", "output": []}}))
    await runner.handle(envelope({"type": "response.completed", "response": {"id": "r1", "output": []}}))
    assert writes == [(tenant, {"sets": [{"weight": 120, "reps": 10}]})]
    assert [e["type"] for e in sent] == ["response.item.create", "response.create"]
    assert json.loads(sent[0]["item"]["output"])["id"] == "saved"
    assert changes == [True]
    with pytest.raises(Exception): current_user_id()


@pytest.mark.asyncio
async def test_delegated_failure_and_tenant_injection_never_claim_success():
    sent, writes = [], []
    async def log(args):
        writes.append(args)
        raise RuntimeError("SECRET database details")
    async def send(event): sent.append(event)
    async def changed(): pytest.fail("failed action must not refresh")
    runner = live_coach.LiveToolSession(user_id=uuid4(), handlers={"log_meal": log}, send=send, changed=changed)
    for index, args in enumerate(({"user_id": "other"}, {})):
        def envelope(event): return {"type": "response.event", "delegation_id": f"d{index}", "event": event}
        await runner.handle(envelope({"type": "response.created", "response": {"id": f"r{index}"}}))
        await runner.handle(envelope({"type": "response.output_item.done", "item": {"type": "function_call", "call_id": f"c{index}", "name": "log_meal", "arguments": json.dumps(args)}}))
        await runner.handle(envelope({"type": "response.completed", "response": {"id": f"r{index}"}}))
    assert writes == [{}]
    assert all("error" in json.loads(e["item"]["output"]) for e in sent if "item" in e)
    assert "SECRET" not in json.dumps(sent)


class WorkoutStore:
    def __init__(self):
        self.plan = {"rotation": ["Push"], "days": {"Push": {"exercises": [{"name": "Bench Press", "sets": 3, "reps": "8-10", "rest_sec": 90}, {"name": "Fly", "sets": 3, "reps": "12", "rest_sec": 60}]}}}
        self.sessions, self.logs = {}, []
    async def fetch_workout_plan(self): return copy.deepcopy(self.plan)
    async def fetch_session_day_state(self): return None
    async def fetch_workouts(self, *args, **kwargs): return []
    async def fetch_today_workout(self, day): return copy.deepcopy(self.sessions.get((current_user_id(), day)))
    async def put_today_workout(self, day, workout_type, exercises):
        row = {"workout_type": workout_type, "exercises": copy.deepcopy(exercises)}
        self.sessions[current_user_id(), day] = row
        return row
    async def fetch_workout_library(self): return [{"name": "Dumbbell Bench Press", "workout_type": ["Push"]}]
    async def insert_workout(self, **kwargs):
        self.logs.append((current_user_id(), kwargs)); return {"id": "workout-1"}


@pytest.mark.asyncio
async def test_voice_swap_is_persisted_for_today_only_and_preserves_routine(monkeypatch):
    store = WorkoutStore()
    baseline = copy.deepcopy(store.plan)
    monkeypatch.setattr(srv, "store_client", lambda: store)
    token = bind_user(uuid4())
    try:
        handlers = srv._live_tool_handlers()
        result = await handlers["replace_today_exercise"]({"old_exercise": "Bench Press", "new_exercise": "Dumbbell Bench Press"})
        assert result["today_type"] == "Push"
        assert result["exercises"] == [{"name": "Dumbbell Bench Press", "sets": 3, "reps": "8-10", "rest_sec": 90}, baseline["days"]["Push"]["exercises"][1]]
        assert (await handlers["get_today_session"]({}))["exercises"] == result["exercises"]
        assert store.plan == baseline
        assert await store.fetch_today_workout(date(2000, 1, 1)) is None
    finally: reset_user(token)


@pytest.mark.asyncio
async def test_voice_logging_preserves_each_set_and_rejects_missing_units(monkeypatch):
    store = WorkoutStore()
    monkeypatch.setattr(srv, "store_client", lambda: store)
    token = bind_user(uuid4())
    try:
        handler = srv._live_tool_handlers()["log_workout"]
        args = {"exercise": "Bench Press", "workout_type": "Push", "sets": [{"weight": 120, "reps": 10}, {"weight": 115, "reps": 8}]}
        with pytest.raises(srv.MacroError): await handler(args)
        assert store.logs == []
        result = await handler({**args, "weight_unit": "lb"})
        assert result["sets"] == args["sets"]
        assert store.logs[0][1]["sets"] == args["sets"]
    finally: reset_user(token)


@pytest.mark.asyncio
async def test_bridge_returns_real_function_result_and_canonical_refresh():
    tenant = uuid4()
    client = FakeClientWebSocket()
    writes = []
    class Provider:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.sent = []
        async def recv(self): return json.dumps(await self.queue.get())
        async def send(self, raw):
            event = json.loads(raw); self.sent.append(event)
            if event["type"] == "response.create":
                await self.queue.put({"type": "session.closed", "reason": "close_requested"})
    provider = Provider()
    async def log(args):
        writes.append(current_user_id())
        return {"id": "persisted-meal"}
    for event in [
        {"type": "response.created", "response": {"id": "r"}},
        {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c", "name": "log_meal", "arguments": "{}"}},
        {"type": "response.completed", "response": {"id": "r", "output": []}},
    ]:
        await provider.queue.put({"type": "response.event", "delegation_id": "d", "event": event})
    service = live_coach.LiveCoachService(store=None, provider_connect=None, api_key="test", handlers={"log_meal": log})
    await asyncio.wait_for(service._bridge(client, provider, tenant), 1)
    assert writes == [tenant]
    fixture = json.loads((Path(__file__).parents[2] / "shared/fixtures/live-data-changed.json").read_text())
    assert client.sent[0] == fixture
    assert json.loads(provider.sent[0]["item"]["output"]) == {"id": "persisted-meal"}
    assert all(e["type"] != "response.event" for e in client.sent)


@pytest.mark.asyncio
async def test_failed_response_does_not_execute_collected_calls():
    async def never(*args): pytest.fail("failed Responses must not commit tools")
    runner = live_coach.LiveToolSession(user_id=uuid4(), handlers={"log_meal": never}, send=never, changed=never)
    for event in [
        {"type": "response.created", "response": {"id": "r"}},
        {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c", "name": "log_meal", "arguments": "{}"}},
        {"type": "response.failed", "response": {"id": "r"}},
        {"type": "response.completed", "response": {"id": "r"}},
    ]:
        await runner.handle({"type": "response.event", "delegation_id": "d", "event": event})


@pytest.mark.asyncio
async def test_daily_override_sql_binds_tenant_and_day_on_read_and_write():
    tenant, day = uuid4(), date(2026, 9, 11)
    calls = []
    class Pool:
        async def fetchrow(self, query, *args):
            calls.append((query, args))
            return {"workout_type": "Push", "exercises": '[{"name":"Dumbbell Bench Press"}]'}
    store = Store("postgresql://blocked")
    store.pool = Pool()
    token = bind_user(tenant)
    try:
        await store.put_today_workout(day, "Push", [{"name": "Dumbbell Bench Press"}])
        read = await store.fetch_today_workout(day)
    finally: reset_user(token)
    assert read["exercises"] == [{"name": "Dumbbell Bench Press"}]
    assert all(args[:2] == (tenant, day) for _, args in calls)
    assert "ON CONFLICT(user_id,day)" in calls[0][0]
    assert "WHERE user_id=$1 AND day=$2" in calls[1][0]


@pytest.mark.asyncio
async def test_app_plan_overlays_today_but_keeps_future_plan(monkeypatch):
    fake = CoachStore(plan=rotation_plan())
    async def override(day): return {"workout_type": "Push", "exercises": [{"name": "Dumbbell Bench Press", "sets": 4, "reps": "10", "rest_sec": 90}]}
    fake.fetch_today_workout = override
    monkeypatch.setattr(srv, "_client", fake)
    payload = await srv.workout_plan_payload()
    fixture = json.loads((Path(__file__).parents[2] / "shared/fixtures/today-workout-plan.json").read_text())
    assert payload == fixture
    assert payload["upcoming"][0]["exercises"][0]["name"] == "Dumbbell Bench Press"
    assert payload["upcoming"][3]["exercises"][0]["name"] == "Bench Press"
    assert fake.plan["days"]["Push"]["exercises"][0]["name"] == "Bench Press"


@pytest.mark.asyncio
async def test_two_voice_users_cannot_see_each_others_today_swap(monkeypatch):
    store = WorkoutStore()
    monkeypatch.setattr(srv, "store_client", lambda: store)
    first, second = uuid4(), uuid4()
    token = bind_user(first)
    try:
        await srv._live_tool_handlers()["replace_today_exercise"]({"old_exercise": "Bench Press", "new_exercise": "Dumbbell Bench Press"})
    finally: reset_user(token)
    token = bind_user(second)
    try:
        session = await srv._live_tool_handlers()["get_today_session"]({})
        assert session["exercises"][0]["name"] == "Bench Press"
    finally: reset_user(token)


@pytest.mark.asyncio
async def test_voice_meal_logs_verified_source_and_returns_fresh_macros(monkeypatch):
    store = MealStore([], lagging=False)
    async def presets(): return []
    store.fetch_presets = presets
    monkeypatch.setattr(srv, "store_client", lambda: store)
    args = {"name": "Protein shake", "meal_type": "Snack", "calories": 150, "protein": 30, "carbs": 4, "fat": 2, "fiber": 0, "macro_source": "Nutrition label on bottle"}
    token = bind_user(uuid4())
    try:
        handler = srv._live_tool_handlers()["log_meal"]
        with pytest.raises(srv.MacroError): await handler({**args, "macro_source": "estimate"})
        assert store.inserted == []
        result = await handler(args)
        today = await srv._live_tool_handlers()["get_today"]({})
        assert today["totals"]["calories"] == 150
        assert today["totals"]["protein"] == 30
        assert today["remaining"]["calories"] == 2250
        assert store.inserted[0]["macro_source"] == "Nutrition label on bottle"
    finally: reset_user(token)


@pytest.mark.asyncio
async def test_backend_text_stream_does_not_fill_action_queue():
    client = FakeClientWebSocket()
    events = [{"type": "response.event", "delegation_id": "d", "event": {"type": "response.output_text.delta", "delta": "word"}} for _ in range(100)]
    events.append({"type": "session.closed", "reason": "close_requested"})
    class Provider:
        async def recv(self): return json.dumps(events.pop(0))
        async def send(self, event): pass
    service = live_coach.LiveCoachService(store=None, provider_connect=None, api_key="test")
    await asyncio.wait_for(service._bridge(client, Provider(), uuid4()), 1)
    assert client.sent == [{"type": "session.closed", "reason": "close_requested"}]


@pytest.mark.asyncio
async def test_close_stops_new_actions_even_when_upstream_queue_is_saturated():
    close_observed, blocked_send, wrote = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Client(FakeClientWebSocket):
        async def receive_json(self):
            event = await super().receive_json()
            if event["type"] == "session.close": close_observed.set()
            return event
    client = Client()
    class Provider:
        def __init__(self): self.incoming = asyncio.Queue()
        async def recv(self): return json.dumps(await self.incoming.get())
        async def send(self, raw):
            blocked_send.set()
            await asyncio.Event().wait()
    provider = Provider()
    async def log(args): wrote.set(); return {"id": "unexpected"}
    service = live_coach.LiveCoachService(store=None, provider_connect=None, api_key="test", handlers={"log_meal": log}, policy=live_coach.LiveCoachPolicy(queue_size=1, close_timeout=0.1))
    task = asyncio.create_task(service._bridge(client, provider, uuid4()))
    audio = {"type": "session.input_audio.append", "audio": "AAAAAA=="}
    try:
        await client.incoming.put(audio)
        await asyncio.wait_for(blocked_send.wait(), 1)
        await client.incoming.put(audio)
        await client.incoming.put({"type": "session.close"})
        await asyncio.wait_for(close_observed.wait(), 1)
        for event in [
            {"type": "response.created", "response": {"id": "r"}},
            {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c", "name": "log_meal", "arguments": "{}"}},
            {"type": "response.completed", "response": {"id": "r"}},
        ]:
            await provider.incoming.put({"type": "response.event", "delegation_id": "d", "event": event})
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.02)
        assert not wrote.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_provider_loss_cancels_action_before_waiting_for_congested_client():
    started, cancelled, client_blocked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Client(FakeClientWebSocket):
        async def send_json(self, event):
            client_blocked.set()
            await asyncio.Event().wait()
    class Provider:
        def __init__(self): self.incoming = asyncio.Queue()
        async def recv(self):
            event = await self.incoming.get()
            if event is None: raise ConnectionError("synthetic disconnect")
            return json.dumps(event)
        async def send(self, raw): pass
    async def action(args):
        started.set()
        try: await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        pytest.fail("disconnected session must not resume an uncommitted action")
    provider = Provider()
    service = live_coach.LiveCoachService(store=None, provider_connect=None, api_key="test", handlers={"log_meal": action}, policy=live_coach.LiveCoachPolicy(queue_size=1, close_timeout=1))
    task = asyncio.create_task(service._bridge(Client(), provider, uuid4()))
    try:
        for event in [
            {"type": "response.created", "response": {"id": "r"}},
            {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c", "name": "log_meal", "arguments": "{}"}},
            {"type": "response.completed", "response": {"id": "r"}},
        ]:
            await provider.incoming.put({"type": "response.event", "delegation_id": "d", "event": event})
            await asyncio.sleep(0.005)
        await asyncio.wait_for(started.wait(), 1)
        await provider.incoming.put({"type": "session.output_transcript.delta", "delta": "one"})
        await asyncio.wait_for(client_blocked.wait(), 1)
        await provider.incoming.put({"type": "session.output_transcript.delta", "delta": "two"})
        await provider.incoming.put(None)
        await asyncio.wait_for(cancelled.wait(), 0.1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_today_prescription_edit_uses_revision_and_preserves_logs_and_routine(monkeypatch):
    store = WorkoutStore()
    async def library(): return [{"name": "Bench Press"}, {"name": "Fly"}, {"name": "Dumbbell Bench Press"}]
    store.fetch_workout_library = library
    monkeypatch.setattr(srv, "store_client", lambda: store)
    baseline = copy.deepcopy(store.plan)
    tenant = uuid4()
    token = bind_user(tenant)
    try:
        handlers = srv._live_tool_handlers()
        before = await handlers["get_today_session"]({})
        exercises = copy.deepcopy(before["exercises"])
        exercises[0].update(sets=4, reps="10", rest_sec=90)
        result = await handlers["update_today_workout"]({"session_revision": before["session_revision"], "exercises": exercises})
        assert result["exercises"][0] == {"name": "Bench Press", "sets": 4, "reps": "10", "rest_sec": 90}
        assert result["exercises"][1] == before["exercises"][1]
        assert result["session_revision"] != before["session_revision"]
        with pytest.raises(srv.MacroError):
            await handlers["update_today_workout"]({"session_revision": before["session_revision"], "exercises": exercises})
        assert store.plan == baseline and store.logs == []
        tomorrow = srv.domain.effective_date() + __import__("datetime").timedelta(days=1)
        monkeypatch.setattr(srv.domain, "effective_date", lambda: tomorrow)
        assert (await handlers["get_today_session"]({}))["exercises"] == before["exercises"]
    finally: reset_user(token)


@pytest.mark.asyncio
async def test_today_edit_rejects_unknown_exercises_invalid_prescription_and_other_tenant_revision(monkeypatch):
    store = WorkoutStore()
    async def library(): return [{"name": "Bench Press"}, {"name": "Fly"}]
    store.fetch_workout_library = library
    monkeypatch.setattr(srv, "store_client", lambda: store)
    token = bind_user(uuid4())
    try:
        handlers = srv._live_tool_handlers()
        before = await handlers["get_today_session"]({})
        for replacement in ({"name": "Invented exercise", "sets": 4, "reps": "10", "rest_sec": 90}, {"name": "Bench Press", "sets": 11, "reps": "10", "rest_sec": 90}):
            with pytest.raises(srv.MacroError):
                await handlers["update_today_workout"]({"session_revision": before["session_revision"], "exercises": [replacement]})
        assert store.sessions == {}
    finally: reset_user(token)
    token = bind_user(uuid4())
    try:
        with pytest.raises(srv.MacroError):
            await srv._live_tool_handlers()["update_today_workout"]({"session_revision": before["session_revision"], "exercises": before["exercises"]})
        assert store.sessions == {}
    finally: reset_user(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["replace_today_exercise", "update_today_workout"])
async def test_today_changes_do_not_cross_day_rollover_during_library_read(monkeypatch, tool):
    store = WorkoutStore()
    today = srv.domain.effective_date()
    tomorrow = today + __import__("datetime").timedelta(days=1)
    async def library():
        monkeypatch.setattr(srv.domain, "effective_date", lambda: tomorrow)
        return [{"name": "Bench Press"}, {"name": "Dumbbell Bench Press"}, {"name": "Fly"}]
    store.fetch_workout_library = library
    monkeypatch.setattr(srv, "store_client", lambda: store)
    token = bind_user(uuid4())
    try:
        handlers = srv._live_tool_handlers()
        snapshot = await handlers["get_today_session"]({})
        args = {"old_exercise": "Bench Press", "new_exercise": "Dumbbell Bench Press"} if tool == "replace_today_exercise" else {"session_revision": snapshot["session_revision"], "exercises": snapshot["exercises"]}
        with pytest.raises(srv.MacroError):
            await handlers[tool](args)
        assert store.sessions == {}
    finally: reset_user(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["replace_today_exercise", "update_today_workout"])
async def test_today_edit_does_not_overwrite_a_change_during_library_read(monkeypatch, tool):
    store = WorkoutStore()
    tenant, today = uuid4(), srv.domain.effective_date()
    concurrent = {"workout_type": "Push", "exercises": [{"name": "Fly", "sets": 2, "reps": "15", "rest_sec": 45}]}
    async def library():
        store.sessions[tenant, today] = copy.deepcopy(concurrent)
        return [{"name": "Bench Press"}, {"name": "Dumbbell Bench Press"}, {"name": "Fly"}]
    store.fetch_workout_library = library
    monkeypatch.setattr(srv, "store_client", lambda: store)
    token = bind_user(tenant)
    try:
        handlers = srv._live_tool_handlers()
        snapshot = await handlers["get_today_session"]({})
        args = {"old_exercise": "Bench Press", "new_exercise": "Dumbbell Bench Press"} if tool == "replace_today_exercise" else {"session_revision": snapshot["session_revision"], "exercises": snapshot["exercises"]}
        with pytest.raises(srv.MacroError): await handlers[tool](args)
        assert store.sessions[tenant, today] == concurrent
    finally: reset_user(token)


@pytest.mark.asyncio
async def test_today_add_remove_and_clear_planned_exercises_keep_completed_logs(monkeypatch):
    store = WorkoutStore()
    async def library(): return [{"name": "Bench Press"}, {"name": "Fly"}, {"name": "Dumbbell Bench Press"}]
    store.fetch_workout_library = library
    monkeypatch.setattr(srv, "store_client", lambda: store)
    token = bind_user(uuid4())
    try:
        handlers = srv._live_tool_handlers()
        await handlers["log_workout"]({"exercise": "Bench Press", "workout_type": "Push", "weight_unit": "lb", "sets": [{"weight": 120, "reps": 10}]})
        completed = copy.deepcopy(store.logs)
        routine = copy.deepcopy(store.plan)
        before = await handlers["get_today_session"]({})
        remaining = [before["exercises"][1]]
        removed = await handlers["update_today_workout"]({"session_revision": before["session_revision"], "exercises": remaining})
        added_exercise = {"name": "Dumbbell Bench Press", "sets": 4, "reps": "10", "rest_sec": 90}
        added = await handlers["update_today_workout"]({"session_revision": removed["session_revision"], "exercises": remaining + [added_exercise]})
        assert added["exercises"] == remaining + [added_exercise]
        cleared = await handlers["update_today_workout"]({"session_revision": added["session_revision"], "exercises": []})
        assert cleared["exercises"] == []
        assert store.logs == completed and store.plan == routine
    finally: reset_user(token)
