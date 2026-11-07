"""food_agent2 release gate, server factory, history recall and the model adapter. Offline."""

from __future__ import annotations

import json
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from auth import bind_user, reset_user

FLAGS = ("FOOD_AGENT2_ENABLED", "FOOD_AGENT2_USER_IDS", "FOOD_AGENT_ENABLED", "FOOD_AGENT_USER_IDS")


@pytest.fixture
def clean_env(monkeypatch):
    for flag in FLAGS:
        monkeypatch.delenv(flag, raising=False)
    return monkeypatch


@pytest.fixture
def srv(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    clean_env.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import server
    return server


class As:
    def __init__(self, user):
        self.user = user

    def __enter__(self):
        self.token = bind_user(self.user)
        return self

    def __exit__(self, *exc):
        reset_user(self.token)


def test_flag_names_are_exact():
    import release_gates
    assert release_gates.FOOD_AGENT2_FLAG == "FOOD_AGENT2_ENABLED"
    assert release_gates.FOOD_AGENT2_USERS_FLAG == "FOOD_AGENT2_USER_IDS"


@pytest.mark.parametrize("value", [None, "", "yes", "1", "on", "truee", "false"])
def test_anything_but_true_keeps_it_closed(clean_env, value):
    import release_gates
    if value is not None:
        clean_env.setenv("FOOD_AGENT2_ENABLED", value)
    with As(uuid4()):
        assert release_gates.food_agent2_allowed() is False


def test_canary_list_and_malformed_list(clean_env):
    import release_gates
    alice, bob = uuid4(), uuid4()
    clean_env.setenv("FOOD_AGENT2_ENABLED", "true")
    clean_env.setenv("FOOD_AGENT2_USER_IDS", f" {alice} ,")
    with As(alice):
        assert release_gates.food_agent2_allowed() is True
    with As(bob):
        assert release_gates.food_agent2_allowed() is False
    clean_env.setenv("FOOD_AGENT2_USER_IDS", "not-a-uuid")
    with As(alice):
        assert release_gates.food_agent2_allowed() is False


def test_old_gate_does_not_open_the_new_one(clean_env):
    import release_gates
    clean_env.setenv("FOOD_AGENT_ENABLED", "true")
    with As(uuid4()):
        assert release_gates.food_agent2_allowed() is False


async def test_gate_off_the_server_factory_builds_nothing_new(srv, monkeypatch):
    import server

    def explode():
        raise AssertionError("store must not be read with both gates off")
    monkeypatch.setattr(server, "store_client", explode)
    assert await server._food_agent_engine() is None


async def test_gate_on_the_server_factory_builds_the_new_agent(srv, clean_env, monkeypatch):
    import server
    from food_agent2.turn import FoodAgent2

    class Ready:
        async def meal_drafts_ready(self):
            return True
    monkeypatch.setattr(server, "store_client", lambda: Ready())
    clean_env.setenv("FOOD_AGENT2_ENABLED", "true")
    assert isinstance(await server._food_agent_engine(), FoodAgent2)


async def test_new_gate_wins_over_the_old_one(srv, clean_env, monkeypatch):
    import server
    from food_agent2.turn import FoodAgent2

    class Ready:
        async def meal_drafts_ready(self):
            return True
    monkeypatch.setattr(server, "store_client", lambda: Ready())
    clean_env.setenv("FOOD_AGENT2_ENABLED", "true")
    clean_env.setenv("FOOD_AGENT_ENABLED", "true")
    assert isinstance(await server._food_agent_engine(), FoodAgent2)


async def test_missing_schema_is_the_old_path(srv, clean_env, monkeypatch):
    import server

    class NotReady:
        async def meal_drafts_ready(self):
            return False
    monkeypatch.setattr(server, "store_client", lambda: NotReady())
    clean_env.setenv("FOOD_AGENT2_ENABLED", "true")
    assert await server._food_agent_engine() is None


# ---------------------------------------------------------------- model adapter


def completed(payload):
    return httpx.Response(200, json={"status": "completed", "output": [
        {"type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": json.dumps(payload)}]}]},
        request=httpx.Request("POST", "https://example.invalid"))


async def test_adapter_returns_a_typed_turn_and_uses_the_strict_schema():
    from food_agent2.contracts import SCHEMA_VERSION, Turn
    from food_agent2.interpret import ModelInterpreter
    seen = {}

    async def post(token, payload):
        seen.update(payload)
        return completed({"schema_version": SCHEMA_VERSION, "actions": [{"type": "cancel"}]})

    turn = await ModelInterpreter(post=post, token="t", model="m")("cancel", {"open_drafts": []})
    assert isinstance(turn, Turn)
    assert seen["store"] is False and seen["text"]["format"]["strict"] is True
    assert "tools" not in seen


