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
import food_lookup  # noqa: E402
from auth import bind_user, current_user_id, reset_user  # noqa: E402
from coach import CoachProviderError, SYSTEM_PROMPT, TOOLS, run_agent, system_prompt  # noqa: E402
from domain import MacroError  # noqa: E402
from store import ChatQuotaExceeded, Store  # noqa: E402
import server as srv  # noqa: E402


def response(message, status=200, **extra):
    return httpx.Response(
        status, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        json={"choices": [{"message": message}], **extra},
    )


def tool_call(call_id, name, arguments=None):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments or {})}}


def tool_response(*calls):
    return response({"role": "assistant", "content": None, "tool_calls": list(calls)})


def text_response(content, **extra):
    return response({"role": "assistant", "content": content}, **extra)


ONBOARDING_METRICS = {
    "height_cm": 178, "weight_kg": 80, "goal_weight_kg": 75,
    "age": 32, "activity_level": "moderate",
}
ONBOARDING_TARGETS = {
    "calories": 2200, "protein": 160, "carbs": 230, "fat": 70, "fiber": 30,
}
ONBOARDING_PLAN = {"days": {"Day 1": {"exercises": [
    {"name": "Bench Press", "sets": 3, "reps": "8-10"}
]}}}


def onboarding_handlers(seen):
    async def handler(name, arguments):
        seen.append((name, arguments))
        if name == "get_metrics":
            return {"metrics": ONBOARDING_METRICS}
        if name == "get_library":
            return {"exercises": [{"name": "Bench Press"}]}
        return {"ok": True}

    return {
        name: (lambda arguments, name=name: handler(name, arguments))
        for name in ("set_display_name", "set_metrics", "get_metrics",
                     "request_metrics_form", "get_library", "set_targets",
                     "set_workout_plan")
    }


