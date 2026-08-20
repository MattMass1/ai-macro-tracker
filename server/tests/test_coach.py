"""Network-free tests for coach tool dispatch and loop guardrails."""
import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import pytest

os.environ.setdefault("NOTION_TOKEN", "secret_test")
os.environ.setdefault("NUTRITION_DS_ID", "ds-nutrition")
os.environ.setdefault("TARGETS_DS_ID", "ds-targets")
os.environ.setdefault("PRESETS_DS_ID", "ds-presets")
os.environ.setdefault("APP_SHARED_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

from starlette.requests import Request  # noqa: E402

import domain  # noqa: E402
from auth import bind_user, current_user_id, reset_user  # noqa: E402
from coach import CoachProviderError, TOOLS, run_agent  # noqa: E402
from domain import MacroError  # noqa: E402
from store import ChatQuotaExceeded, Store  # noqa: E402
import server as srv  # noqa: E402


def response(content, stop_reason="tool_use", status=200, **extra):
    return httpx.Response(
        status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json={"content": content, "stop_reason": stop_reason, **extra},
    )


async def test_tool_dispatch_keeps_authenticated_user_scope(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    wanted_user = uuid4()
    seen = []
    calls = 0

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response([{"type": "tool_use", "id": "t1", "name": "get_today", "input": {}}])
        assert payload["messages"][-1]["content"][0]["type"] == "tool_result"
        return response([{"type": "text", "text": "You're all set."}], "end_turn")

    async def get_today(_args):
        seen.append(current_user_id())
        return {"calories": 100}

    token = bind_user(wanted_user)
    try:
        reply, audit = await run_agent(history=[], message="How am I doing?", onboarding=False,
                                       handlers={"get_today": get_today}, post=fake_post)
    finally:
        reset_user(token)
    assert reply == "You're all set."
    assert seen == [wanted_user]
    assert audit == [{"tool": "get_today", "input": {}, "ok": True}]


async def test_tool_iteration_cap_stops_after_eight_executions(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    provider_calls = tool_calls = 0

    async def fake_post(_token, _payload):
        nonlocal provider_calls
        provider_calls += 1
        return response([{"type": "tool_use", "id": f"t{provider_calls}",
                          "name": "get_today", "input": {}}])

    async def get_today(_args):
        nonlocal tool_calls
        tool_calls += 1
        return {}

    with pytest.raises(CoachProviderError, match="tool limit"):
        await run_agent(history=[], message="loop", onboarding=False,
                        handlers={"get_today": get_today}, post=fake_post)
    assert tool_calls == 8
    assert provider_calls == 9


async def test_total_execution_cap_bounds_multi_tool_rounds(monkeypatch):
    """One round can carry many tool_use blocks; the cap counts executions, not rounds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    executed = 0

    async def fake_post(_token, _payload):
        return response([{"type": "tool_use", "id": f"t{i}", "name": "get_today", "input": {}}
                         for i in range(6)])

    async def get_today(_args):
        nonlocal executed
        executed += 1
        return {}

    with pytest.raises(CoachProviderError, match="tool limit"):
        await run_agent(history=[], message="loop", onboarding=False,
                        handlers={"get_today": get_today}, post=fake_post)
    assert executed == 16  # 6 + 6 + 4, then the 17th execution is refused


async def test_same_role_history_is_merged_before_the_api_call(monkeypatch):
    """Orphaned user rows (failed prior turns) must not produce consecutive user roles."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    captured = {}

    async def fake_post(_token, payload):
        captured["messages"] = payload["messages"]
        return response([{"type": "text", "text": "Merged fine."}], "end_turn")

    history = [
        {"role": "assistant", "content": "orphaned lead reply"},
        {"role": "user", "content": "first orphan"},
        {"role": "user", "content": "second orphan"},
    ]
    reply, _ = await run_agent(history=history, message="new question", onboarding=False,
                               handlers={}, post=fake_post)
    assert reply == "Merged fine."
    assert [m["role"] for m in captured["messages"]] == ["user"]
    merged = captured["messages"][0]["content"]
    assert "first orphan" in merged and "second orphan" in merged and "new question" in merged


async def test_tool_error_is_returned_to_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = 0

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response([{"type": "tool_use", "id": "bad", "name": "log_meal", "input": {}}])
        result = payload["messages"][-1]["content"][0]
        assert result["is_error"] is True
        assert "macro_source is required" in result["content"]
        return response([{"type": "text", "text": "I need a real macro source first."}], "end_turn")

    async def log_meal(_args):
        raise ValueError("macro_source is required")

    reply, audit = await run_agent(history=[], message="log lunch", onboarding=False,
                                   handlers={"log_meal": log_meal}, post=fake_post)
    assert reply.startswith("I need")
    assert audit[0]["ok"] is False


async def test_transient_overload_is_retried_once(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = 0

    async def fake_post(_token, _payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response([], status=529)
        return response([{"type": "text", "text": "Back online."}], "end_turn")

    reply, _ = await run_agent(history=[], message="hi", onboarding=False,
                               handlers={}, post=fake_post)
    assert reply == "Back online."
    assert calls == 2


async def test_second_provider_failure_becomes_friendly_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = 0

    async def fake_post(_token, _payload):
        nonlocal calls
        calls += 1
        return response([], status=529)

    with pytest.raises(CoachProviderError, match="trouble connecting"):
        await run_agent(history=[], message="hi", onboarding=False,
                        handlers={}, post=fake_post)
    assert calls == 2  # one retry, then give up — no retry storms


async def test_usage_is_recorded_per_api_call_and_never_fatal(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = 0
    rows = []

    async def fake_post(_token, _payload):
        nonlocal calls
        calls += 1
        usage = {"input_tokens": 100 * calls, "output_tokens": calls}
        if calls == 1:
            return response([{"type": "tool_use", "id": "t1", "name": "get_today", "input": {}}],
                            model="claude-sonnet-5", usage=usage)
        return response([{"type": "text", "text": "Done."}], "end_turn",
                        model="claude-sonnet-5", usage=usage)

    async def record_usage(row):
        rows.append(row)
        raise RuntimeError("telemetry DB down")  # must never surface to the user

    async def get_today(_args):
        return {}

    reply, _ = await run_agent(history=[], message="hi", onboarding=False,
                               handlers={"get_today": get_today}, post=fake_post,
                               record_usage=record_usage)
    assert reply == "Done."
    assert rows == [
        {"model": "claude-sonnet-5", "input_tokens": 100, "output_tokens": 1},
        {"model": "claude-sonnet-5", "input_tokens": 200, "output_tokens": 2},
    ]


class FakeStore:
    def __init__(self, today_count=0, plan=None, has_targets=False, chat_messages=None):
        self.today_count = today_count
        self.plan = plan
        self.targets = has_targets
        self.inserted = []
        self.usage = []
        self.saved_presets = []
        self.display_names = []
        self.metrics = []
        self.chat_messages = chat_messages or []
        self.history_day_start = None

    async def insert_user_chat_message(self, content, daily_cap):
        if self.today_count >= daily_cap:
            raise ChatQuotaExceeded(f"daily chat cap of {daily_cap} reached")
        self.today_count += 1
        self.inserted.append(("user", content, None))
        return {}

    async def fetch_presets(self, active_only=True):
        return []

    async def save_preset(self, values, existing_id=None):
        self.saved_presets.append((dict(values), existing_id))
        return {"id": existing_id or "new", **values}

    async def put_display_name(self, name):
        self.display_names.append(name)
        return {"display_name": name}

    async def put_metrics(self, values):
        self.metrics.append(dict(values))
        return dict(values)

    async def get_metrics(self):
        return dict(self.metrics[-1]) if self.metrics else None

    async def fetch_chat_messages(self, limit=20):
        return []

    async def fetch_chat_messages_since(self, day_start, limit=20):
        self.history_day_start = day_start
        return [row for row in self.chat_messages if row["created_at"] >= day_start][:limit]

    async def fetch_meals(self, _start, _end=None):
        return []

    async def fetch_meal_rollups(self, _day):
        return []

    async def fetch_day_rollups(self, _start=None, _end=None):
        return []

    async def fetch_targets(self, _day):
        return None

    async def fetch_workout_plan(self):
        return self.plan

    async def has_macro_targets(self):
        return self.targets

    async def insert_chat_message(self, role, content, tool_calls=None):
        self.inserted.append((role, content, tool_calls))
        return {}

    async def insert_coach_usage(self, model, input_tokens, output_tokens):
        self.usage.append((model, input_tokens, output_tokens))


def chat_request(body):
    raw = json.dumps(body).encode()
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST", "path": "/api/chat",
        "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1234),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"),
                    (b"x-app-token", srv.CONFIG.app_shared_token.encode())],
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


async def test_daily_message_cap_returns_429(monkeypatch):
    fake = FakeStore(today_count=50)
    monkeypatch.setattr(srv, "_client", fake)

    http_response = await srv.api_chat(chat_request({"message": "hi"}))
    assert http_response.status_code == 429
    assert "Ask Matt to raise it" in json.loads(http_response.body)["error"]
    assert fake.inserted == []  # capped turns are not persisted


async def test_gym_chat_uses_coach_and_reports_onboarding(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def fake_parse(message):
        assert message == "what should I do today?"
        return [], None

    async def fake_run_agent(*, history, message, onboarding, handlers, record_usage=None, **_kw):
        seen.update(onboarding=onboarding, tools=sorted(handlers), history=history)
        await record_usage({"model": "claude-sonnet-5", "input_tokens": 10, "output_tokens": 5})
        return "Welcome! What's your goal?", [{"tool": "get_today", "input": {}, "ok": True}]

    async def fake_day_payload(day):
        assert day == domain.effective_date()
        return {"totals": {"calories": 725, "protein": 55, "carbs": 80,
                           "fat": 20, "fiber": 9}}

    monkeypatch.setattr(srv, "run_agent", fake_run_agent)
    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    http_response = await srv.api_chat(chat_request({"message": "what should I do today?"}))
    assert http_response.status_code == 200
    payload = json.loads(http_response.body)
    assert payload == {
        "reply": "Welcome! What's your goal?",
        "logged": [],
        "totals": {"calories": 725, "protein": 55, "carbs": 80, "fat": 20, "fiber": 9},
        "has_plan": False,
        "has_targets": False,
        "widget": None,
    }
    assert seen["onboarding"] is True  # no plan + no targets → interview mode
    assert len(seen["tools"]) == 20
    assert [row[0] for row in fake.inserted] == ["user", "assistant"]
    assert fake.usage == [("claude-sonnet-5", 10, 5)]


async def test_chat_history_is_current_coaching_day_only_and_onboarding_persists(monkeypatch):
    day_start, _ = domain.effective_day_window()
    fake = FakeStore(
        plan={"version": 1},
        has_targets=True,
        chat_messages=[
            {"role": "user", "content": "yesterday food", "created_at": day_start - timedelta(seconds=1)},
            {"role": "assistant", "content": "today plan", "created_at": day_start + timedelta(seconds=1)},
        ],
    )
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def fake_parse(_message):
        return [], None

    async def fake_run_agent(**kwargs):
        seen.update(kwargs)
        return "Fresh day.", []

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)

    response = await srv.api_chat(chat_request({"message": "hello"}))

    assert response.status_code == 200
    assert fake.history_day_start == day_start
    assert seen["history"] == [{"role": "assistant", "content": "today plan"}]
    assert seen["onboarding"] is False


async def test_chat_records_user_message_before_the_agent_loop(monkeypatch):
    """In-flight requests must already count toward the daily cap."""
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)

    async def fake_parse(_message):
        return [], None

    async def fake_run_agent(**_kw):
        assert [row[0] for row in fake.inserted] == ["user"]
        return "ok", []

    monkeypatch.setattr(srv, "run_agent", fake_run_agent)
    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    http_response = await srv.api_chat(chat_request({"message": "hi coach"}))
    assert http_response.status_code == 200
    assert [(row[0], row[1]) for row in fake.inserted] == [("user", "hi coach"), ("assistant", "ok")]


async def test_chat_failure_persists_user_and_synthetic_assistant(monkeypatch):
    """A failed loop still counts the turn and keeps history roles alternating."""
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)

    async def fake_parse(_message):
        return [], None

    async def failing_run_agent(**_kw):
        raise CoachProviderError("The coach is having trouble connecting. Try again in a moment.")

    monkeypatch.setattr(srv, "run_agent", failing_run_agent)
    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    http_response = await srv.api_chat(chat_request({"message": "hi"}))
    assert http_response.status_code == 502
    assert [(row[0], row[1]) for row in fake.inserted] == [
        ("user", "hi"),
        ("assistant", "Sorry, I couldn't reach the coach. Try again."),
    ]


async def test_parser_failure_falls_through_to_coach_with_raw_message(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def failing_parse(_message):
        raise MacroError("parser unavailable")

    async def fake_run_agent(**kwargs):
        seen.update(kwargs)
        return "Let's work on that.", []

    monkeypatch.setattr(srv, "parse_chat_message", failing_parse)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)

    http_response = await srv.api_chat(chat_request({"message": "  build me a gym plan  "}))
    assert http_response.status_code == 200
    assert seen["message"] == "build me a gym plan"
    assert [(row[0], row[1]) for row in fake.inserted] == [
        ("user", "build me a gym plan"),
        ("assistant", "Let's work on that."),
    ]


async def test_food_path_failure_persists_synthetic_assistant(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)

    async def fake_parse(_message):
        return [{"name": "eggs", "calories": 140, "protein": 12,
                 "carbs": 1, "fat": 10, "fiber": 0,
                 "meal": "Breakfast", "note": "USDA"}], None

    async def failing_write_meal(*_args, **_kwargs):
        raise MacroError("database unavailable")

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "write_meal", failing_write_meal)

    http_response = await srv.api_chat(chat_request({"message": "log eggs"}))
    assert http_response.status_code == 400
    assert [(row[0], row[1]) for row in fake.inserted] == [
        ("user", "log eggs"),
        ("assistant", "Sorry, I couldn't log that. Try again."),
    ]


async def test_food_chat_uses_light_parser_without_anthropic(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)

    async def fake_parse(message):
        assert message == "log 2 eggs"
        return [{"name": "2 eggs", "calories": 144, "protein": 12.6,
                 "carbs": 0.7, "fat": 9.5, "fiber": 0,
                 "meal": "Breakfast", "note": "USDA"}], None

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0):
        assert (name, macro_source, meal, allow_estimate) == (
            "2 eggs", "USDA", "Breakfast", True
        )
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                            "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        assert ensure and ensure[0]["name"] == "2 eggs"
        return {"totals": {"calories": 144, "protein": 12.6, "carbs": 0.7,
                            "fat": 9.5, "fiber": 0},
                "targets": {"calories": 2000}}

    async def unexpected_anthropic(**_kw):
        pytest.fail("food logging must not call the Anthropic coach")

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)
    monkeypatch.setattr(srv, "run_agent", unexpected_anthropic)

    http_response = await srv.api_chat(chat_request({"message": "log 2 eggs"}))
    payload = json.loads(http_response.body)
    assert http_response.status_code == 200
    assert set(payload) == {"reply", "logged", "totals", "widget"}
    assert payload["widget"] is None
    assert payload["logged"][0]["name"] == "2 eggs"
    assert payload["totals"]["calories"] == 144
    assert [row[0] for row in fake.inserted] == ["user", "assistant"]


