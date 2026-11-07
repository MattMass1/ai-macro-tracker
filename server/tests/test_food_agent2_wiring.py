"""food_agent2 wiring: adapter helpers, request pricing, and the live bench path with fake providers. Offline."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import food_agent_bench as bench  # noqa: E402
import food_agent_bench_live as live  # noqa: E402
from test_food_agent_bench_live import (FakeModel, _no_real_network, _providers, _spec,  # noqa: E402,F401
                                        enable_fake_fatsecret)


@pytest.fixture
def usda(monkeypatch):
    import usda_lookup
    usda_lookup.clear_cache()
    monkeypatch.setenv("USDA_API_KEY", "synthetic")
    yield usda_lookup
    usda_lookup.clear_cache()


def _rows_response(*descriptions):
    foods = [{"fdcId": i, "dataType": "SR Legacy", "description": d, "foodNutrients": [
        {"nutrientId": 1008, "value": 100.0}, {"nutrientId": 1003, "value": 1.0}, {"nutrientId": 1005, "value": 0.0},
        {"nutrientId": 1004, "value": 1.0}]} for i, d in enumerate(descriptions, start=1)]
    return httpx.Response(200, json={"foods": foods}, request=httpx.Request("POST", "https://fake.invalid"))


async def test_exact_lookup_and_closest_rows_share_one_wire_call(usda):
    calls = []

    async def post(params):
        calls.append(params)
        return _rows_response("Bagel, plain")

    await usda.lookup("bagel", post=post)
    found = await usda.rows("bagel", post=post)
    assert len(calls) == 1 and found[0]["description"] == "Bagel, plain"


async def test_a_failed_search_is_not_remembered_as_an_empty_answer(usda):
    async def down(params):
        return httpx.Response(503, request=httpx.Request("POST", "https://fake.invalid"))

    async def up(params):
        return _rows_response("Bagel, plain")

    assert await usda.rows("bagel", post=down) == []
    assert len(await usda.rows("bagel", post=up)) == 1


async def test_relaxed_and_strict_searches_are_separate(usda):
    seen = []

    async def post(params):
        seen.append(params["requireAllWords"])
        return _rows_response()

    await usda.rows("bagel", post=post)
    await usda.rows("bagel", require_all=False, post=post)
    assert seen == ["true", "false"]


async def test_rows_without_a_key_makes_no_call(usda, monkeypatch):
    monkeypatch.delenv("USDA_API_KEY")

    async def post(params):
        raise AssertionError("no key, no call")

    assert await usda.rows("bagel", post=post) == []


async def test_fatsecret_helpers_are_quiet_when_it_is_not_configured(monkeypatch):
    import food_lookup
    monkeypatch.setattr(food_lookup, "_fatsecret_provider_mode", lambda: None)
    assert await food_lookup.fatsecret_candidates("bagel") == []
    assert await food_lookup.fatsecret_detail("1", "bagel", {}) is None


async def test_fatsecret_detail_returns_a_near_match_the_exact_gate_would_refuse(monkeypatch):
    import food_lookup

    async def request(data):
        return {"food": {"food_id": "9", "food_name": "Marshmallow Rocky Road", "brand_name": "Barebells",
                         "servings": {"serving": [{"serving_description": "1 bar", "calories": "200", "protein": "20",
                                                   "carbohydrate": "16", "fat": "9", "fiber": "3"}]}}}
    monkeypatch.setattr(food_lookup, "_fatsecret_provider_mode", lambda: "oauth2")
    monkeypatch.setattr(food_lookup, "_fatsecret_request", request)
    found = await food_lookup.fatsecret_detail("9", "Barebells Marshmallow Peanut Road",
                                               {"food_id": "9", "food_name": "Marshmallow Rocky Road"})
    assert found["name"] == "Marshmallow Rocky Road"
    assert found["attribution"]["verification_state"] == "provider_search_match"  # never claimed as exact


def test_a_full_context_request_stays_inside_the_priced_route():
    """A request the rate card refuses would stop a paid run on its first call."""
    from food_agent2.interpret import ModelInterpreter
    from food_agent_budget import RateCard
    item = {"item_ref": "i1", "identity": {"name": "x" * 60, "brand": "y" * 30, "variant": None, "kind": "restaurant",
                                           "preparation": None}, "portion": {"kind": "count", "amount": 1, "unit": None,
                                                                             "text": "1"},
            "modifications": [], "matched": "z" * 80}
    context = {"now": "2026-11-02T17:30:00+00:00", "last_question": None, "last_logged": {"items": [item] * 12},
               "open_drafts": [{"awaiting_yes": True, "question": None, "items": [item] * 12}]}
    payload = ModelInterpreter(token="t", model="gpt-6.1-terra").payload("3 eggs and toast", context)
    _cost, input_bound, _output = RateCard(version="synthetic").model_cost(payload)
    assert input_bound <= 32000


class Agent2Model(FakeModel):
    """The scripted correct answers, in the new contract."""

    async def __call__(self, token, payload):
        self.seen_format = payload["text"]["format"]["name"]
        response = await super().__call__(token, payload)
        body = response.json()
        text = body["output"][0]["content"][0]["text"]
        body["output"][0]["content"][0]["text"] = bench.as_agent2_turn(json.loads(text)).model_dump_json()
        return httpx.Response(200, json=body, request=response.request)


async def test_lane_c_drives_the_new_agent_with_every_wire_attempt_counted(monkeypatch):
    monkeypatch.setenv("FOOD_BENCH_AGENT", "2")
    spec = _spec("syn-half-burger", "syn-boiled-eggs-control", "syn-last-time-bowl")
    fatsecret_calls = enable_fake_fatsecret(monkeypatch, spec)
    budget, model = live.Budget(max_model_calls=300, max_lookups=200), Agent2Model(spec)
    results = await live.run_live_bench(spec, bench.MemoryBackend(), budget, repeats=1,
                                        **_providers(model, spec=spec))
    assert results.get("configuration_errors") is None
    assert [run["status"] for run in results["runs"]] == ["run"] * 3
    assert model.seen_format == "food_turn"
    assert results["summary"]["old_path_calls"] == 0
    assert all(value == 0 for value in results["summary"]["zero_tolerance"].values())
    assert budget.lookups_used >= len(fatsecret_calls) > 0
    burger = next(run for run in results["runs"] if run["case_id"] == "syn-half-burger")
    assert burger["items_audit"][0]["multiplier"] == 0.5


async def test_lane_c_still_stops_at_a_cap_on_the_new_agent(monkeypatch):
    monkeypatch.setenv("FOOD_BENCH_AGENT", "2")
    spec = _spec("syn-half-burger")
    enable_fake_fatsecret(monkeypatch, spec)
    budget, model = live.Budget(max_model_calls=0, max_lookups=200), Agent2Model(spec)
    results = await live.run_live_bench(spec, bench.MemoryBackend(), budget, repeats=1,
                                        **_providers(model, spec=spec))
    assert results["summary"]["stopped_by_cap"] == "model"
    assert results["summary"]["model_calls"] == 0
