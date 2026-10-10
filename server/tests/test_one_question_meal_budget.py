"""One question per meal (Matt, 2026-10-10): say the food; at most one
clarification; move on. The server remembers the question it asked per canvas
session and finishes the follow-up instead of asking again, stating the
assumption it made. A different food, or a different session, starts fresh."""
import asyncio
import json
from uuid import uuid4

import pytest

from auth import bind_user, reset_user
from test_food_lookup import _fatsecret_get_hit
from test_live_coach import FakeVoiceStore, _import_server, _web_estimate


def _fresh(monkeypatch):
    srv = _import_server(monkeypatch)
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)
    srv._resolved_food_cache.clear()
    srv._web_estimate_confirmations.clear()
    srv._meal_questions.clear()
    monkeypatch.setattr(srv.food_lookup.cofid_lookup, "lookup", lambda _q: asyncio.sleep(0, result=None))
    return srv, fake


def _variants(monkeypatch, srv):
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: "oauth1")
    fetched = []

    async def request(data):
        if data["method"] == "foods.search":
            return {"foods": {"food": [
                {"food_id": "whole", "food_name": "BBQ Sandwich Whole", "brand_name": "Fixture Bakery"},
                {"food_id": "half", "food_name": "BBQ Sandwich Half", "brand_name": "Fixture Bakery"},
            ]}}
        fetched.append(data["food_id"])
        name = "BBQ Sandwich Whole" if data["food_id"] == "whole" else "BBQ Sandwich Half"
        body = _fatsecret_get_hit(name, description="1 sandwich", grams=300 if name.endswith("Whole") else 150,
                                 calories=640 if name.endswith("Whole") else 320, protein=40, carbs=70,
                                 fat=20, fiber=4)
        body["food"]["brand_name"] = "Fixture Bakery"
        body["food"]["food_id"] = data["food_id"]
        return body

    monkeypatch.setattr(srv.food_lookup, "_fatsecret_request", request)
    return fetched


@pytest.mark.asyncio
async def test_variant_question_then_the_follow_up_logs_the_top_variant(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    fetched = _variants(monkeypatch, srv)
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        first = await handlers["log_meal"]("canvas:S1:turn-1", {
            "description": "fixture bakery bbq sandwich", "meal_type": "Lunch"})
        assert first["status"] == "needs_clarification"
        assert len(first["options"]) == 2 and fake.insert_count == 0 and fetched == []
        # The user answers without picking ("whatever, log it"): no second question.
        second = await handlers["log_meal"]("canvas:S1:turn-2", {
            "description": "fixture bakery bbq sandwich", "meal_type": "Lunch"})
    finally:
        reset_user(token)
    assert second["status"] == "committed"
    assert fake.insert_count == 1
    assert second["logged"]["calories"] == 640
    assert "I assumed the BBQ Sandwich Whole variant." in second["confirmation"]
    assert second["components"][0]["attribution"]["verification_state"] == "provider_assumed_variant"
    assert srv._meal_questions == {}


@pytest.mark.asyncio
async def test_naming_the_variant_in_the_answer_needs_no_assumption(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _variants(monkeypatch, srv)
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        await handlers["log_meal"]("canvas:S1:turn-1", {"description": "fixture bakery bbq sandwich"})
        second = await handlers["log_meal"]("canvas:S1:turn-2", {"description": "fixture bakery bbq sandwich half"})
    finally:
        reset_user(token)
    assert second["status"] == "committed" and second["logged"]["calories"] == 320
    assert "assumed" not in second["confirmation"]


@pytest.mark.asyncio
async def test_web_estimate_follow_up_logs_without_needing_the_ref(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    monkeypatch.setattr(srv.food_lookup.web_nutrition_lookup, "lookup",
                        lambda query, **_k: asyncio.sleep(0, result=_web_estimate(query=query)))
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        first = await handlers["log_meal"]("canvas:S2:t1", {"description": "200 g cooked red quinoa"})
        assert first["reason"] == "web_estimate_confirmation" and fake.insert_count == 0
        # The model relays the user's yes but forgets the ref: the budget is spent, so it logs.
        second = await handlers["log_meal"]("canvas:S2:t2", {"description": "200 g cooked red quinoa"})
    finally:
        reset_user(token)
    assert second["status"] == "committed" and fake.insert_count == 1
    assert second["confirmation"].startswith("Estimated and logged")


@pytest.mark.asyncio
async def test_unknown_food_follow_up_reports_plainly_with_no_second_question(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        first = await handlers["log_meal"]("canvas:S3:t1", {"description": "fixture zebra steak"})
        assert first["status"] == "needs_clarification"
        second = await handlers["log_meal"]("canvas:S3:t2", {"description": "fixture zebra steak"})
    finally:
        reset_user(token)
    assert second["status"] == "failed"
    assert "question" not in second
    assert second["confirmation"].startswith("I still couldn't find nutrition for fixture zebra steak")
    assert fake.insert_count == 0 and srv._meal_questions == {}


@pytest.mark.asyncio
async def test_a_different_food_or_session_starts_a_fresh_budget(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        assert (await handlers["log_meal"]("canvas:S4:t1", {"description": "fixture zebra steak"}))["status"] == "needs_clarification"
        other_food = await handlers["log_meal"]("canvas:S4:t2", {"description": "fixture alpaca chop"})
        other_session = await handlers["log_meal"]("canvas:S5:t1", {"description": "fixture alpaca chop"})
    finally:
        reset_user(token)
    assert other_food["status"] == "needs_clarification"
    assert other_session["status"] == "needs_clarification"
    assert fake.insert_count == 0


@pytest.mark.asyncio
async def test_unusable_portion_follow_up_logs_a_standard_serving(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    monkeypatch.setattr(srv.food_lookup.web_nutrition_lookup, "lookup",
                        lambda query, **_k: asyncio.sleep(0, result=_web_estimate(
                            query=query, basis_unit="serving", food_name="fixture bread")))
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        first = await handlers["log_meal"]("canvas:S6:t1", {"description": "fixture bread", "grams": 80})
        assert first["reason"] == "invalid_portion"
        second = await handlers["log_meal"]("canvas:S6:t2", {"description": "fixture bread", "grams": 80})
    finally:
        reset_user(token)
    assert second["status"] == "committed" and fake.insert_count == 1
    assert "I assumed a standard serving." in second["confirmation"]
    assert "Estimated and logged" in second["confirmation"]
    assert "question" not in json.dumps(second)