def test_chat_quota_window_rolls_at_4am_not_midnight():
    tz = domain.LOCAL_TZ
    start, end = domain.effective_day_window(datetime(2026, 8, 19, 2, 30, tzinfo=tz))
    assert start == datetime(2026, 8, 18, 4, 0, tzinfo=tz)  # 2:30am still belongs to Aug 18
    assert end == datetime(2026, 8, 19, 4, 0, tzinfo=tz)
    start, end = domain.effective_day_window(datetime(2026, 8, 19, 12, 0, tzinfo=tz))
    assert start == datetime(2026, 8, 19, 4, 0, tzinfo=tz)
    assert end == datetime(2026, 8, 20, 4, 0, tzinfo=tz)


async def test_store_fetch_chat_messages_since_excludes_prior_day():
    day_start = datetime(2026, 8, 19, 4, 0, tzinfo=domain.LOCAL_TZ)

    class HistoryPool:
        def __init__(self):
            self.sql = ""
            self.args = ()

        async def fetch(self, sql, *args):
            self.sql = sql
            self.args = args
            rows = [
                {"id": "old", "role": "user", "content": "yesterday", "tool_calls": None,
                 "created_at": day_start - timedelta(seconds=1)},
                {"id": "new", "role": "assistant", "content": "today", "tool_calls": None,
                 "created_at": day_start + timedelta(seconds=1)},
            ]
            return [row for row in rows if row["created_at"] >= args[1]][:args[2]]

    pool = HistoryPool()
    store = Store("postgresql://unused/unused")
    store.pool = pool
    user_id = uuid4()
    token = bind_user(user_id)
    try:
        rows = await store.fetch_chat_messages_since(day_start)
    finally:
        reset_user(token)

    assert [row["content"] for row in rows] == ["today"]
    assert "WHERE user_id=$1 AND created_at >= $2" in pool.sql
    assert "ORDER BY created_at DESC, id DESC LIMIT" in pool.sql
    assert pool.args == (user_id, day_start, 20)