@pytest.mark.parametrize("reply", [
    httpx.Response(500, json={}, request=httpx.Request("POST", "https://example.invalid")),
    completed({"schema_version": "food-agent/2", "actions": [{"type": "ask", "question": "which?"}]}),
    completed({"schema_version": "food-agent/1", "actions": [{"type": "cancel"}]}),
])
async def test_adapter_refuses_anything_outside_the_contract(reply):
    from food_agent2.interpret import InterpreterError, ModelInterpreter

    async def post(token, payload):
        return reply
    with pytest.raises(InterpreterError):
        await ModelInterpreter(post=post, token="t", model="m")("x", {})


def test_instructions_forbid_asking_and_inventing_nutrients():
    from food_agent2.interpret import INSTRUCTIONS
    text = INSTRUCTIONS.casefold()
    assert "never ask" in text and "never invent" in text


# ---------------------------------------------------------------- history recall


class History:
    def __init__(self, rows):
        self.rows, self.terms = rows, None

    async def search_own_entries(self, terms, *, since, until=None, limit=20):
        self.terms = list(terms)
        return [dict(row) for row in self.rows]


def row(name, days_ago, kcal=500, entry=None):
    today = date(2026, 11, 2)
    return {"id": entry or str(uuid4()), "name": name, "day": today - timedelta(days=days_ago),
            "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc), "calories": kcal, "protein": 30, "carbs": 40,
            "fat": 20, "fiber": 3, "macro_source": "FatSecret: 1", "component_metadata": None}


CLOCK = lambda: datetime(2026, 11, 2, 17, 30, tzinfo=timezone.utc)  # noqa: E731


async def test_recall_finds_the_named_food_on_the_named_day():
    from food_agent2.history import HistoryRecall
    rows = [row("Ahi tuna bowl", 4, kcal=610), row("Ahi tuna bowl", 20, kcal=700)]
    found = await HistoryRecall(History(rows), CLOCK).recall("ahi tuna bowl", "last Thursday")
    assert found.status == "found" and found.items[0].resolved.panel.calories == 610


async def test_recall_never_falls_back_to_a_recent_unrelated_meal():
    from food_agent2.history import HistoryRecall
    found = await HistoryRecall(History([row("2 Burger King Royal Crispy Wrap", 1)]), CLOCK).recall("ahi tuna bowl", None)
    assert found.status == "not_found" and not found.items


async def test_several_past_matches_take_the_newest_and_never_a_variant_list():
    from food_agent2.history import HistoryRecall
    rows = [row("Chipotle chicken bowl", 3, kcal=650), row("Chipotle steak bowl", 9, kcal=720)]
    found = await HistoryRecall(History(rows), CLOCK).recall("chipotle bowl", None)
    assert found.status == "found" and found.items[0].resolved.panel.calories == 650
    assert "Chipotle chicken bowl" in found.note


async def test_a_typo_is_matched_with_disclosure_not_did_you_mean():
    from food_agent2.history import HistoryRecall
    found = await HistoryRecall(History([row("Ahi tuna bowl", 3)]), CLOCK).recall("ahi tuan bowl", None)
    assert found.status == "found" and "Did you mean" not in (found.note or "")


async def test_empty_query_is_not_found_not_a_question_loop():
    from food_agent2.history import HistoryRecall
    found = await HistoryRecall(History([row("Anything", 1)]), CLOCK).recall("the one from last time", None)
    assert found.status == "not_found"


async def test_recall_miss_asks_once_then_moves_on(monkeypatch):
    from test_food_agent2_flow import Rig
    from test_food_agent2_retrieval import FakeSources, web_estimate
    from food_agent2.history import HistoryRecall
    sources = FakeSources(web={"ahi tuna bowl": web_estimate("ahi tuna bowl", 600.0)})
    rig = Rig(sources, recall=HistoryRecall(History([row("2 Burger King Royal Crispy Wrap", 1)]), CLOCK))
    action = {"type": "recall_meal", "query": "ahi tuna bowl", "when_hint": None}
    rig.script.push(action)
    first = await rig.say("I had an ahi tuna bowl again the one I had last time")
    assert first.question.endswith("?") and "where was the ahi tuna bowl from" in first.question.casefold()
    assert "how much of each" in first.question
    rig.script.push({**action, "when_hint": "last Thursday"})
    second = await rig.say("No man, log the ahi tuna bowl I had last Thursday")
    assert second.reply != first.reply and second.preview is not None
    assert not rig.entries


# ---------------------------------------------------------------- the bench oracle


def _oracle():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import food_agent_bench
    return food_agent_bench


def test_a_brand_with_a_lone_a_can_match_its_own_name():
    bench = _oracle()
    gold = {"brand": "Chick-fil-A", "name": "Yogurt Parfait", "variant": None, "preparation": None}
    assert bench.identity_matches(gold, "Chick-fil-A Yogurt Parfait") is True
    assert bench.identity_matches(gold, "Burger King Yogurt Parfait") is False
    assert bench.identity_matches(gold, "Chick-fil-A Fruit Cup") is False


def test_oracle_still_requires_a_stated_preparation():
    bench = _oracle()
    gold = {"brand": None, "name": "egg", "variant": None, "preparation": "sunny side up"}
    assert bench.identity_matches(gold, "Sunny side up egg") is True
    assert bench.identity_matches(gold, "Egg") is False