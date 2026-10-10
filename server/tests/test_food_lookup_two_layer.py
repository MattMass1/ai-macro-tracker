"""Two-layer lookup finish (spec 2026-10-09): one ranked provider gate, honest
outage wording, and did-you-mean options that reuse the first search."""
import asyncio
import json
import os
from uuid import uuid4

import httpx
import pytest

os.environ.setdefault("APP_SHARED_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

import food_lookup  # noqa: E402
import server as srv  # noqa: E402
import web_nutrition_lookup as web  # noqa: E402
from auth import bind_user, reset_user  # noqa: E402
from test_food_lookup import _enable_fatsecret, _fatsecret_get_hit  # noqa: E402
from test_live_coach import FakeVoiceStore  # noqa: E402


async def test_search_fetches_ranked_hits_in_order_and_at_most_two(monkeypatch):
    _enable_fatsecret(monkeypatch)
    fetched = []

    async def request(data):
        if data["method"] == "foods.search":
            return {"foods": {"food": [
                {"food_id": "broad", "food_name": "Grilled Chicken Sandwich", "brand_name": "Chick-fil-A"},
                {"food_id": "exact-empty", "food_name": "Chicken Sandwich", "brand_name": "Chick-fil-A"},
                {"food_id": "exact", "food_name": "Chicken Sandwich", "brand_name": "Chick-fil-A"},
            ]}}
        fetched.append(data["food_id"])
        if data["food_id"] == "exact-empty":
            return {"food": {"food_name": "Chicken Sandwich", "brand_name": "Chick-fil-A",
                             "servings": {"serving": []}}}
        body = _fatsecret_get_hit("Chicken Sandwich", description="1 sandwich", grams=183,
                                 calories=440, protein=28, carbs=41, fat=17, fiber=1)
        body["food"]["brand_name"] = "Chick-fil-A"
        return body

    monkeypatch.setattr(food_lookup, "_fatsecret_request", request)
    found = await food_lookup.search_fatsecret("chick fil a chicken sandwich")
    # Exact item names outrank the broader brand match; a hit without usable
    # servings yields to the next one; the broad hit is never fetched.
    assert fetched == ["exact-empty", "exact"]
    assert found["source"] == "FatSecret: exact"
    assert found["macros_per_serving"]["calories"] == 440


async def test_did_you_mean_reuses_the_failed_resolutions_search(monkeypatch):
    _enable_fatsecret(monkeypatch)
    searches = []

    async def request(data):
        if data["method"] == "foods.search":
            searches.append(data["search_expression"])
            return {"foods": {"food": [
                {"food_id": "1", "food_name": "Spicy Chicken Sandwich", "brand_name": "Chick-fil-A"},
                {"food_id": "2", "food_name": "Grilled Chicken Sandwich", "brand_name": "Chick-fil-A"},
            ]}}
        raise AssertionError("no acceptable hit, so no item is fetched")

    monkeypatch.setattr(food_lookup, "_fatsecret_request", request)
    assert await food_lookup.search_fatsecret("chick fil a chicken") is None
    options = await food_lookup.fatsecret_name_options("chick fil a chicken")
    assert options == ["Chick-fil-A Spicy Chicken Sandwich",
                       "Chick-fil-A Grilled Chicken Sandwich"]
    assert searches == ["chick fil a chicken"]


def test_compound_phrase_is_probed_whole_and_noise_fragments_are_dropped():
    # A compound utterance can only match a provider item as a whole; its
    # fragments ("and 2 slices of bacon", "2 eggs") are never searched.
    assert food_lookup._query_variants("2 eggs and 2 slices of bacon") == [
        "2 eggs and 2 slices of bacon"]
    assert food_lookup._query_variants("sweet and sour chicken") == ["sweet and sour chicken"]
    # A single identity keeps its brand/flavor/generic ladder, minus fragments
    # that start with a connective or article.
    assert food_lookup._query_variants("chick fil a chicken sandwich") == [
        "chick fil a chicken sandwich", "fil a chicken sandwich", "chick fil", "sandwich"]


async def test_same_text_is_searched_once_across_probe_and_component_pass(monkeypatch):
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)
    srv._resolved_food_cache.clear()
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: "oauth1")
    searches = []

    async def request(data):
        if data["method"] == "foods.search":
            searches.append(data["search_expression"])
        return {"foods": {"food": []}}

    monkeypatch.setattr(srv.food_lookup, "_fatsecret_request", request)
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("once", {
            "description": "zebra steak", "meal_type": "Dinner"})
    finally:
        reset_user(token)
    assert result["status"] == "needs_clarification"
    # The whole-phrase probe and the component resolution asked the same
    # question; the provider heard it once.
    assert searches == ["zebra steak"]