async def test_tool_dispatch_keeps_authenticated_user_scope(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    wanted_user = uuid4()
    seen = []
    calls = 0

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert payload["model"] == "gpt-5.6-luna"
            assert payload["max_completion_tokens"] == 1200
            assert payload["messages"][0]["role"] == "system"
            assert "system" not in payload
            assert payload["tools"][0]["function"]["parameters"]["type"] == "object"
            return tool_response(tool_call("t1", "get_today"))
        assert payload["messages"][-1]["role"] == "tool"
        assert payload["messages"][-1]["tool_call_id"] == "t1"
        return text_response("You're all set.")

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
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    provider_calls = tool_calls = 0

    async def fake_post(_token, _payload):
        nonlocal provider_calls
        provider_calls += 1
        return tool_response(tool_call(f"t{provider_calls}", "get_today"))

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
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    executed = 0

    async def fake_post(_token, _payload):
        return tool_response(*(tool_call(f"t{i}", "get_today") for i in range(6)))

    async def get_today(_args):
        nonlocal executed
        executed += 1
        return {}

    with pytest.raises(CoachProviderError, match="tool limit"):
        await run_agent(history=[], message="loop", onboarding=False,
                        handlers={"get_today": get_today}, post=fake_post)
    assert executed == 16  # 6 + 6 + 4, then the 17th execution is refused


async def test_library_audit_is_compact_but_model_receives_full_result(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    exercises = [{
        "name": f"Exercise {index}", "muscle_group": ["Legs"],
        "workout_type": "Strength", "equipment": "barbell",
        "video_url": f"https://media.example/{index}.gif",
        "instructions": "A large instruction payload", "overview": "A large overview",
    } for index in range(15)]
    calls = 0

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_response(tool_call("library", "get_library", {"query": "missing"}))
        tool_result = json.loads(payload["messages"][-1]["content"])["result"]
        assert tool_result["exercises"][0]["instructions"] == "A large instruction payload"
        assert len(tool_result["exercises"]) == 15
        return text_response("Try Exercise 13.")

    async def get_library(_args):
        return {"exercises": exercises, "note": "full library returned"}

    _reply, audit = await run_agent(
        history=[], message="Find something", onboarding=False,
        handlers={"get_library": get_library}, post=fake_post,
    )

    persisted = audit[0]["result"]["exercises"]
    assert len(persisted) == 15
    assert set(persisted[0]) == {
        "name", "instructions", "video_url", "muscle_group", "equipment",
        "workout_type",
    }
    assert persisted[0]["instructions"] == "A large instruction payload"
    assert "overview" not in json.dumps(audit)
    widget = srv.exercise_card_widget("Try Exercise 13.", audit)
    assert widget["exercise"]["exercise_name"] == "Exercise 13"
    assert widget["exercise"]["video_url"] == "https://media.example/13.gif"


async def test_same_role_history_is_merged_before_the_api_call(monkeypatch):
    """Orphaned user rows (failed prior turns) must not produce consecutive user roles."""
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    captured = {}

    async def fake_post(_token, payload):
        captured["messages"] = payload["messages"]
        return text_response("Merged fine.")

    history = [
        {"role": "assistant", "content": "orphaned lead reply"},
        {"role": "user", "content": "first orphan"},
        {"role": "user", "content": "second orphan"},
    ]
    reply, _ = await run_agent(history=history, message="new question", onboarding=False,
                               handlers={}, post=fake_post)
    assert reply == "Merged fine."
    assert [m["role"] for m in captured["messages"]] == ["system", "user"]
    merged = captured["messages"][1]["content"]
    assert "first orphan" in merged and "second orphan" in merged and "new question" in merged


async def test_full_onboarding_completes_plan_and_targets_immediately_after_metrics(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    history = []
    saved_metrics = {}
    writes = []

    async def handle(name, arguments):
        if name == "get_metrics":
            return {"metrics": saved_metrics}
        if name == "set_metrics":
            saved_metrics.update(arguments)
        if name in {"set_targets", "set_workout_plan"}:
            writes.append(name)
        if name == "get_library":
            return {"exercises": [{"name": "Bench Press"}]}
        return {"ok": True}

    handlers = {
        name: (lambda arguments, name=name: handle(name, arguments))
        for name in ("set_display_name", "set_metrics", "get_metrics",
                     "request_metrics_form", "get_library", "set_targets",
                     "set_workout_plan")
    }

    async def fake_post(_token, payload):
        system = payload["messages"][0]["content"]
        assert "HARD ONBOARDING COMPLETION RULE" in system
        assert "Conversation history is onboarding state" in system
        messages = payload["messages"][1:]
        user_text = next(message["content"] for message in reversed(messages)
                         if message["role"] == "user")
        tools_since_user = []
        for message in reversed(messages):
            if message["role"] == "user":
                break
            if message["role"] == "assistant":
                tools_since_user.extend(
                    call["function"]["name"] for call in message.get("tool_calls", [])
                )
        tools_since_user.reverse()

        if user_text == "Alex":
            if "set_display_name" not in tools_since_user:
                return tool_response(tool_call("name", "set_display_name", {"name": "Alex"}))
            return text_response("What is your main goal?")
        if user_text == "Build muscle":
            return text_response("What is your experience level?")
        if user_text == "Intermediate":
            return text_response("How many days can you train, and what equipment do you have?")
        if user_text == "3 days in a full gym":
            if "get_metrics" not in tools_since_user:
                return tool_response(tool_call("metrics-check", "get_metrics"))
            if "request_metrics_form" not in tools_since_user:
                return tool_response(tool_call("metrics-form", "request_metrics_form"))
            return text_response("Add your measurements here.")
        if user_text.startswith("178 cm"):
            if "set_metrics" not in tools_since_user:
                return tool_response(tool_call("metrics-save", "set_metrics", ONBOARDING_METRICS))
            if "get_library" not in tools_since_user:
                return tool_response(tool_call("library", "get_library", {"query": "full gym"}))
            if "set_targets" not in tools_since_user:
                return tool_response(
                    tool_call("targets", "set_targets", {"values": ONBOARDING_TARGETS}),
                    tool_call("plan", "set_workout_plan", {"plan": ONBOARDING_PLAN}),
                )
            return text_response("Your targets and plan are ready.")
        pytest.fail(f"Unexpected onboarding state: {user_text!r}")

    user_turns = [
        "Alex", "Build muscle", "Intermediate", "3 days in a full gym",
        "178 cm, 80 kg, goal 75 kg, age 32, moderately active",
    ]
    final_audit = []
    for message in user_turns:
        reply, final_audit = await run_agent(
            history=history, message=message, onboarding=True,
            handlers=handlers, post=fake_post,
        )
        history.extend((
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply},
        ))

    assert writes == ["set_targets", "set_workout_plan"]
    assert [entry["tool"] for entry in final_audit] == [
        "set_metrics", "get_library", "set_targets", "set_workout_plan",
    ]


async def test_all_onboarding_details_in_one_message_builds_without_questions(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    seen = []
    handlers = onboarding_handlers(seen)

    async def fake_post(_token, payload):
        messages = payload["messages"]
        called = [call["function"]["name"] for message in messages
                  for call in message.get("tool_calls", [])]
        if not called:
            return tool_response(
                tool_call("name", "set_display_name", {"name": "Alex"}),
                tool_call("metrics", "set_metrics", ONBOARDING_METRICS),
            )
        if "get_library" not in called:
            return tool_response(tool_call("library", "get_library", {"query": "full gym"}))
        if "set_targets" not in called:
            return tool_response(
                tool_call("targets", "set_targets", {"values": ONBOARDING_TARGETS}),
                tool_call("plan", "set_workout_plan", {"plan": ONBOARDING_PLAN}),
            )
        return text_response("Your targets and plan are ready.")

    message = ("I'm Alex. I want to build muscle, I'm intermediate, and I can train "
               "3 days a week in a full gym. I'm 178 cm, 80 kg, aiming for 75 kg, "
               "age 32, and moderately active.")
    reply, audit = await run_agent(
        history=[], message=message, onboarding=True,
        handlers=handlers, post=fake_post,
    )

    assert "?" not in reply
    assert [entry["tool"] for entry in audit] == [
        "set_display_name", "set_metrics", "get_library",
        "set_targets", "set_workout_plan",
    ]


async def test_tool_error_is_returned_to_model(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    calls = 0

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_response(tool_call("bad", "log_meal"))
        result = json.loads(payload["messages"][-1]["content"])
        assert result["is_error"] is True
        assert "macro_source is required" in result["result"]["error"]
        return text_response("I need a real macro source first.")

    async def log_meal(_args):
        raise ValueError("macro_source is required")

    reply, audit = await run_agent(history=[], message="log lunch", onboarding=False,
                                   handlers={"log_meal": log_meal}, post=fake_post)
    assert reply.startswith("I need")
    assert audit[0]["ok"] is False


async def test_transient_overload_is_retried_once(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    calls = 0

    async def fake_post(_token, _payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response({}, status=529)
        return text_response("Back online.")

    reply, _ = await run_agent(history=[], message="hi", onboarding=False,
                               handlers={}, post=fake_post)
    assert reply == "Back online."
    assert calls == 2


async def test_second_provider_failure_becomes_friendly_error(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    calls = 0

    async def fake_post(_token, _payload):
        nonlocal calls
        calls += 1
        return response({}, status=529)

    with pytest.raises(CoachProviderError, match="trouble connecting"):
        await run_agent(history=[], message="hi", onboarding=False,
                        handlers={}, post=fake_post)
    assert calls == 2  # one retry, then give up — no retry storms


async def test_usage_is_recorded_per_api_call_and_never_fatal(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    calls = 0
    rows = []

    async def fake_post(_token, _payload):
        nonlocal calls
        calls += 1
        usage = {"prompt_tokens": 100 * calls, "completion_tokens": calls}
        if calls == 1:
            result = tool_response(tool_call("t1", "get_today"))
        else:
            result = text_response("Done.")
        data = json.loads(result.content)
        data.update(model="gpt-5.6-luna", usage=usage)
        return httpx.Response(200, request=result.request, json=data)

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
        {"model": "gpt-5.6-luna", "input_tokens": 100, "output_tokens": 1},
        {"model": "gpt-5.6-luna", "input_tokens": 200, "output_tokens": 2},
    ]


class FakeStore:
    def __init__(self, today_count=0, plan=None, has_targets=False, chat_messages=None,
                 library=None, display_name=None):
        self.today_count = today_count
        self.plan = plan
        self.targets = has_targets
        self.inserted = []
        self.usage = []
        self.saved_presets = []
        self.display_names = []
        self.display_name = display_name
        self.metrics = []
        self.chat_messages = chat_messages or []
        self.history_day_start = None
        self.library = library or []
        self.workouts = []
        self.session_state = None

    async def insert_user_chat_message(self, content, daily_cap):
        if self.today_count >= daily_cap:
            raise ChatQuotaExceeded(f"daily chat cap of {daily_cap} reached")
        self.today_count += 1
        self.inserted.append(("user", content, None))
        return {}

    async def fetch_presets(self, active_only=True):
        return []

    async def insert_meal(self, **kwargs):
        if not hasattr(self, "meals"):
            self.meals = []
        self.meals.append(dict(kwargs))
        return {"id": "m1", "created_time": "", **kwargs}

    async def save_preset(self, values, existing_id=None):
        self.saved_presets.append((dict(values), existing_id))
        return {"id": existing_id or "new", **values}

    async def put_display_name(self, name):
        self.display_names.append(name)
        self.display_name = name
        return {"display_name": name}

    async def get_display_name(self):
        return self.display_name

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

    async def put_workout_plan(self, plan):
        self.plan = plan
        return plan

    async def fetch_workout_library(self):
        return self.library

    async def fetch_workouts(self, start=None, end=None, exercise=None):
        return list(self.workouts)

    async def fetch_prs(self):
        return []

    async def fetch_session_day_state(self):
        return self.session_state

    async def put_session_day_state(self, rotation_index, done_date):
        self.session_state = {"rotation_index": rotation_index, "done_date": done_date}
        return dict(self.session_state)

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
        await record_usage({"model": "gpt-5.6-luna", "input_tokens": 10, "output_tokens": 5})
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
    assert len(seen["tools"]) == 23
    assert [row[0] for row in fake.inserted] == ["user", "assistant"]
    assert fake.usage == [("gpt-5.6-luna", 10, 5)]


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


async def test_new_day_chat_injects_stored_name_for_onboarded_user(monkeypatch):
    fake = FakeStore(plan={"version": 1}, has_targets=True, display_name="Ana")
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def fake_parse(_message):
        return [], None

    async def fake_run_agent(**kwargs):
        seen.update(kwargs)
        return "Good to see you, Ana.", []

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)

    response = await srv.api_chat(chat_request({"message": "test"}))

    assert response.status_code == 200
    assert seen["history"] == []
    assert seen["onboarding"] is False
    assert seen["display_name"] == "Ana"
    prompt = system_prompt(seen["onboarding"], seen["display_name"])
    assert "Current user: Ana (already onboarded)" in prompt
    assert "NEVER ask for their name again" in prompt


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


async def test_every_message_routes_to_coach_without_parser(monkeypatch):
    """Split models (Matt's call): NON-food messages reach the coach; the
    parser fast-path only handles food and is not invoked for coaching."""
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def parse_returns_empty(_message):
        seen["parsed"] = True
        return ([], None, [])

    async def fake_run_agent(**kwargs):
        seen.update(kwargs)
        return "Let's work on that.", []

    monkeypatch.setattr(srv, "parse_chat_message", parse_returns_empty)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)

    http_response = await srv.api_chat(chat_request({"message": "  build me a gym plan  "}))
    assert http_response.status_code == 200
    assert seen.get("parsed") is True
    assert seen["message"] == "build me a gym plan"
    assert [(row[0], row[1]) for row in fake.inserted] == [
        ("user", "build me a gym plan"),
        ("assistant", "Let's work on that."),
    ]


async def test_coach_error_keeps_friendly_fallback_without_parser(monkeypatch):
    """A coach failure still yields the friendly 502 reply; the parser path
    is for food only and never used as a coach fallback."""
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)

    async def fake_parse(_message):
        return ([], None, [])

    async def failing_run_agent(**_kw):
        raise CoachProviderError("The coach is having trouble connecting. Try again in a moment.")

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", failing_run_agent)

    http_response = await srv.api_chat(chat_request({"message": "log 2 eggs"}))
    assert http_response.status_code == 502
    assert [(row[0], row[1]) for row in fake.inserted] == [
        ("user", "log 2 eggs"),
        ("assistant", "Sorry, I couldn't reach the coach. Try again."),
    ]


async def test_food_message_goes_through_parser_not_coach(monkeypatch):
    """Split models (Matt's call): a food message goes to the gpt-4o-mini
    parser fast-path; the coach loop must not run for food."""
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def fake_parse(_message):
        seen["parsed"] = True
        return ([{"name": "eggs", "calories": 140, "protein": 12, "carbs": 1, "fat": 10, "fiber": 0,
                   "quantity": 2, "grams": None, "basis": "per_unit", "sourced_from": "lookup",
                   "meal": "Breakfast", "note": "OpenFoodFacts: egg-cooked"}], None, [])

    async def unexpected_run_agent(*args, **kw):
        pytest.fail("food logging must go through the parser fast-path, never the coach loop")

    async def fake_day_payload(_day, **kw):
        return {"totals": {"calories": 140}, "targets": {"calories": 2000}}

    async def fake_write_meal(*args, **kw):
        return {"logged": {"name": args[0], "calories": 140}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", unexpected_run_agent)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)

    http_response = await srv.api_chat(chat_request({"message": "log 2 eggs and toast"}))
    assert http_response.status_code == 200
    assert seen.get("parsed") is True


def _fast_path_env(monkeypatch, items):
    """Wire the food fast-path with a canned parser result; return the writes."""
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    written = []

    async def fake_parse(_message):
        return (items, None, [])

    async def unexpected_run_agent(*_args, **_kw):
        pytest.fail("food logging must go through the parser fast-path, never the coach loop")

    async def fake_day_payload(_day, **_kw):
        return {"totals": {"calories": 0}, "targets": {"calories": 2000}}

    async def fake_write_meal(name, calories, protein, carbs, fat, source,
                              meal, _day, allow_estimate=False, fiber=0):
        written.append({"name": name, "calories": calories, "protein": protein,
                        "carbs": carbs, "fat": fat, "fiber": fiber,
                        "source": source, "meal": meal})
        return {"logged": {"name": name, "calories": calories}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", unexpected_run_agent)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    return written


EGG_PER_100G = {"calories": 143, "protein": 12.6, "carbs": 0.7, "fat": 9.5, "fiber": 0}
APPLE_PER_100G = {"calories": 52, "protein": 0.3, "carbs": 13.8, "fat": 0.2, "fiber": 2.4}


async def test_per_serving_without_per_100g_logs_parser_finals_as_is(monkeypatch):
    """A complete serving with no per-100g panel (and no sanity match) passes
    through unchanged; a user-stated weight is never clamped."""
    written = _fast_path_env(monkeypatch, [
        {"name": "mystery stew", "macros_per_100g": None,
         "grams": None, "quantity": None, "basis": "per_serving",
         "calories": 300, "protein": 12, "carbs": 35, "fat": 12, "fiber": 4,
         "sourced_from": "estimate", "meal": "Dinner", "note": "ESTIMATE"},
        # 500 g of apple slices at a user-stated weight keeps its real size.
        {"name": "apple slices", "macros_per_100g": APPLE_PER_100G,
         "grams": 500, "quantity": None, "basis": "per_100g",
         "calories": 260, "protein": 1.5, "carbs": 69, "fat": 1, "fiber": 12,
         "sourced_from": "lookup", "meal": "Snack", "note": "OpenFoodFacts: 171688"},
    ])

    response = await srv.api_chat(chat_request({"message": "stew and 500g apple slices"}))

    assert response.status_code == 200
    assert written[0]["calories"] == 300
    assert written[0]["source"] == "ESTIMATE"
    assert written[1]["calories"] == 260.0  # 52 × 5 — stated weight wins
    assert written[1]["source"] == "OpenFoodFacts: 171688"


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


async def test_store_connect_applies_schema_before_first_query(monkeypatch):
    """Deploys run no migration step: connect() must apply schema.sql so a
    table added by a release (coach_usage, session_day_state) exists on live
    before the first query can 500 with UndefinedTableError."""
    executed = []

    class SchemaPool:
        async def execute(self, sql, *args):
            executed.append(sql)

    async def fake_create_pool(url, min_size, max_size):
        return SchemaPool()

    monkeypatch.setattr("store.asyncpg.create_pool", fake_create_pool)
    store = Store("postgresql://unused/unused")

    pool = await store.connect()
    assert "CREATE TABLE IF NOT EXISTS session_day_state" in executed[0]
    assert await store.connect() is pool  # pool caches; schema applies once
    assert len(executed) == 1


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


FOOD_HIT = {
    "name": "Egg, whole, cooked",
    "macros_per_100g": {"calories": 155, "protein": 13, "carbs": 1.1,
                        "fat": 11, "fiber": 0},
    "source": "OpenFoodFacts: 171705",
}


async def test_coach_logs_food_through_lookup_and_meal_tools(monkeypatch):
    """The coach is the only agent: when it decides a message is food it runs
    lookup_food and then log_meal with the looked-up macro_source."""
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    calls = 0
    written = {}

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_response(tool_call("lookup-1", "lookup_food", {"query": "2 eggs"}))
        if calls == 2:
            return tool_response(tool_call("log-1", "log_meal", {
                "name": "2 eggs", "meal_type": "Breakfast",
                "calories": 144, "protein": 12.6, "carbs": 0.7, "fat": 9.5,
                "fiber": 0, "macro_source": "OpenFoodFacts: 171705",
            }))
        return text_response("Logged 2 eggs, 144 kcal.")

    async def fake_resolve(query):
        assert query == "2 eggs"
        return FOOD_HIT

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0):
        written.update(name=name, macro_source=macro_source,
                       allow_estimate=allow_estimate, meal=meal)
        return {"logged": {"name": name, "calories": calories}}

    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)

    token = bind_user(uuid4())
    try:
        reply, audit = await run_agent(
            history=[], message="log 2 eggs and toast", onboarding=False,
            handlers=srv._coach_tool_handlers(), post=fake_post,
        )
    finally:
        reset_user(token)

    assert calls == 3
    assert [entry["tool"] for entry in audit] == ["lookup_food", "log_meal"]
    assert [entry["ok"] for entry in audit] == [True, True]
    assert audit[1]["result"] == {
        "logged": {"name": "2 eggs", "calories": 144}
    }
    assert reply == "Logged 2 eggs, 144 kcal."
    assert written == {"name": "2 eggs", "macro_source": "OpenFoodFacts: 171705",
                       "allow_estimate": False, "meal": "Breakfast"}


async def test_chat_returns_meals_logged_by_coach(monkeypatch):
    fake = FakeStore(plan={"version": 1}, has_targets=True)
    monkeypatch.setattr(srv, "_client", fake)
    logged_meal = {
        "id": "meal-1", "name": "2 eggs", "meal": "Breakfast",
        "calories": 144, "protein": 12.6, "carbs": 0.7, "fat": 9.5,
        "fiber": 0, "date": domain.effective_date().isoformat(),
        "created_time": "2026-08-23T12:00:00Z",
        "macro_source": "OpenFoodFacts: 171705",
    }

    async def fake_parse(_message):
        return ([], None, [])

    async def fake_run_agent(**_kwargs):
        return "Logged 2 eggs, 144 kcal.", [
            {"tool": "lookup_food", "input": {"query": "2 eggs"}, "ok": True},
            {"tool": "log_meal", "input": {}, "ok": True,
             "result": {"logged": logged_meal}},
        ]

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)

    response = await srv.api_chat(chat_request({"message": "log 2 eggs"}))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["reply"] == "Logged 2 eggs, 144 kcal."
    assert payload["logged"] == [logged_meal]
    assert payload["has_plan"] is True
    assert payload["has_targets"] is True
    assert payload["widget"] is None


async def test_log_meal_still_requires_real_macro_source(monkeypatch):
    """Food provenance survives the unification: log_meal rejects placeholder
    macro_sources exactly as before."""
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    handler = srv._coach_tool_handlers()["log_meal"]
    args = {"name": "2 eggs", "meal_type": "Breakfast", "calories": 144,
            "protein": 12.6, "carbs": 0.7, "fat": 9.5, "fiber": 0}

    with pytest.raises(MacroError):
        await handler({**args, "macro_source": "estimate"})
    with pytest.raises(MacroError):
        await handler({**args, "macro_source": " "})


def test_food_guidance_never_refuses_and_allows_flagged_estimates():
    """The coach must log every food message: verified macros when a source
    resolves, a clearly-flagged estimate when nothing does — never a label ask."""
    log_meal = next(tool for tool in TOOLS if tool["name"] == "log_meal")
    description = log_meal["description"]
    assert "Never refuse" in description
    assert "ESTIMATE" in description
    assert "Never estimate" not in description

    for prompt in (SYSTEM_PROMPT, system_prompt(onboarding=False)):
        normalized = " ".join(prompt.replace("`", "").split())
        assert "never ask for a label" in normalized
        assert "ESTIMATE" in normalized
        assert "Only ask for a label" not in normalized
        assert "Never estimate" not in normalized


def rotation_plan():
    return {
        "version": 1,
        "rotation": ["Push", "Pull", "Legs"],
        "days": {
            "Push": {"label": "Push Day", "exercises": [
                {"name": "Bench Press", "sets": 3, "reps": "8-10", "rest_sec": 90}]},
            "Pull": {"label": "Pull Day", "exercises": [
                {"name": "Lat Pulldown", "sets": 3, "reps": "10", "rest_sec": 90}]},
            "Legs": {"label": "Legs Day", "exercises": [
                {"name": "Squat", "sets": 3, "reps": "8", "rest_sec": 120}]},
        },
    }


async def test_get_today_session_returns_rotation_day_and_done_state(monkeypatch):
    fake = FakeStore(plan=rotation_plan())
    monkeypatch.setattr(srv, "_client", fake)
    handlers = srv._coach_tool_handlers()

    # No workout history and no day-state: the rotation starts at Push.
    session = await handlers["get_today_session"]({})
    assert session == {
        "today_type": "Push",
        "exercises": [{"name": "Bench Press", "sets": 3, "reps": "8-10", "rest_sec": 90}],
        "done": False,
        "has_plan": True,
        "session_size": None,
    }

    # A completion recorded today pins today's type and flips done.
    await handlers["complete_today_session"]({})
    session = await handlers["get_today_session"]({})
    assert session["today_type"] == "Push"
    assert session["done"] is True

    # A completion from yesterday advances the rotation to Pull.
    fake.session_state = {"rotation_index": 0,
                          "done_date": domain.effective_date() - timedelta(days=1)}
    session = await handlers["get_today_session"]({})
    assert session["today_type"] == "Pull"
    assert session["done"] is False


async def test_today_session_advances_past_last_logged_workout(monkeypatch):
    fake = FakeStore(plan=rotation_plan())
    fake.workouts = [{
        "id": "w1", "exercise": "Bench Press", "workout_type": ["Push"],
        "muscle_group": ["Chest"], "sets": [{"weight": 60, "reps": 8}],
        "date": (domain.effective_date() - timedelta(days=1)).isoformat(),
    }]
    monkeypatch.setattr(srv, "_client", fake)

    session = await srv._coach_tool_handlers()["get_today_session"]({})
    assert session["today_type"] == "Pull"  # after Push, Pull is next
    assert session["done"] is False


async def test_get_today_session_reads_legacy_list_shaped_days(monkeypatch):
    """Live rows written before the dict-days schema store days as a
    [{type, exercises}] list; today's session must still surface exercises."""
    plan = rotation_plan()
    plan["days"] = [
        {"type": "Push", "exercises": [{"name": "Bench Press"}]},
        {"type": "Pull", "exercises": [{"name": "Lat Pulldown"}]},
        {"type": "Legs", "exercises": [{"name": "Squat"}]},
    ]
    fake = FakeStore(plan=plan)
    monkeypatch.setattr(srv, "_client", fake)

    session = await srv._coach_tool_handlers()["get_today_session"]({})
    assert session["today_type"] == "Push"
    assert session["exercises"] == [{"name": "Bench Press"}]
    assert session["done"] is False


async def test_get_today_session_without_plan_is_onboarding_state(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    handlers = srv._coach_tool_handlers()

    session = await handlers["get_today_session"]({})
    assert session == {"today_type": None, "exercises": [], "done": False, "has_plan": False}

    with pytest.raises(MacroError, match="plan"):
        await handlers["complete_today_session"]({})


async def test_complete_today_session_marks_today_payload_done(monkeypatch):
    fake = FakeStore(plan=rotation_plan())
    monkeypatch.setattr(srv, "_client", fake)
    handlers = srv._coach_tool_handlers()

    result = await handlers["complete_today_session"]({})
    assert result["today_type"] == "Push"
    assert result["done"] is True
    assert result["already_done"] is False
    assert result["completed_on"] == domain.effective_date().isoformat()
    assert fake.session_state == {"rotation_index": 0, "done_date": domain.effective_date()}

    # Idempotent: completing twice reports already_done and stays pinned.
    again = await handlers["complete_today_session"]({})
    assert again["already_done"] is True
    assert again["done"] is True

    # The app's Today view reads the same per-user day-state.
    payload = await srv.workout_plan_payload()
    assert payload["upcoming"][0]["type"] == "Push"
    assert payload["upcoming"][0]["done"] is True

    stats = await srv.workout_stats_payload()
    assert stats["plan"]["today"]["done"] is True


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


async def test_onboarding_weight_question_emits_metrics_form_without_tool_call(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)

    async def fake_parse(_message): return [], None
    async def fake_run_agent(**_kwargs):
        return "What is your current weight?", []
    async def fake_day_payload(_day): return {"totals": {"calories": 0}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(chat_request({"message": "Ready."}))
    payload = json.loads(response.body)

    assert payload["widget"] == {
        "type": "metrics_form",
        "fields": ["height_cm", "weight_kg", "goal_weight_kg", "age", "activity_level"],
    }


async def test_metrics_fallback_does_not_repeat_form_when_metrics_are_saved(monkeypatch):
    fake = FakeStore()
    fake.metrics.append(ONBOARDING_METRICS)
    monkeypatch.setattr(srv, "_client", fake)

    async def fake_parse(_message): return [], None
    async def fake_run_agent(**_kwargs):
        return "Your measurements are already saved.", []

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)

    response = await srv.api_chat(chat_request({"message": "What next?"}))

    assert json.loads(response.body)["widget"] is None


def test_metrics_form_prompt_forbids_plain_text_measurement_questions():
    form_tool = next(tool for tool in TOOLS if tool["name"] == "request_metrics_form")
    assert "CALL THIS TOOL" in form_tool["description"]
    assert "instead of asking for weight" in form_tool["description"]
    for prompt in (SYSTEM_PROMPT, system_prompt(onboarding=True)):
        normalized = " ".join(prompt.replace("`", "").split())
        assert "request_metrics_form" in normalized
        assert "never ask for weight, height" in normalized.casefold()


async def test_library_tool_returns_enriched_exercise_objects(monkeypatch):
    exercise = {
        "name": "Bench Press", "muscle_group": ["Chest", "Triceps"],
        "workout_type": "Push", "equipment": "barbell",
        "difficulty": "intermediate", "swaps": [],
        "video_url": "https://media.example/bench.gif",
        "instructions": "Lower the bar.\nPress it up.",
    }
    monkeypatch.setattr(srv, "_client", FakeStore(library=[exercise]))

    result = await srv._coach_tool_handlers()["get_library"]({"query": "push"})

    assert result["exercises"][0]["video_url"] == exercise["video_url"]
    assert result["exercises"][0]["instructions"] == exercise["instructions"]


def workout_plan(exercise):
    return {
        "version": 1,
        "rotation": ["Legs"],
        "days_per_week": 1,
        "days": {"Legs": {"label": "Legs", "exercises": [{
            **exercise, "sets": 3, "reps": "8-10", "rest_sec": 90,
        }]}},
    }


async def test_set_workout_plan_canonicalizes_library_exercise_name(monkeypatch):
    fake = FakeStore(library=[
        {"id": "squat-1", "name": "Squat"},
        {"id": "back-squat-1", "name": "Back Squat"},
        {"id": "front-squat-1", "name": "Front Squat"},
    ])
    monkeypatch.setattr(srv, "_client", fake)

    result = await srv._coach_tool_handlers()["set_workout_plan"]({
        "plan": workout_plan({"library_id": "missing", "name": "Barbell Back Squat"})
    })

    assert result["plan"]["days"]["Legs"]["exercises"][0]["name"] == "Back Squat"
    assert fake.plan == result["plan"]

    result = await srv._coach_tool_handlers()["set_workout_plan"]({
        "plan": workout_plan({"id": "back-squat-1", "name": "Coach Squat Variation"})
    })
    # id path removed by design: name matching resolves to the longest contained library name
    assert result["plan"]["days"]["Legs"]["exercises"][0]["name"] == "Squat"


async def test_set_workout_plan_injects_server_owned_version(monkeypatch):
    fake = FakeStore(library=[{"id": "squat-1", "name": "Back Squat"}])
    monkeypatch.setattr(srv, "_client", fake)
    plan = workout_plan({"name": "Back Squat"})
    plan.pop("version")

    result = await srv._coach_tool_handlers()["set_workout_plan"]({"plan": plan})

    assert result["plan"]["version"] == domain.WORKOUT_PLAN_VERSION
    assert fake.plan == result["plan"]


async def test_chat_text_metrics_can_complete_onboarding_with_versionless_plan(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    fake = FakeStore(library=[{"id": "squat-1", "name": "Back Squat"}])
    monkeypatch.setattr(srv, "_client", fake)
    plan = workout_plan({"name": "Back Squat"})
    plan.pop("version")
    calls = 0

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_response(
                tool_call("metrics", "set_metrics", {
                    "height_cm": 180, "weight_kg": 85, "goal_weight_kg": 80,
                    "age": 22, "activity_level": "moderate",
                }),
                tool_call("plan", "set_workout_plan", {"plan": plan}),
            )
        tool_messages = payload["messages"][-2:]
        assert all(json.loads(item["content"])["is_error"] is False
                   for item in tool_messages)
        return text_response("Your metrics and plan are saved.")

    reply, audit = await run_agent(
        history=[],
        message="no injuries, height 180cm weight 85kg goal 80kg age 22 moderate",
        onboarding=True,
        handlers=srv._coach_tool_handlers(),
        post=fake_post,
    )

    assert reply == "Your metrics and plan are saved."
    assert [entry["ok"] for entry in audit] == [True, True]
    assert fake.metrics[-1]["age"] == 22
    assert fake.plan["version"] == domain.WORKOUT_PLAN_VERSION


async def test_set_workout_plan_exact_name_wins_over_substring_matches(monkeypatch):
    fake = FakeStore(library=[
        {"id": "squat-1", "name": "Squat"},
        {"id": "back-squat-1", "name": "Back Squat"},
    ])
    monkeypatch.setattr(srv, "_client", fake)

    result = await srv._coach_tool_handlers()["set_workout_plan"]({
        "plan": workout_plan({"name": "sQuAt"})
    })

    assert result["plan"]["days"]["Legs"]["exercises"][0]["name"] == "Squat"


async def test_set_workout_plan_validates_against_large_library(monkeypatch):
    library = [
        {"id": f"exercise-{index}", "name": f"Exercise {index}"}
        for index in range(900)
    ]
    library.append({"id": "db-bench", "name": "Dumbbell Bench Press"})
    fake = FakeStore(library=library)
    monkeypatch.setattr(srv, "_client", fake)

    result = await srv._coach_tool_handlers()["set_workout_plan"]({
        "plan": workout_plan({"name": "dumbbell bench press"})
    })

    assert result["plan"]["days"]["Legs"]["exercises"][0]["name"] == (
        "Dumbbell Bench Press"
    )


async def test_set_workout_plan_casefold_name_collision_is_tool_error(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    fake = FakeStore(library=[
        {"id": "squat-1", "name": "Squat"},
        {"id": "squat-2", "name": "SQUAT"},
    ])
    monkeypatch.setattr(srv, "_client", fake)
    calls = 0

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_response(tool_call(
                "ambiguous", "set_workout_plan",
                {"plan": workout_plan({"name": "Squat"})},
            ))
        error_result = json.loads(payload["messages"][-1]["content"])
        assert error_result["is_error"] is True
        assert error_result["result"]["error"] == (
            "ambiguous exercise name, use the exact library name"
        )
        return text_response("Please use the exact library name.")

    reply, audit = await run_agent(
        history=[], message="Build my plan", onboarding=True,
        handlers=srv._coach_tool_handlers(), post=fake_post,
    )

    assert reply.startswith("Please use")
    assert [entry["ok"] for entry in audit] == [False]
    assert fake.plan is None


async def test_set_workout_plan_tied_name_matches_are_tool_error(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    fake = FakeStore(library=[
        {"id": "back-squat-1", "name": "Back Squat"},
        {"id": "hack-squat-1", "name": "Hack Squat"},
    ])
    monkeypatch.setattr(srv, "_client", fake)
    calls = 0

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
                return tool_response(tool_call(
                    "ambiguous", "set_workout_plan",
                    {"plan": workout_plan({"name": "Back Squat Hack Squat"})},
                ))
        error_result = json.loads(payload["messages"][-1]["content"])
        assert error_result["is_error"] is True
        assert "Ambiguous workout library exercise" in error_result["result"]["error"]
        return text_response("Please clarify which squat you want.")

    reply, audit = await run_agent(
        history=[], message="Build my plan", onboarding=True,
        handlers=srv._coach_tool_handlers(), post=fake_post,
    )

    assert reply.startswith("Please clarify")
    assert [entry["ok"] for entry in audit] == [False]
    assert fake.plan is None


async def test_set_workout_plan_unknown_exercise_is_tool_error_and_can_retry(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    fake = FakeStore(library=[{"id": "squat-1", "name": "Back Squat"}])
    monkeypatch.setattr(srv, "_client", fake)
    calls = 0

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_response(tool_call(
                "unknown", "set_workout_plan",
                {"plan": workout_plan({"name": "Moon Walk Lunge"})},
            ))
        if calls == 2:
            error_result = json.loads(payload["messages"][-1]["content"])
            assert error_result["is_error"] is True
            assert "Moon Walk Lunge" in error_result["result"]["error"]
            return tool_response(tool_call(
                "retry", "set_workout_plan",
                {"plan": workout_plan({"name": "Back Squat"})},
            ))
        return text_response("I fixed the exercise and saved your plan.")

    reply, audit = await run_agent(
        history=[], message="Build my plan", onboarding=True,
        handlers=srv._coach_tool_handlers(), post=fake_post,
    )

    assert reply.startswith("I fixed")
    assert [entry["ok"] for entry in audit] == [False, True]
    assert fake.plan["days"]["Legs"]["exercises"][0]["name"] == "Back Squat"


async def test_library_call_emits_exercise_card_widget(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    exercise = {
        "name": "Bench Press", "muscle_group": ["Chest", "Triceps"],
        "workout_type": "Push", "equipment": "barbell",
        "video_url": "https://media.example/bench.gif",
        "instructions": "Lower the bar.\nPress it up.",
    }

    async def fake_parse(_message): return [], None
    async def fake_run_agent(**_kwargs):
        return "Try Bench Press today.", [{
            "tool": "get_library", "input": {"query": "push"}, "ok": True,
            "result": {"exercises": [exercise]},
        }]
    async def fake_day_payload(_day): return {"totals": {"calories": 0}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "run_agent", fake_run_agent)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(chat_request({"message": "What should I train?"}))

    assert json.loads(response.body)["widget"] == {
        "type": "exercise_card",
        "exercise": {
            "exercise_name": "Bench Press",
            "muscle_group": ["Chest", "Triceps"],
            "equipment": "barbell", "sets": None, "reps": None,
            "video_url": "https://media.example/bench.gif",
            "instructions": "Lower the bar.\nPress it up.",
            "workout_type": "Push",
        },
    }


def test_exercise_demo_guidance_requires_library_first_and_forbids_video_refusal():
    library_tool = next(tool for tool in TOOLS if tool["name"] == "get_library")
    description = library_tool["description"]
    assert "call this tool FIRST" in description
    assert "exact returned exercise name" in description
    assert "never say videos cannot be embedded" in description

    for prompt in (SYSTEM_PROMPT, system_prompt(onboarding=False)):
        normalized = " ".join(prompt.replace("`", "").split())
        assert "call get_library with the exercise name FIRST" in normalized
        assert "exact returned exercise name" in normalized
        assert "Never say you cannot embed or show videos" in normalized


async def test_exercise_demo_turn_emits_video_card(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    exercise = {
        "name": "Dumbbell Bench Press",
        "muscle_group": ["Chest", "Triceps"],
        "workout_type": "Push",
        "equipment": "dumbbell",
        "video_url": "https://media.example/dumbbell-bench-press.gif",
        "instructions": "Lower the dumbbells with control, then press them up.",
    }
    calls = 0

    async def fake_post(_token, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert payload["messages"][-1]["content"] == "show me how to do dumbbell press"
            prompt = " ".join(payload["messages"][0]["content"].replace("`", "").split())
            assert "call get_library with the exercise name FIRST" in prompt
            return tool_response(tool_call(
                "demo-library", "get_library", {"query": "dumbbell press"},
            ))
        result = json.loads(payload["messages"][-1]["content"])["result"]
        assert result["exercises"][0]["video_url"] == exercise["video_url"]
        assert result["exercises"][0]["instructions"] == exercise["instructions"]
        return text_response("Use Dumbbell Bench Press with a controlled range of motion.")

    async def get_library(arguments):
        assert arguments == {"query": "dumbbell press"}
        return {"exercises": [exercise]}

    reply, audit = await run_agent(
        history=[], message="show me how to do dumbbell press", onboarding=False,
        handlers={"get_library": get_library}, post=fake_post,
    )
    widget = srv.exercise_card_widget(reply, audit)

    assert calls == 2
    assert [entry["tool"] for entry in audit] == ["get_library"]
    assert "can't embed" not in reply.casefold()
    assert widget == {
        "type": "exercise_card",
        "exercise": {
            "exercise_name": "Dumbbell Bench Press",
            "muscle_group": ["Chest", "Triceps"],
            "equipment": "dumbbell",
            "sets": None,
            "reps": None,
            "video_url": "https://media.example/dumbbell-bench-press.gif",
            "instructions": "Lower the dumbbells with control, then press them up.",
            "workout_type": "Push",
        },
    }


async def test_run_agent_library_audit_keeps_exercise_card_fields(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-key")
    exercise = {
        "name": "Bench Press", "muscle_group": ["Chest", "Triceps"],
        "workout_type": "Push", "equipment": "barbell",
        "video_url": "https://media.example/bench.gif",
        "instructions": "Lower the bar.\nPress it up.",
        "overview": "A long overview that must not be persisted.",
        "description": "A long description that must not be persisted.",
    }
    calls = 0

    async def fake_post(_token, _payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_response(tool_call("library", "get_library", {"query": "push"}))
        return text_response("Try Bench Press today.")

    async def get_library(_arguments):
        return {"exercises": [exercise]}

    reply, audit = await run_agent(
        history=[], message="What should I train?", onboarding=False,
        handlers={"get_library": get_library}, post=fake_post,
    )

    compact = audit[0]["result"]["exercises"][0]
    assert compact == {key: exercise[key] for key in (
        "name", "instructions", "video_url", "muscle_group", "equipment", "workout_type",
    )}
    assert srv.exercise_card_widget(reply, audit)["exercise"]["instructions"] == exercise["instructions"]


async def test_library_call_without_exercise_mention_emits_no_card(monkeypatch):
    result = {"tool": "get_library", "input": {}, "ok": True,
              "result": {"exercises": [{"name": "Bench Press"}]}}
    widget = srv.exercise_card_widget("Here is another option.", [result])
    assert widget is None


def test_library_widget_selects_exercise_mentioned_in_reply():
    first_result = {"tool": "get_library", "input": {}, "ok": True,
                    "result": {"exercises": [
                        {"name": "Back Squat"},
                        {"name": "Deadlift", "video_url": "https://media.example/deadlift.gif"},
                    ]}}
    second_result = {"tool": "get_library", "input": {}, "ok": True,
                     "result": {"exercises": [{"name": "Leg Press"}]}}

    widget = srv.exercise_card_widget(
        "Deadlift is the best choice today.", [first_result, second_result],
    )

    assert widget["exercise"]["exercise_name"] == "Deadlift"
    assert widget["exercise"]["video_url"] == "https://media.example/deadlift.gif"


def test_library_tool_alias_emits_exercise_card_widget():
    result = {"tool": "library_tool", "input": {"query": "dumbbell press"}, "ok": True,
              "result": {"exercises": [{
                  "name": "Dumbbell Bench Press",
                  "video_url": "https://media.example/dumbbell-bench-press.gif",
              }]}}

    widget = srv.exercise_card_widget("Try Dumbbell Bench Press.", [result])

    assert widget["exercise"]["exercise_name"] == "Dumbbell Bench Press"
    assert widget["exercise"]["video_url"] == "https://media.example/dumbbell-bench-press.gif"


def test_library_widget_prefers_longest_overlapping_exercise_name():
    exercises = [
        {"name": "Squat", "video_url": "https://media.example/squat.gif"},
        {"name": "Back Squat", "video_url": "https://media.example/back-squat.gif"},
    ]
    result = {"tool": "get_library", "input": {}, "ok": True,
              "result": {"exercises": exercises}}

    widget = srv.exercise_card_widget("Try Back Squat today.", [result])

    assert widget["exercise"]["exercise_name"] == "Back Squat"


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
    assert tools["get_display_name"]["input_schema"]["required"] == []
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
        if sql.startswith("SELECT display_name"):
            return "Ana"
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


async def test_fetch_workout_library_returns_media_and_instructions():
    class LibraryPool:
        def __init__(self):
            self.sql = ""

        async def fetch(self, sql, *args):
            self.sql = sql
            return [{"name": "Bench Press", "muscle_group": ["Chest"],
                     "workout_type": "Push", "equipment": "barbell",
                     "difficulty": "intermediate", "swaps": [],
                     "video_url": "https://media.example/bench.gif",
                     "instructions": "Press with control."}]

    pool = LibraryPool()
    store = Store("postgresql://unused/unused")
    store.pool = pool

    rows = await store.fetch_workout_library()

    assert rows[0]["video_url"] == "https://media.example/bench.gif"
    assert rows[0]["instructions"] == "Press with control."
    assert "video_url,instructions" in pool.sql


async def test_profile_store_writes_and_reads_only_bound_user():
    pool = ProfilePool()
    store = Store("postgresql://unused/unused")
    store.pool = pool
    user_id = uuid4()
    token = bind_user(user_id)
    try:
        assert await store.put_display_name("Ana") == {"display_name": "Ana"}
        assert await store.get_display_name() == "Ana"
        await store.put_metrics({"height_cm": 178, "weight_kg": 80,
                                 "goal_weight_kg": 75})
        metrics = await store.get_metrics()
    finally:
        reset_user(token)

    assert pool.calls[0][1] == ("Ana", user_id)
    assert pool.calls[1][1] == (user_id,)
    assert pool.calls[2][1][0] == user_id
    assert pool.calls[3][1] == (user_id,)
    assert metrics["height_cm"] == 178
    assert all("user_id" in sql or "WHERE id=$" in sql for sql, _args in pool.calls)


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