async def test_coach_save_preset_requires_a_real_macro_source(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    handlers = srv._coach_tool_handlers()
    values = {"name": "Shake", "calories": 150, "protein": 30, "carbs": 3, "fat": 2.5, "fiber": 0}

    with pytest.raises(MacroError):
        await handlers["save_preset"]({"values": dict(values)})  # missing source
    with pytest.raises(MacroError):
        await handlers["save_preset"]({"values": dict(values), "macro_source": "estimate"})
    assert fake.saved_presets == []

    await handlers["save_preset"]({"values": dict(values), "macro_source": "Fairlife Core Power label"})
    assert len(fake.saved_presets) == 1
    saved_values, existing_id = fake.saved_presets[0]
    assert saved_values["macro_source"] == "Fairlife Core Power label"
    assert existing_id is None


async def test_set_display_name_validates_and_updates_current_store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    handler = srv._coach_tool_handlers()["set_display_name"]

    assert await handler({"name": "  Ana  "}) == {"display_name": "Ana"}
    with pytest.raises(MacroError, match="required"):
        await handler({"name": "   "})
    with pytest.raises(MacroError, match="40"):
        await handler({"name": "x" * 41})
    assert fake.display_names == ["Ana"]


async def test_set_metrics_validates_and_stores_sane_values(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    handler = srv._coach_tool_handlers()["set_metrics"]
    values = {"height_cm": 178, "weight_kg": 80, "goal_weight_kg": 75,
              "age": 32, "activity_level": "moderate"}

    result = await handler(values)
    assert result["metrics"]["height_cm"] == 178.0
    assert fake.metrics == [result["metrics"]]
    for field, bad_value in (("height_cm", 99), ("weight_kg", 301),
                             ("goal_weight_kg", 900)):
        bad = {**values, field: bad_value}
        with pytest.raises(MacroError, match=field):
            await handler(bad)
    assert len(fake.metrics) == 1


async def test_widget_is_emitted_when_metrics_form_tool_was_called(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)

    async def fake_parse(_message): return [], None
    async def fake_run_agent(**_kwargs):
        return "Add your measurements here.", [
            {"tool": "request_metrics_form", "input": {}, "ok": True}
        ]
    async def fake_day_payload(_day): return {"totals": {"calories": 0}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)
    response = await srv.api_chat(chat_request({"message": "Okay"}))
    payload = json.loads(response.body)
    assert payload["widget"] == {
        "type": "metrics_form",
        "fields": ["height_cm", "weight_kg", "goal_weight_kg", "age", "activity_level"],
    }


async def test_structured_metrics_are_stored_once_and_routed_to_coach(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def unexpected_parse(_message): pytest.fail("structured metrics must bypass food parsing")
    async def fake_run_agent(**kwargs):
        seen.update(kwargs)
        return "Got it. How many days can you train?", []
    async def fake_day_payload(_day): return {"totals": {"calories": 0}}

    monkeypatch.setattr(srv, "parse_chat_message", unexpected_parse)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)
    response = await srv.api_chat(chat_request({
        "message": "Here are my measurements.",
        "metrics": {"height_cm": 178, "weight_kg": 80, "goal_weight_kg": 75,
                    "age": 32, "activity_level": "moderate"},
    }))
    assert response.status_code == 200
    assert fake.metrics == [{"height_cm": 178.0, "weight_kg": 80.0,
                             "goal_weight_kg": 75.0, "age": 32,
                             "activity_level": "moderate"}]
    assert seen["message"] == (
        "Here are my measurements.\n\nMetrics stored this turn: height_cm=178.0, "
        "weight_kg=80.0, goal_weight_kg=75.0, age=32, "
        "activity_level=moderate. Do not call set_metrics."
    )
    assert fake.inserted[0] == ("user", seen["message"], None)
    assert "set_metrics" not in seen["handlers"]


async def test_get_metrics_returns_stored_values_on_later_turn(monkeypatch):
    fake = FakeStore()
    fake.metrics.append({"height_cm": 178.0, "weight_kg": 80.0,
                         "goal_weight_kg": 75.0, "age": 32,
                         "activity_level": "moderate"})
    monkeypatch.setattr(srv, "_client", fake)

    result = await srv._coach_tool_handlers()["get_metrics"]({})

    assert result == {"metrics": fake.metrics[0]}

    fake.metrics.clear()
    assert await srv._coach_tool_handlers()["get_metrics"]({}) == {"metrics": {}}


async def test_chat_text_metrics_fallback_still_saves(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def fake_parse(_message): return [], None
    async def fake_run_agent(**kwargs):
        seen.update(kwargs)
        await kwargs["handlers"]["set_metrics"]({
            "height_cm": 178, "weight_kg": 80, "goal_weight_kg": 75,
            "age": 32, "activity_level": "moderate",
        })
        return "Saved.", []
    async def fake_day_payload(_day): return {"totals": {"calories": 0}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)
    response = await srv.api_chat(chat_request({
        "message": "I'm 178 cm, 80 kg, aiming for 75 kg, age 32, moderately active."
    }))

    assert response.status_code == 200
    assert "set_metrics" in seen["handlers"]
    assert fake.metrics[-1]["age"] == 32
    assert fake.metrics[-1]["activity_level"] == "moderate"


def test_onboarding_tools_are_declared_with_required_fields():
    tools = {tool["name"]: tool for tool in TOOLS}
    assert tools["set_display_name"]["input_schema"]["required"] == ["name"]
    assert tools["set_metrics"]["input_schema"]["required"] == [
        "height_cm", "weight_kg", "goal_weight_kg"
    ]
    assert tools["get_metrics"]["input_schema"]["required"] == []
    assert tools["request_metrics_form"]["input_schema"]["required"] == []


class ProfilePool:
    def __init__(self):
        self.calls = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return args[0]

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if sql.startswith("SELECT"):
            return {"height_cm": 178, "weight_kg": 80, "goal_weight_kg": 75,
                    "age": None, "activity_level": None,
                    "updated_at": datetime(2026, 8, 19, 12, 0)}
        return {"height_cm": args[1], "weight_kg": args[2],
                "goal_weight_kg": args[3], "age": args[4],
                "activity_level": args[5],
                "updated_at": datetime(2026, 8, 19, 12, 0)}


async def test_profile_store_writes_and_reads_only_bound_user():
    pool = ProfilePool()
    store = Store("postgresql://unused/unused")
    store.pool = pool
    user_id = uuid4()
    token = bind_user(user_id)
    try:
        assert await store.put_display_name("Ana") == {"display_name": "Ana"}
        await store.put_metrics({"height_cm": 178, "weight_kg": 80,
                                 "goal_weight_kg": 75})
        metrics = await store.get_metrics()
    finally:
        reset_user(token)

    assert pool.calls[0][1] == ("Ana", user_id)
    assert pool.calls[1][1][0] == user_id
    assert pool.calls[2][1] == (user_id,)
    assert metrics["height_cm"] == 178
    assert all("user_id" in sql or "WHERE id=$2" in sql for sql, _args in pool.calls)


async def test_store_rejects_unsourced_presets():
    """The store is the provenance choke point: no path may save a preset
    without a real macro_source, including MCP/Poke."""
    store = Store("postgresql://unused/unused")
    values = {"name": "Shake", "emoji": "🥤", "calories": 150, "protein": 30,
              "carbs": 3, "fat": 2.5, "fiber": 0, "meal": "Snack"}

    with pytest.raises(MacroError):
        await store.save_preset(dict(values))  # no source at all
    with pytest.raises(MacroError):
        await store.save_preset({**values, "macro_source": "  "})
    with pytest.raises(MacroError):
        await store.save_preset({**values, "macro_source": "estimate"})
    assert store.pool is None  # rejected before any database work


class RecordingPool:
    """Fake asyncpg pool that records the SQL issued inside the transaction."""

    def __init__(self, today_count):
        self.today_count = today_count
        self.statements = []

    def acquire(self):
        return self._Acquire(self)

    class _Acquire:
        def __init__(self, pool): self.pool = pool
        async def __aenter__(self): return RecordingPool._Conn(self.pool)
        async def __aexit__(self, *exc): return False

    class _Conn:
        def __init__(self, pool): self.pool = pool

        def transaction(self): return RecordingPool._Tx(self.pool)

        async def fetchval(self, sql, *args):
            self.pool.statements.append(sql)
            return self.pool.today_count if "count(*)" in sql else None

        async def fetchrow(self, sql, *args):
            self.pool.statements.append(sql)
            return {"id": "m1", "role": "user", "content": args[1],
                    "tool_calls": None, "created_at": datetime(2026, 8, 19, 12, 0)}

    class _Tx:
        def __init__(self, pool): self.pool = pool
        async def __aenter__(self): self.pool.statements.append("BEGIN")
        async def __aexit__(self, exc_type, *rest):
            self.pool.statements.append("ROLLBACK" if exc_type else "COMMIT")


async def test_chat_cap_takes_advisory_lock_before_count_and_rolls_back_at_cap():
    """Concurrent turns must serialize: the per-user advisory lock is acquired
    inside the transaction before the count, and a capped turn inserts nothing."""
    pool = RecordingPool(today_count=50)
    store = Store("postgresql://unused/unused")
    store.pool = pool
    token = bind_user(uuid4())
    try:
        with pytest.raises(ChatQuotaExceeded):
            await store.insert_user_chat_message("hi", daily_cap=50)
    finally:
        reset_user(token)

    lock = next(i for i, s in enumerate(pool.statements) if "pg_advisory_xact_lock" in s)
    count = next(i for i, s in enumerate(pool.statements) if "count(*)" in s)
    assert pool.statements[0] == "BEGIN" and lock < count
    assert not any("INSERT" in s for s in pool.statements)
    assert pool.statements[-1] == "ROLLBACK"


async def test_chat_cap_insert_lands_in_locked_transaction_under_cap():
    pool = RecordingPool(today_count=49)
    store = Store("postgresql://unused/unused")
    store.pool = pool
    token = bind_user(uuid4())
    try:
        row = await store.insert_user_chat_message("hi", daily_cap=50)
    finally:
        reset_user(token)

    assert row["content"] == "hi" and row["role"] == "user"
    lock = next(i for i, s in enumerate(pool.statements) if "pg_advisory_xact_lock" in s)
    insert = next(i for i, s in enumerate(pool.statements) if "INSERT INTO chat_messages" in s)
    assert pool.statements[0] == "BEGIN" and lock < insert
    assert pool.statements[-1] == "COMMIT"