async def test_one_food_tool_call_reads_presets_once_for_all_components(monkeypatch):
    class CountingStore(FakeVoiceStore):
        preset_reads = 0

        async def fetch_presets(self, *args, **kwargs):
            type(self).preset_reads += 1
            return await super().fetch_presets(*args, **kwargs)

    fake = CountingStore()
    monkeypatch.setattr(srv, "_client", fake)
    srv._resolved_food_cache.clear()
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("presets-once", {
            "description": "2 eggs and 2 slices of bacon", "meal_type": "Breakfast"})
    finally:
        reset_user(token)
    assert result["status"] == "committed"
    assert CountingStore.preset_reads == 1
    assert srv._turn_presets.get() is None  # the shared read ends with the call


async def test_close_variants_without_an_exact_match_ask_instead_of_guessing(monkeypatch):
    _enable_fatsecret(monkeypatch)
    fetched = []

    async def request(data):
        if data["method"] == "foods.search":
            return {"foods": {"food": [
                {"food_id": "f", "food_name": "Chicken Sandwich Fried", "brand_name": "Fixture Grill"},
                {"food_id": "g", "food_name": "Chicken Sandwich Grilled", "brand_name": "Fixture Grill"},
            ]}}
        fetched.append(data["food_id"])
        body = _fatsecret_get_hit("Chicken Sandwich Fried", description="1 sandwich", grams=180,
                                 calories=440, protein=28, carbs=40, fat=18, fiber=1)
        body["food"]["brand_name"] = "Fixture Grill"
        return body

    monkeypatch.setattr(food_lookup, "_fatsecret_request", request)
    assert await food_lookup.search_fatsecret("fixture grill chicken sandwich") is None
    assert fetched == []
    assert await food_lookup.fatsecret_name_options("fixture grill chicken sandwich") == [
        "Fixture Grill Chicken Sandwich Fried", "Fixture Grill Chicken Sandwich Grilled"]
    # Naming the variant resolves it outright.
    found = await food_lookup.search_fatsecret("fixture grill chicken sandwich fried")
    assert found is not None and fetched == ["f"]


async def test_web_recent_failure_reports_the_fresh_reason_only(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "token")

    async def post(_token, _payload):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(web.NutritionLookupError) as error:
        await web.lookup("fixture zebra steak", post=post)
    assert error.value.reason == "timeout"
    assert web.recent_failure("fixture zebra steak") == "timeout"
    assert web.recent_failure("Fixture  Zebra Steak") == "timeout"
    assert web.recent_failure("something else") is None
    web.clear_cache()
    assert web.recent_failure("fixture zebra steak") is None
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)
    with pytest.raises(web.NutritionLookupError):
        await web.lookup("fixture zebra steak")
    assert web.recent_failure("fixture zebra steak") == "not_configured"
    assert "not_configured" in web.INFRASTRUCTURE_REASONS
    assert "no_results" not in web.INFRASTRUCTURE_REASONS


def _no_evidence_post(_token, _payload):
    return asyncio.sleep(0, result=httpx.Response(
        200, json={"output": []}, headers={"content-type": "application/json"}))


async def _timeout_post(_token, _payload):
    raise httpx.ReadTimeout("slow")


async def _outage_post(_token, _payload):
    raise httpx.ConnectError("refused")


async def test_composite_whole_phrase_retry_never_spends_a_second_web_search(monkeypatch):
    """Measured in production: an unresolved compound paid the web budget per
    part AND again for the whole phrase (25 s). The whole-phrase retry is a
    Layer 1 question only."""
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "token")
    monkeypatch.setattr(food_lookup, "_fatsecret_provider_mode", lambda: "oauth1")
    monkeypatch.setattr(food_lookup.cofid_lookup, "lookup", lambda _q: asyncio.sleep(0, result=None))
    provider_calls, web_calls = [], []

    async def provider(query):
        provider_calls.append(query); return None

    async def web_lookup(query, **_kwargs):
        web_calls.append(query); raise web.NutritionLookupError("no_results")

    monkeypatch.setattr(food_lookup, "search_fatsecret", provider)
    monkeypatch.setattr(web, "lookup", web_lookup)
    assert await food_lookup.resolve_food("fixture alpha and fixture beta") is None
    assert sorted(web_calls) == ["fixture alpha", "fixture beta"]
    assert "fixture alpha and fixture beta" in provider_calls  # Layer 1 still asked whole


async def test_variant_question_skips_the_web_fallback(monkeypatch):
    """Measured in production: a variant pick-list used to arrive only after a
    12 s web search for an unspecified variant. Ambiguity now short-circuits."""
    _enable_fatsecret(monkeypatch)
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "token")
    web_calls = []

    async def request(data):
        return {"foods": {"food": [
            {"food_id": "w", "food_name": "BBQ Smokehouse Chicken Sandwich Whole", "brand_name": "Fixture Bakery"},
            {"food_id": "h", "food_name": "BBQ Smokehouse Chicken Sandwich Half", "brand_name": "Fixture Bakery"},
        ]}}

    async def web_lookup(query, **_kwargs):
        web_calls.append(query)
        raise web.NutritionLookupError("no_results")

    monkeypatch.setattr(food_lookup, "_fatsecret_request", request)
    monkeypatch.setattr(web, "lookup", web_lookup)
    assert await food_lookup.resolve_food("fixture bakery bbq smokehouse sandwich") is None
    assert web_calls == []
    assert food_lookup.recently_ambiguous("fixture bakery bbq smokehouse sandwich")
    assert len(await food_lookup.fatsecret_name_options("fixture bakery bbq smokehouse sandwich")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("post", "expected"), [
    (_timeout_post, "The nutrition lookup for fixture zebra steak took too long. "
                    "Try again in a moment."),
    (_outage_post, "The nutrition lookup service was unreachable for fixture zebra steak. "
                   "Try again in a moment."),
    (_no_evidence_post, "I couldn't find nutrition for fixture zebra steak. What was it exactly: "
                        "the brand, the dish, or how it was prepared?"),
])
async def test_unverified_food_names_an_outage_and_never_asks_for_a_label(monkeypatch, post, expected):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "token")
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)
    srv._resolved_food_cache.clear()
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    monkeypatch.setattr(srv.food_lookup.cofid_lookup, "lookup",
                        lambda _q: asyncio.sleep(0, result=None))
    monkeypatch.setattr(web, "post_responses", post)
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        logged = await handlers["log_meal"]("outage-log", {
            "description": "fixture zebra steak", "meal_type": "Dinner"})
        looked_up = await handlers["lookup_food"]("outage-lookup", {
            "query": "fixture zebra steak"})
    finally:
        reset_user(token)
    assert logged["status"] == "needs_clarification"
    assert logged["question"] == logged["confirmation"] == expected
    assert logged["unresolved"] == ["fixture zebra steak"]
    assert looked_up["status"] == "needs_clarification"
    assert looked_up["question"] == expected
    assert fake.insert_count == 0
    for payload in (logged, looked_up):
        assert "label" not in json.dumps(payload).casefold()
        assert "ESTIMATE" not in json.dumps(payload)
