"""Network-free tests for the free food-database lookup cascade."""
import asyncio
import json
import os
import time
from uuid import uuid4

import httpx
import pytest

os.environ.setdefault("NOTION_TOKEN", "secret_test")
os.environ.setdefault("NUTRITION_DS_ID", "ds-nutrition")
os.environ.setdefault("TARGETS_DS_ID", "ds-targets")
os.environ.setdefault("PRESETS_DS_ID", "ds-presets")
os.environ.setdefault("APP_SHARED_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

import coach  # noqa: E402
import food_lookup  # noqa: E402
import server as srv  # noqa: E402
from auth import bind_user, reset_user  # noqa: E402
from test_coach import FakeStore, chat_request  # noqa: E402


def test_provider_defaults_and_global_off_license_metadata(monkeypatch):
    monkeypatch.delenv("FATSECRET_ATTRIBUTION_ENABLED", raising=False)
    assert os.environ.get("FATSECRET_ATTRIBUTION_ENABLED", "false") == "false"
    product = {"product_name": "Bar", "nutriments": {
        "energy-kcal_100g": 200, "proteins_100g": 10,
        "carbohydrates_100g": 20, "fat_100g": 8, "fiber_100g": 2}}
    exact = food_lookup._off_result(product, "123", exact=True)
    text = food_lookup._off_result(product, "123", exact=False)
    assert exact["attribution"]["cache_allowed"] is True
    assert exact["attribution"]["license"] == "Open Database License (ODbL) 1.0"
    assert exact["attribution"]["license_url"].endswith("/odbl/1-0/")
    assert text["attribution"]["cache_allowed"] is False
    assert text["attribution"]["verification_state"] == "unknown"

OFF_PAYLOAD = {"products": [{
    "code": "737628064502",
    "product_name": "Rice noodles",
    "nutriments": {"energy-kcal_100g": 355, "proteins_100g": 7.1,
                   "carbohydrates_100g": 78.6, "fat_100g": 1.2,
                   "fiber_100g": 1.8},
}]}

OFF_BARCODE_PAYLOAD = {
    "status": 1,
    "product": {
        "product_name": "Chocolate protein bar",
        "serving_size": "1 bar (55 g)",
        "nutriments": {
            "energy-kcal_100g": 364,
            "proteins_100g": 36.4,
            "carbohydrates_100g": 32.7,
            "fat_100g": 14.5,
            "fiber_100g": 5.5,
            "energy-kcal_serving": 200,
            "proteins_serving": 20,
            "carbohydrates_serving": 18,
            "fat_serving": 8,
            "fiber_serving": 3,
        },
    },
}

BANANA_HIT = {
    "name": "Bananas, raw",
    "macros_per_100g": {"calories": 89.0, "protein": 1.09, "carbs": 22.84,
                        "fat": 0.33, "fiber": 2.6},
    "source": "OpenFoodFacts: 4011",
}




def mock_transport(monkeypatch, handler):
    """Route every food_lookup HTTP call through a MockTransport, recording requests."""
    calls = []

    def recording_handler(request):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(food_lookup, "_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(recording_handler), timeout=food_lookup.TIMEOUT,
        headers={"User-Agent": food_lookup.USER_AGENT}))
    return calls


def without_attribution(result):
    return {key: value for key, value in result.items() if key != "attribution"}


async def test_client_sends_identifying_user_agent():
    # OpenFoodFacts rejects/throttles the default python-httpx User-Agent.
    client = food_lookup._client()
    try:
        assert client.headers["User-Agent"] == \
            "MacroCoach/1.0 (ai-macro-tracker; contact@biz21.com)"
    finally:
        await client.aclose()


async def test_barcode_hit_parses_per_100g_and_serving_macros(monkeypatch):
    def handler(request):
        assert request.method == "GET"
        assert request.url == httpx.URL(
            "https://world.openfoodfacts.org/api/v2/product/737628064502.json"
        )
        assert request.headers["User-Agent"] == food_lookup.USER_AGENT
        return httpx.Response(200, json=OFF_BARCODE_PAYLOAD)

    mock_transport(monkeypatch, handler)
    assert without_attribution(await food_lookup.search_openfoodfacts_by_code("737628064502")) == {
        "name": "Chocolate protein bar",
        "macros_per_100g": {
            "calories": 364.0, "protein": 36.4, "carbs": 32.7,
            "fat": 14.5, "fiber": 5.5,
        },
        "source": "OpenFoodFacts barcode: 737628064502",
        "serving_size": "1 bar (55 g)",
        "macros_per_serving": {
            "calories": 200.0, "protein": 20.0, "carbs": 18.0,
            "fat": 8.0, "fiber": 3.0,
        },
    }


async def test_barcode_not_found_returns_none(monkeypatch):
    mock_transport(monkeypatch, lambda _request: httpx.Response(
        200, json={"status": 0, "status_verbose": "product not found"}
    ))
    assert await food_lookup.resolve_by_barcode("737628064502") is None


async def test_barcode_network_error_returns_none(monkeypatch):
    def handler(_request):
        raise httpx.ReadTimeout("slow")

    mock_transport(monkeypatch, handler)
    assert await food_lookup.search_openfoodfacts_by_code("737628064502") is None


async def test_openfoodfacts_hit_parses_macros_and_source(monkeypatch):
    def handler(request):
        assert request.method == "GET"
        assert request.url.host == "world.openfoodfacts.org"
        assert request.url.params["search_terms"] == "banana"
        return httpx.Response(200, json={"products": [{
            "code": "4011",
            "product_name": "Bananas, raw",
            "nutriments": {
                "energy-kcal_100g": 89, "proteins_100g": 1.09,
                "carbohydrates_100g": 22.84, "fat_100g": 0.33,
                "fiber_100g": 2.6,
            },
        }]})

    calls = mock_transport(monkeypatch, handler)
    assert without_attribution(await food_lookup.resolve_food("banana")) == BANANA_HIT
    assert len(calls) == 1  # Tavily is never contacted on an OpenFoodFacts hit












async def test_openfoodfacts_resolves_without_tavily(monkeypatch):
    def handler(request):
        assert request.url.host == "world.openfoodfacts.org"
        assert request.url.params["search_terms"] == "rice noodles"
        return httpx.Response(200, json=OFF_PAYLOAD)

    calls = mock_transport(monkeypatch, handler)
    result = await food_lookup.resolve_food("rice noodles")
    assert without_attribution(result) == {
        "name": "Rice noodles",
        "macros_per_100g": {"calories": 355.0, "protein": 7.1, "carbs": 78.6,
                            "fat": 1.2, "fiber": 1.8},
        "source": "OpenFoodFacts: 737628064502",
    }
    assert [c.url.host for c in calls] == ["world.openfoodfacts.org"]


async def test_both_databases_missing_returns_none(monkeypatch):
    def handler(_request):
        return httpx.Response(200, json={"products": []})

    mock_transport(monkeypatch, handler)
    assert await food_lookup.resolve_food("unicorn steak") is None


async def test_openfoodfacts_requires_no_api_key(monkeypatch):
    def handler(request):
        assert request.url.host == "world.openfoodfacts.org"
        return httpx.Response(200, json=OFF_PAYLOAD)

    calls = mock_transport(monkeypatch, handler)
    result = await food_lookup.resolve_food("rice noodles")
    assert result["source"] == "OpenFoodFacts: 737628064502"
    assert [c.url.host for c in calls] == ["world.openfoodfacts.org"]






























@pytest.mark.parametrize("off_body", [
    {"products": [{"code": "1", "product_name": "y",
                   "nutriments": {"energy-kcal_100g": "abc"}}]},
    {"products": [{"nutriments": None}]},
    {},
    {"products": [{"code": "2", "product_name": "y",
                   "nutriments": {"energy-kcal_100g": 99999}}]},
    # Valid macros but no source id must not persist an empty macro_source.
    {"products": [{"code": "  ", "product_name": "Rice noodles",
                   "nutriments": OFF_PAYLOAD["products"][0]["nutriments"]}]},
])
async def test_malformed_payloads_degrade_to_none(monkeypatch, off_body):
    mock_transport(monkeypatch, lambda _request: httpx.Response(200, json=off_body))
    assert await food_lookup.resolve_food("banana") is None


async def test_http_errors_and_non_json_degrade_to_none(monkeypatch):
    mock_transport(monkeypatch, lambda _request: httpx.Response(
        200, text="<html>not json</html>"
    ))
    assert await food_lookup.resolve_food("banana") is None


async def test_blank_query_makes_no_network_calls(monkeypatch):
    calls = mock_transport(monkeypatch, lambda _request: httpx.Response(500))
    assert await food_lookup.resolve_food("   ") is None
    assert calls == []


def test_portion_from_grams_scales_database_macros():
    macros, source = food_lookup.portion_from_grams(BANANA_HIT, 150)
    factor = 150 / 100.0
    assert macros == {key: round(value * factor, 2)
                      for key, value in BANANA_HIT["macros_per_100g"].items()}
    assert source == "OpenFoodFacts: 4011 — Bananas, raw, 150 g"


def test_portion_from_grams_rejects_unusable_input():
    assert food_lookup.portion_from_grams(BANANA_HIT, 0) is None
    assert food_lookup.portion_from_grams(BANANA_HIT, None) is None
    assert food_lookup.portion_from_grams(BANANA_HIT, "abc") is None
    assert food_lookup.portion_from_grams(BANANA_HIT, 5001) is None  # >5 kg
    zero_cal = {**BANANA_HIT, "macros_per_100g": {**BANANA_HIT["macros_per_100g"],
                                                  "calories": 0}}
    assert food_lookup.portion_from_grams(zero_cal, 100) is None
    assert food_lookup.portion_from_grams({"source": "x"}, 100) is None


def test_lookup_food_tool_is_declared_and_prompted_lookup_first():
    tools = {tool["name"]: tool for tool in coach.TOOLS}
    assert tools["lookup_food"]["input_schema"]["required"] == ["query"]
    assert "FIRST" in tools["lookup_food"]["description"]
    # Both the built-in fallback prompt and the SOUL.md persona instruct
    # lookup-first for unknown foods.
    assert "lookup_food" in coach.SYSTEM_PROMPT
    assert "lookup_food" in coach.system_prompt(onboarding=False)


def _openai_response(message):
    return httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        json={"choices": [{"message": message}]},
    )


async def test_parser_unknown_food_calls_lookup_and_stamps_verified_source(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())
    calls = []
    responses = [
        _openai_response({
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "lookup-1", "type": "function",
                "function": {"name": "lookup_food", "arguments": '{"name":"sunchoke"}'},
            }],
        }),
        _openai_response({
            "role": "assistant",
            "content": json.dumps([{
                "name": "sunchoke", "calories": 110, "protein": 3,
                "carbs": 26, "fat": 0, "fiber": 2, "quantity": 1,
                "grams": 150, "basis": "per_100g", "sourced_from": "lookup",
                "meal": "Lunch", "note": "OpenFoodFacts: 170002",
            }]),
        }),
    ]

    async def fake_post(_token, payload):
        calls.append(payload["messages"][:])
        return responses.pop(0)

    async def fake_resolve(name, **_kwargs):
        assert name == "sunchoke"
        return {**BANANA_HIT, "source": "OpenFoodFacts: 170002"}

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)

    items, _, _ = await srv.parse_chat_message("150g sunchoke")

    assert len(calls) == 2
    assert calls[1][-1]["role"] == "tool"
    assert json.loads(calls[1][-1]["content"])["source"] == "OpenFoodFacts: 170002"
    assert items[0]["note"] == "OpenFoodFacts: 170002"


async def test_parser_lookup_miss_forces_estimate_source(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())
    responses = [
        _openai_response({
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "lookup-1", "type": "function",
                "function": {"name": "lookup_food", "arguments": '{"name":"mystery"}'},
            }],
        }),
        _openai_response({
            "role": "assistant",
            "content": '[{"name":"mystery","calories":200,"protein":5,'
                       '"carbs":20,"fat":10,"fiber":1,"sourced_from":"lookup",'
                       '"meal":"Snack","note":"OpenFoodFacts: fabricated"}]',
        }),
    ]

    async def fake_post(_token, _payload):
        return responses.pop(0)

    async def fake_resolve(_name, **_kwargs):
        return None

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)

    items, _, _ = await srv.parse_chat_message("mystery")
    assert items[0]["note"] == "ESTIMATE"


async def test_parser_lookup_source_is_bound_to_the_item_that_triggered_it(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())
    responses = [
        _openai_response({
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "lookup-1", "type": "function",
                "function": {"name": "lookup_food", "arguments": '{"name":"sunchoke"}'},
            }],
        }),
        _openai_response({
            "role": "assistant",
            "content": json.dumps([
                {"name": "sunchoke", "calories": 110, "protein": 3,
                 "carbs": 26, "fat": 0, "fiber": 2, "grams": 150,
                 "basis": "per_100g", "sourced_from": "lookup",
                 "meal": "Lunch", "note": "OpenFoodFacts: 170002"},
                {"name": "mystery stew", "calories": 300, "protein": 12,
                 "carbs": 35, "fat": 12, "fiber": 4,
                 "basis": "per_serving", "sourced_from": "lookup",
                 "meal": "Lunch", "note": "OpenFoodFacts: 170002"},
            ]),
        }),
    ]

    async def fake_post(_token, _payload):
        return responses.pop(0)

    async def fake_resolve(_name, **_kwargs):
        return {**BANANA_HIT, "source": "OpenFoodFacts: 170002"}

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)

    items, _, _ = await srv.parse_chat_message("150g sunchoke and mystery stew")

    assert items[0]["note"] == "OpenFoodFacts: 170002"
    assert items[1]["note"] == "ESTIMATE"


async def test_parser_known_food_skips_lookup(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())
    captured = {}

    async def fake_post(_token, payload):
        captured.update(payload)
        return _openai_response({
            "role": "assistant",
            "content": '[{"name":"Banana","calories":105,"protein":1,'
                       '"carbs":27,"fat":0,"fiber":0,"quantity":1,'
                       '"basis":"per_unit","sourced_from":"known",'
                       '"meal":"Snack","note":"Known food: Banana"}]',
        })

    async def unexpected_resolve(_name):
        pytest.fail("known food must not execute lookup_food")

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", unexpected_resolve)

    items, _, _ = await srv.parse_chat_message("a banana")
    assert captured["model"] == "gpt-5.6-luna"
    assert captured["reasoning_effort"] == "none"
    assert captured["max_completion_tokens"] == 1200
    assert "max_tokens" not in captured
    assert captured["tools"][0]["function"]["name"] == "lookup_food"
    assert items[0]["sourced_from"] == "known"


@pytest.mark.parametrize("name,approved", [
    ("Chipotle burrito", (945, 83, 88, 31)),
    ("CFA 8ct grilled nuggets + grilled club + sauce", (710, 62, 61, 25)),
])
async def test_approved_known_food_macros_survive_chat_to_log_end_to_end(monkeypatch, name, approved):
    fake = FakeStore(plan={"version": 1}, has_targets=True)
    monkeypatch.setattr(srv, "_client", fake)
    captured = {}
    async def fake_parse(_message):
        return [{"name":name, "calories":1, "protein":1, "carbs":1, "fat":1,
                 "fiber":0, "quantity":1, "basis":"per_serving",
                 "sourced_from":"known", "meal":"Lunch", "note":f"Known food: {name}"}], None
    async def fake_write(name, calories, protein, carbs, fat, macro_source, meal,
                         day_value, allow_estimate=False, fiber=0, component_metadata=None):
        captured.update(calories=calories, protein=protein, carbs=carbs, fat=fat,
                        macro_source=macro_source)
        return {"logged":{"id":"known", "name":name, "meal":meal, "calories":calories,
            "protein":protein, "carbs":carbs, "fat":fat, "fiber":fiber}}
    async def fake_day(_day, ensure=None):
        return {"totals":{"calories":captured.get("calories", 0)},
                "targets":{"calories":2000}}
    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "write_meal", fake_write)
    monkeypatch.setattr(srv, "day_payload", fake_day)
    response = await srv.api_chat(chat_request({"message":f"log {name}"}))
    assert response.status_code == 200
    assert tuple(captured[key] for key in ("calories","protein","carbs","fat")) == approved
    assert captured["macro_source"] == f"Known food: {name}"


async def test_parser_lookup_tool_executes_at_most_three_calls(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())
    resolved = []
    tool_calls = [{
        "id": f"lookup-{index}", "type": "function",
        "function": {"name": "lookup_food", "arguments": json.dumps({"name": f"food {index}"})},
    } for index in range(4)]
    responses = [
        _openai_response({"role": "assistant", "content": None, "tool_calls": tool_calls}),
        _openai_response({"role": "assistant", "content": "[]"}),
    ]
    final_payload = {}

    async def fake_post(_token, payload):
        final_payload.update(payload)
        return responses.pop(0)

    async def fake_resolve(name, **_kwargs):
        resolved.append(name)
        return None

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)

    await srv.parse_chat_message("four unknown foods")
    assert resolved == ["food 0", "food 1", "food 2"]
    assert final_payload["tool_choice"] == "none"


async def test_coach_lookup_food_tool_returns_hit_and_not_found(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)

    async def fake_resolve(query, **_kwargs):
        assert query == "rice noodles"
        return {**BANANA_HIT, "attribution": {"provider":"OpenFoodFacts",
            "license":"Open Database License (ODbL) 1.0",
            "attribution_text":"Data from OpenFoodFacts"}}

    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    handler = srv._coach_tool_handlers()["lookup_food"]

    # Fails closed like every other tool: no tenant context, no lookup.
    with pytest.raises(RuntimeError):
        await handler({"query": "rice noodles"})

    token = bind_user(uuid4())
    try:
        hit = await handler({"query": "rice noodles"})
        assert hit["source"] == "OpenFoodFacts: 4011"
        assert hit["attribution"]["attribution_text"] == "Data from OpenFoodFacts"

        async def fake_miss(_query, **_kwargs):
            return None

        monkeypatch.setattr(food_lookup, "resolve_food", fake_miss)
        result = await srv._coach_tool_handlers()["lookup_food"]({"query": "mystery"})
        assert result["result"] == "not found"
    finally:
        reset_user(token)


async def test_food_path_does_not_repeat_parser_lookup(monkeypatch):
    fake = FakeStore(plan={"version": 1}, has_targets=True)
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def fake_parse(_message):
        return [{"name": "sliced banana", "calories": 105, "protein": 2, "carbs": 25,
                 "fat": 1, "fiber": 3, "grams": 150, "meal": "Snack",
                 "note": "ESTIMATE"}], None

    async def fake_resolve(query, **_kwargs):
        assert query == "sliced banana"
        return BANANA_HIT

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0, component_metadata=None):
        seen.update(name=name, calories=calories, protein=protein,
                    macro_source=macro_source, allow_estimate=allow_estimate)
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 105}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(chat_request({"message": "I ate 150g of banana"}))
    assert response.status_code == 200
    assert seen["macro_source"] == "ESTIMATE"
    assert seen["calories"] == 105.0
    assert seen["protein"] == 2.0
    assert seen["allow_estimate"] is True
    payload = json.loads(response.body)
    assert payload["logged"][0]["name"] == "sliced banana"


async def test_food_path_gram_portion_takes_precedence_over_quantity(monkeypatch):
    fake = FakeStore(plan={"version": 1}, has_targets=True)
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def fake_parse(_message):
        return [{"name": "sliced banana", "calories": 105, "protein": 2,
                 "carbs": 25, "fat": 1, "fiber": 3, "quantity": 1,
                 "grams": 100, "meal": "Snack", "note": "ESTIMATE"}], None

    async def fake_resolve(query, *, whole_item=False, **_kwargs):
        assert query == "sliced banana"
        assert whole_item is False
        return BANANA_HIT

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0, component_metadata=None):
        seen.update(calories=calories, protein=protein, carbs=carbs, fat=fat,
                    fiber=fiber, macro_source=macro_source)
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 89}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(chat_request({"message": "1 100g banana"}))
    assert response.status_code == 200
    assert seen == {
        "calories": 105.0, "protein": 2.0, "carbs": 25.0, "fat": 1.0,
        "fiber": 3.0, "macro_source": "ESTIMATE",
    }


async def test_food_path_labels_unsourced_lookup_miss_as_estimate(monkeypatch):
    fake = FakeStore(plan={"version": 1}, has_targets=True)
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}
    lookups = []
    items = [{"name": "mystery smoothie", "calories": 300, "protein": 10,
              "carbs": 40, "fat": 8, "fiber": 2, "grams": 400, "meal": "Snack",
              "note": "ESTIMATE"}]

    async def fake_parse(_message):
        return items, None

    async def fake_miss(query):
        lookups.append(query)
        return None

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0, component_metadata=None):
        seen.update(macro_source=macro_source, calories=calories)
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 300}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_miss)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    # Grams stated but both databases miss: parser macros stay, honestly labeled.
    response = await srv.api_chat(chat_request({"message": "400g smoothie"}))
    assert response.status_code == 200
    assert seen["macro_source"] == "ESTIMATE"
    assert seen["calories"] == 300.0
    assert lookups == []

    # No grams or whole-item intent: nothing can be scaled honestly, so no
    # additional lookup fires.
    items[0].pop("grams")
    response = await srv.api_chat(chat_request({"message": "smoothie"}))
    assert response.status_code == 200
    assert seen["macro_source"] == "ESTIMATE"
    assert lookups == []


async def test_food_path_upgrades_whole_item_from_serving_panel(monkeypatch):
    fake = FakeStore(plan={"version": 1}, has_targets=True)
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}
    lookup_queries = []

    async def fake_parse(_message):
        return [{"name": "Chipotle chicken burrito bowl", "calories": 600,
                 "protein": 40, "carbs": 80, "fat": 20, "fiber": 2,
                 "grams": None, "quantity": 1, "meal": "Lunch",
                 "basis": "per_serving", "sourced_from": "cascade"}], None

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0, component_metadata=None):
        seen.update(calories=calories, macro_source=macro_source)
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 700}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(
        chat_request({"message": "log 1 chipotle chicken burrito bowl"})
    )
    assert response.status_code == 200
    assert seen == {"calories": 600.0, "macro_source": "ESTIMATE"}
    assert lookup_queries == []

    async def fake_parse_multiple(_message):
        return [
            {"name": "Barebells", "calories": 210, "protein": 18,
             "carbs": 22, "fat": 8, "fiber": 2, "quantity": 1,
             "grams": None, "meal": "Snack", "basis": "per_serving",
             "sourced_from": "known"},
            {"name": "cookies", "calories": 180, "protein": 2,
             "carbs": 28, "fat": 8, "fiber": 1, "quantity": 2,
             "grams": None, "meal": "Snack", "basis": "per_unit",
             "sourced_from": "cascade"},
        ], None

    async def fake_resolve_multiple(query, *, whole_item=False, **_kwargs):
        lookup_queries.append((query, whole_item))
        return None

    lookup_queries.clear()
    monkeypatch.setattr(srv, "parse_chat_message", fake_parse_multiple)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve_multiple)

    response = await srv.api_chat(
        chat_request({"message": "log 1 Barebells and 2 cookies"})
    )
    assert response.status_code == 200
    # A routed exact known-food match bypasses lookup. The parser already
    # returned final macros for two cookies, so no one-serving cascade runs.
    assert lookup_queries == []


async def test_whole_item_per_100g_only_keeps_flagged_estimate(monkeypatch):
    async def fake_resolve(_query, **_kwargs):
        return BANANA_HIT

    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    assert await srv._upgrade_estimate(
        "mystery bar", None, whole_item=True
    ) is None


async def test_food_path_derives_sources_from_exact_real_data_matches(monkeypatch):
    fake = FakeStore(plan={"version": 1}, has_targets=True)
    monkeypatch.setattr(srv, "_client", fake)
    seen = []

    async def fake_parse(_message):
        return [
            {"name": "  bareBELLS  ", "calories": 200, "protein": 20, "carbs": 21,
             "fat": 7, "fiber": 0, "quantity": 2, "grams": 300, "meal": "Snack",
             "basis": "per_serving", "sourced_from": "preset"},
            {"name": "Banana", "calories": 210, "protein": 2, "carbs": 54,
             "fat": 0, "fiber": 6, "quantity": 2, "meal": "Snack",
             "basis": "per_unit", "sourced_from": "known"},
            {"name": "sweet potato", "calories": 172, "protein": 3.2,
             "carbs": 40.2, "fat": 0.2, "fiber": 6, "quantity": 1,
             "grams": 200, "meal": "Lunch", "basis": "per_100g",
             "sourced_from": "known"},
            {"name": "93/7 ground beef", "calories": 340, "protein": 44,
             "carbs": 0, "fat": 17.6, "fiber": 0, "quantity": 1,
             "meal": "Dinner", "basis": "per_unit", "sourced_from": "known"},
            {"name": "pizza", "calories": 200, "protein": 8, "carbs": 24,
             "fat": 8, "fiber": 1, "quantity": 1, "meal": "Snack",
             "basis": "per_serving", "sourced_from": "preset"},
            {"name": "pizza", "calories": 200, "protein": 8, "carbs": 24,
             "fat": 8, "fiber": 1, "quantity": 1, "meal": "Snack",
             "basis": "per_serving", "sourced_from": "known"},
            {"name": "Barebells pizza", "calories": 300, "protein": 12,
             "carbs": 30, "fat": 14, "fiber": 1, "quantity": 1,
             "meal": "Snack", "basis": "per_serving",
             "sourced_from": "preset"},
        ], None, [{
            "name": "Barebells", "calories": 210, "protein": 22,
            "carbs": 19, "fat": 8, "fiber": 4,
        }]

    lookup_queries = []

    async def fake_resolve(query, *, whole_item=False, **_kwargs):
        lookup_queries.append((query, whole_item))
        return None

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0, component_metadata=None):
        seen.append((macro_source, calories, protein, carbs, fat, fiber))
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 200}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(chat_request({"message": "a Barebells and a banana"}))
    assert response.status_code == 200
    assert lookup_queries == []  # existing known-food data now wins before providers
    assert len(seen) == 1
    source, calories, protein, carbs, fat, fiber = seen[0]
    assert source.startswith("Composite: ESTIMATE; Known food: Banana")
    assert (calories, protein, carbs, fat, fiber) == (
        1622.0, 97.2, 193.2, 54.8, 15.0
    )


async def test_food_path_weightless_preset_grams_use_cascade(monkeypatch):
    monkeypatch.setattr(
        srv, "_client", FakeStore(plan={"version": 1}, has_targets=True)
    )
    seen = {}

    async def fake_parse(_message):
        return [{
            "name": "chili", "calories": 360, "protein": 24,
            "carbs": 30, "fat": 15, "fiber": 9, "quantity": 1,
            "grams": 100, "basis": "per_100g", "sourced_from": "preset",
            "meal": "Dinner", "note": "Meal Preset: chili",
        }], None, [{
            "name": "chili", "calories": 360, "protein": 24,
            "carbs": 30, "fat": 15, "fiber": 9,
        }]

    async def fake_resolve(name, **_kwargs):
        assert name == "chili"
        return {
            "name": "Chili with beans",
            "macros_per_100g": {
                "calories": 120, "protein": 8, "carbs": 10,
                "fat": 5, "fiber": 3,
            },
            "source": "OpenFoodFacts: 999999",
        }

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0, component_metadata=None):
        seen.update(calories=calories, protein=protein, carbs=carbs, fat=fat,
                    fiber=fiber, macro_source=macro_source)
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 120}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(chat_request({"message": "100g chili"}))

    assert response.status_code == 200
    assert seen == {
        "calories": 120.0, "protein": 8.0, "carbs": 10.0, "fat": 5.0,
        "fiber": 3.0,
        "macro_source": "OpenFoodFacts: 999999 — Chili with beans, 100 g",
    }


# --------------------------------------------------------------------------- #
# Branded + flavor lookup resolution and the deterministic never-refuse path
# --------------------------------------------------------------------------- #

LOVEN_QUERY = "L'oven fresh Cinnamon Raisin bread"

LOVEN_OFF_PAYLOAD = {"products": [{
    "code": "4099100179378",
    "product_name": "Cinnamon Raisin Bread",
    "nutriments": {"energy-kcal_100g": 230, "proteins_100g": 7.7,
                   "carbohydrates_100g": 46.2, "fat_100g": 3.8,
                   "fiber_100g": 3.8},
}]}


def test_query_variants_ladder_covers_brand_flavor_and_generic():
    assert food_lookup._query_variants(LOVEN_QUERY) == [
        LOVEN_QUERY,
        "fresh Cinnamon Raisin bread",
        "Cinnamon Raisin bread",
        "L'oven fresh",
        "bread",
    ]
    assert food_lookup._query_variants("banana") == ["banana"]
    assert food_lookup._query_variants("   ") == []


async def test_branded_flavor_query_resolves_via_generic_variant(monkeypatch):
    # Full brand+flavor phrase misses OpenFoodFacts; the generic variant hits.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request):
        assert request.url.host == "world.openfoodfacts.org"
        if request.url.params["search_terms"].casefold() == "cinnamon raisin bread":
            return httpx.Response(200, json=LOVEN_OFF_PAYLOAD)
        return httpx.Response(200, json={"products": []})

    calls = mock_transport(monkeypatch, handler)
    result = await food_lookup.resolve_food(LOVEN_QUERY)
    assert result is not None
    assert result["source"] == "OpenFoodFacts: 4099100179378"
    assert result["macros_per_100g"]["calories"] == 230.0
    assert set(c.url.params["search_terms"] for c in calls) == set(
        food_lookup._query_variants(LOVEN_QUERY)
    )








async def test_parser_refusal_after_failed_lookup_still_logs_estimate(monkeypatch):
    # The model called lookup_food (proving the message names food), the
    # cascade missed, and the model refused with a label ask anyway. The
    # deterministic backstop logs a flagged estimate instead.
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())
    responses = [
        _openai_response({
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "lookup-1", "type": "function",
                "function": {"name": "lookup_food",
                             "arguments": json.dumps({"name": LOVEN_QUERY})},
            }],
        }),
        _openai_response({
            "role": "assistant",
            "content": "[] I couldn't find that product — could you share "
                       "the nutrition label?",
        }),
    ]

    async def fake_post(_token, _payload):
        return responses.pop(0)

    async def fake_resolve(_name, **_kwargs):
        return None

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)

    items, reply, _ = await srv.parse_chat_message(LOVEN_QUERY)
    assert reply is None
    assert len(items) == 1
    assert items[0]["name"] == LOVEN_QUERY
    assert items[0]["note"] == "ESTIMATE"
    assert items[0]["calories"] == 250.0


async def test_parser_refusal_without_lookup_runs_cascade_then_estimates(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())
    lookups = []

    async def fake_post(_token, _payload):
        return _openai_response({
            "role": "assistant",
            "content": "[] I can't log that without the nutrition label.",
        })

    async def fake_resolve(name, **_kwargs):
        lookups.append(name)
        return None

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)

    items, reply, _ = await srv.parse_chat_message(LOVEN_QUERY)
    assert lookups == [LOVEN_QUERY]
    assert reply is None
    assert items[0]["note"] == "ESTIMATE"
    assert items[0]["calories"] == 250.0


async def test_parser_refusal_after_successful_lookup_logs_lookup_macros(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())
    responses = [
        _openai_response({
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "lookup-1", "type": "function",
                "function": {"name": "lookup_food",
                             "arguments": json.dumps({"name": LOVEN_QUERY})},
            }],
        }),
        _openai_response({
            "role": "assistant",
            "content": "[] Please confirm the serving size on the label first.",
        }),
    ]

    async def fake_post(_token, _payload):
        return responses.pop(0)

    async def fake_resolve(_name, **_kwargs):
        return {
            "name": "L'Oven Fresh Cinnamon Raisin Bread",
            "macros_per_100g": {"calories": 230, "protein": 7.7,
                                "carbs": 46.2, "fat": 3.8, "fiber": 3.8},
            "macros_per_serving": {"calories": 60, "protein": 2,
                                   "carbs": 12, "fat": 1, "fiber": 1},
            "serving_size": "1 slice (26 g)",
            "source": "OpenFoodFacts: 4099100179378",
        }

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)

    items, reply, _ = await srv.parse_chat_message(LOVEN_QUERY)
    assert reply is None
    assert items[0]["sourced_from"] == "lookup"
    assert items[0]["note"] == "OpenFoodFacts: 4099100179378"
    assert items[0]["calories"] == 60.0
    assert items[0]["protein"] == 2.0


async def test_parser_greeting_reply_is_not_forced_into_a_food_entry(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())

    async def fake_post(_token, _payload):
        return _openai_response({
            "role": "assistant",
            "content": "[] Hey Matthew! Ready to crush today.",
        })

    async def unexpected_resolve(_name):
        pytest.fail("a greeting must not trigger the food fallback lookup")

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", unexpected_resolve)

    items, reply, _ = await srv.parse_chat_message("hello")
    assert items == []
    assert reply == "Hey Matthew! Ready to crush today."


SUSHI_MESSAGE = "salmon sashimi, 1 piece of tuna sushi, 3 pieces of white tuna sushi"


async def test_parser_zero_items_for_food_message_forces_estimate(monkeypatch):
    # The model returned zero items with refusal wording that matches no
    # known pattern and never called lookup_food. The backstop no longer
    # cares: a non-greeting, non-question message force-logs regardless of
    # how the model worded its deflection.
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())
    lookups = []

    async def fake_post(_token, _payload):
        return _openai_response({
            "role": "assistant",
            "content": "[] Sushi portions vary a lot between restaurants, so "
                       "reliable entries are hard to pin down.",
        })

    async def fake_resolve(name, **_kwargs):
        lookups.append(name)
        return None

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)

    items, reply, _ = await srv.parse_chat_message(SUSHI_MESSAGE)
    assert lookups == [SUSHI_MESSAGE]
    assert reply is None
    assert len(items) == 1
    assert items[0]["name"] == SUSHI_MESSAGE
    assert items[0]["note"] == "ESTIMATE"
    assert items[0]["sourced_from"] == "estimate"
    assert items[0]["calories"] == 250.0


@pytest.mark.parametrize(
    "message, answer",
    [
        ("what's my plan", "[] Push day: bench, rows, curls."),
        ("how many calories did I eat", "[] You're at 1,200 kcal so far."),
        ("can you undo that", "[] Done — removed the last entry."),
    ],
)
async def test_parser_question_without_question_mark_is_not_forced(
    monkeypatch, message, answer
):
    # Questions and commands often arrive without a "?" — they must pass
    # through as conversation, never force-log as food.
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "test-openai-token")
    monkeypatch.setattr(srv, "_client", FakeStore())

    async def fake_post(_token, _payload):
        return _openai_response({"role": "assistant", "content": answer})

    async def unexpected_resolve(_name):
        pytest.fail("a question must not trigger the food fallback lookup")

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", unexpected_resolve)

    items, reply, _ = await srv.parse_chat_message(message)
    assert items == []
    assert reply == answer.removeprefix("[] ")


async def test_food_path_zero_macro_item_is_rescued_by_lookup(monkeypatch):
    monkeypatch.setattr(
        srv, "_client", FakeStore(plan={"version": 1}, has_targets=True)
    )
    seen = {}

    async def fake_parse(_message):
        return [{"name": LOVEN_QUERY, "calories": 0, "protein": 0,
                 "carbs": 0, "fat": 0, "fiber": 0, "quantity": 1,
                 "grams": None, "meal": "Snack", "sourced_from": "estimate",
                 "note": "ESTIMATE"}], None

    async def fake_resolve(name, **_kwargs):
        assert name == LOVEN_QUERY
        return {
            "name": "L'Oven Fresh Cinnamon Raisin Bread",
            "macros_per_serving": {"calories": 60, "protein": 2,
                                   "carbs": 12, "fat": 1, "fiber": 1},
            "source": "OpenFoodFacts: 4099100179378",
        }

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0, component_metadata=None):
        seen.update(calories=calories, protein=protein, macro_source=macro_source)
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 60}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(chat_request({"message": LOVEN_QUERY}))
    assert response.status_code == 200
    assert seen == {
        "calories": 60.0, "protein": 2.0,
        "macro_source": "OpenFoodFacts: 4099100179378",
    }


async def test_food_path_zero_macro_item_without_lookup_gets_default_estimate(monkeypatch):
    monkeypatch.setattr(
        srv, "_client", FakeStore(plan={"version": 1}, has_targets=True)
    )
    seen = {}

    async def fake_parse(_message):
        return [{"name": "mystery pastry", "calories": 0, "protein": 0,
                 "carbs": 0, "fat": 0, "fiber": 0, "meal": "Snack",
                 "sourced_from": "estimate", "note": "ESTIMATE"}], None

    async def fake_resolve(_name, **_kwargs):
        return None

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0, component_metadata=None):
        seen.update(calories=calories, protein=protein, carbs=carbs, fat=fat,
                    macro_source=macro_source)
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 250}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(chat_request({"message": "mystery pastry"}))
    assert response.status_code == 200
    assert seen == {
        "calories": 250.0, "protein": 10.0, "carbs": 30.0, "fat": 10.0,
        "macro_source": "ESTIMATE",
    }


async def test_food_path_zero_calorie_foods_keep_their_zeros(monkeypatch):
    monkeypatch.setattr(
        srv, "_client", FakeStore(plan={"version": 1}, has_targets=True)
    )
    seen = {}

    async def fake_parse(_message):
        return [{"name": "Diet Coke", "calories": 0, "protein": 0,
                 "carbs": 0, "fat": 0, "fiber": 0, "meal": "Snack",
                 "sourced_from": "estimate", "note": "ESTIMATE"}], None

    async def unexpected_resolve(_name):
        pytest.fail("zero-calorie foods must not trigger the rescue lookup")

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0, component_metadata=None):
        seen.update(calories=calories, macro_source=macro_source)
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 0}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(food_lookup, "resolve_food", unexpected_resolve)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(chat_request({"message": "a diet coke"}))
    assert response.status_code == 200
    assert seen == {"calories": 0.0, "macro_source": "ESTIMATE"}


# --------------------------------------------------------------------------- #
# Composite voice-query resolution: normalize, split, quantify, sum once
# (voice-first-utterance-fix-20260912)
# --------------------------------------------------------------------------- #

def _fatsecret_search_hit(food_id, food_name):
    return {"foods": {"food": {"food_id": food_id, "food_name": food_name}}}


def _fatsecret_get_hit(food_name, *, description, grams, calories, protein, carbs, fat, fiber):
    return {"food": {"food_name": food_name, "servings": {"serving": {
        "serving_description": description, "metric_serving_amount": str(grams),
        "metric_serving_unit": "g", "calories": str(calories), "protein": str(protein),
        "carbohydrate": str(carbs), "fat": str(fat), "fiber": str(fiber),
    }}}}


def _enable_fatsecret(monkeypatch):
    monkeypatch.setenv("FATSECRET_ATTRIBUTION_ENABLED", "true")
    monkeypatch.setenv("FATSECRET_CONSUMER_KEY", "consumer")
    monkeypatch.setenv("FATSECRET_CONSUMER_SECRET", "consumer-secret")
    monkeypatch.delenv("FATSECRET_CLIENT_ID", raising=False)
    monkeypatch.delenv("FATSECRET_CLIENT_SECRET", raising=False)


def test_relevance_check_never_lets_a_bare_unit_match_an_unrelated_brand():
    # Regression for the honey-for-ground-beef bug: the stray "oz" variant a
    # naive query-window slicer used to generate matched "Oz Miel" (a honey
    # brand) purely because "oz" is a substring token of "Oz". A unit word
    # must never count as meaningful overlap.
    assert not food_lookup._relevant("6 oz", {"food_name": "Oz Miel"})
    assert food_lookup._relevant("ground beef", {"food_name": "Ground Beef 93/7"})


async def test_query_normalization_strips_quantity_parens_and_hedge_before_search(monkeypatch):
    monkeypatch.delenv("FATSECRET_ATTRIBUTION_ENABLED", raising=False)
    calls = mock_transport(monkeypatch, lambda _request: httpx.Response(200, json={"products": []}))
    await food_lookup.resolve_food("6 oz ground beef (93/7, estimated)")
    searched = [c.url.params["search_terms"] for c in calls]
    assert "ground beef 93/7" in searched
    assert not any(term in ("oz", "estimated)", "6 oz") for term in searched)


async def test_honey_fixture_can_never_resolve_for_a_ground_beef_query(monkeypatch):
    # Even a maximally noisy provider that returns the honey product for
    # every search must be rejected by the deterministic relevance filter —
    # this is not supposed to depend on the provider behaving well.
    monkeypatch.delenv("FATSECRET_ATTRIBUTION_ENABLED", raising=False)

    def handler(_request):
        return httpx.Response(200, json={"products": [{
            "code": "9999", "product_name": "Oz Miel",
            "nutriments": {"energy-kcal_100g": 381, "proteins_100g": 0.3,
                           "carbohydrates_100g": 80, "fat_100g": 0, "fiber_100g": 0},
        }]})

    mock_transport(monkeypatch, handler)
    result = await food_lookup.resolve_food("6 oz ground beef (93/7, estimated)")
    assert result is None  # never silently becomes honey


async def test_short_beef_variant_cannot_accept_bouillon(monkeypatch):
    monkeypatch.delenv("FATSECRET_ATTRIBUTION_ENABLED", raising=False)

    def handler(_request):
        return httpx.Response(200, json={"products": [{
            "code": "bouillon", "product_name": "Cube De Bouillon Boeuf Beef",
            "nutriments": {"energy-kcal_100g": 210, "proteins_100g": 9,
                           "carbohydrates_100g": 20, "fat_100g": 10, "fiber_100g": 0},
        }]})

    mock_transport(monkeypatch, handler)
    assert await food_lookup.resolve_food("93/7 ground beef") is None

    assert not food_lookup._full_query_relevant(
        "93/7 ground beef", {"food_name": "93/7 Beef"}
    )


async def test_short_variant_accepts_legitimate_branded_flavor_match(monkeypatch):
    monkeypatch.delenv("FATSECRET_ATTRIBUTION_ENABLED", raising=False)

    def handler(request):
        if request.url.params["search_terms"].casefold() == "cinnamon raisin bread":
            return httpx.Response(200, json=LOVEN_OFF_PAYLOAD)
        return httpx.Response(200, json={"products": []})

    mock_transport(monkeypatch, handler)
    assert await food_lookup.resolve_food(LOVEN_QUERY) is not None


async def test_clean_ground_beef_query_is_unchanged_and_six_ounces_scales_to_anchor(monkeypatch):
    _enable_fatsecret(monkeypatch)

    async def fake_request(data):
        if data.get("method") == "foods.search":
            return _fatsecret_search_hit("111", "93/7 Ground Beef")
        assert data.get("food_id") == "111"
        return _fatsecret_get_hit(
            "93/7 Ground Beef", description="4 oz", grams=113.4,
            calories=170, protein=23, carbs=0, fat=8, fiber=0,
        )

    monkeypatch.setattr(food_lookup, "_fatsecret_request", fake_request)

    clean = await food_lookup.resolve_food("93/7 ground beef")
    assert clean["source"] == "FatSecret: 111"
    assert clean["macros_per_serving"] == {
        "calories": 170.0, "protein": 23.0, "carbs": 0.0, "fat": 8.0, "fiber": 0.0,
    }

    scaled = await food_lookup.resolve_food("6 oz 93/7 ground beef")
    macros = scaled["macros_per_serving"]
    assert macros["calories"] == pytest.approx(255, abs=3)
    assert macros["protein"] == pytest.approx(34.5, abs=1)


async def test_composite_voice_query_resolves_each_component_and_sums_once(monkeypatch):
    _enable_fatsecret(monkeypatch)

    async def fake_request(data):
        if data.get("method") == "foods.search":
            expr = str(data.get("search_expression") or "").casefold()
            if "beef" in expr:
                return _fatsecret_search_hit("111", "93/7 Ground Beef")
            if "potato" in expr:
                return _fatsecret_search_hit("222", "Sweet Potato")
            return {"foods": {}}
        if data.get("food_id") == "111":
            return _fatsecret_get_hit(
                "93/7 Ground Beef", description="4 oz", grams=113.4,
                calories=170, protein=23, carbs=0, fat=8, fiber=0,
            )
        return _fatsecret_get_hit(
            "Sweet Potato", description="1 medium", grams=130,
            calories=112, protein=2, carbs=26, fat=0, fiber=4,
        )

    monkeypatch.setattr(food_lookup, "_fatsecret_request", fake_request)

    result = await food_lookup.resolve_food(
        "6 ounces 93/7 ground beef and 1 medium sweet potato"
    )
    assert result is not None
    assert "FatSecret: 111" in result["source"]
    assert "FatSecret: 222" in result["source"]
    assert "UNRESOLVED" not in result["source"]
    macros = result["macros_per_serving"]
    # 6 oz ground beef (~255 cal / ~34.5 g protein) + 1 medium sweet potato (112 cal / 2 g)
    assert macros["calories"] == pytest.approx(255 + 112, abs=3)
    assert macros["protein"] == pytest.approx(34.5 + 2, abs=1)


async def test_asr_ratio_composite_resolves_and_keeps_canonical_label(monkeypatch):
    _enable_fatsecret(monkeypatch)

    async def fake_request(data):
        if data.get("method") == "foods.search":
            expression = str(data.get("search_expression") or "").casefold()
            if expression == "93/7 ground beef":
                return _fatsecret_search_hit("111", "93/7 Ground Beef")
            if expression == "sweet potato":
                return _fatsecret_search_hit("222", "Sweet Potato")
            return {"foods": {}}
        if data.get("food_id") == "111":
            return _fatsecret_get_hit(
                "93/7 Ground Beef", description="4 oz", grams=113.4,
                calories=170, protein=23, carbs=0, fat=8, fiber=0,
            )
        return _fatsecret_get_hit(
            "Sweet Potato", description="100 g", grams=100,
            calories=86, protein=1.6, carbs=20.1, fat=0.1, fiber=3,
        )

    monkeypatch.setattr(food_lookup, "_fatsecret_request", fake_request)
    result = await food_lookup.resolve_food(
        "6 ounces of 93 7 ground beef and 50 grams of sweet potato"
    )
    assert result is not None
    assert result["name"] == "6 ounces of 93/7 ground beef and 50 grams of sweet potato"
    assert "FatSecret: 111" in result["source"]
    assert "FatSecret: 222" in result["source"]
    assert "UNRESOLVED" not in result["source"]


@pytest.mark.parametrize("query", ["mac and cheese", "fish and chips"])
async def test_named_and_foods_resolve_as_one_food(monkeypatch, query):
    calls = []

    async def catalog(name):
        calls.append(name)
        return {"name": name, "macros_per_serving": {
            "calories": 500, "protein": 20, "carbs": 50, "fat": 20, "fiber": 3,
        }, "source": "Catalog: compound food"} if name == query else None

    result = await food_lookup.resolve_food(query, catalog_lookup=catalog)
    assert result["name"] == query
    assert result["macros_per_serving"]["calories"] == 500
    assert calls == [query]


async def test_unresolved_split_falls_back_to_whole_food(monkeypatch):
    calls = []

    async def no_provider(_query): return None
    monkeypatch.setattr(food_lookup, "search_fatsecret", no_provider)
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", no_provider)

    async def catalog(name):
        calls.append(name)
        if name == "peanut butter and jelly":
            return {"name": name, "macros_per_serving": {
                "calories": 400, "protein": 12, "carbs": 45, "fat": 20, "fiber": 4,
            }, "source": "Catalog: sandwich"}
        return None

    result = await food_lookup.resolve_food(
        "peanut butter and jelly", catalog_lookup=catalog
    )
    assert result["name"] == "peanut butter and jelly"
    assert result["macros_per_serving"]["calories"] == 400
    assert calls[-1] == "peanut butter and jelly"


@pytest.mark.parametrize(("query", "quantity", "unit", "identity"), [
    ("half a cup of rice", 0.5, "cup", "rice"),
    ("2 1/2 cups rice", 2.5, "cups", "rice"),
    ("1 1/2 tbsp olive oil", 1.5, "tbsp", "olive oil"),
    ("2 and a half cups rice", 2.5, "cups", "rice"),
    ("a quarter cup rice", 0.25, "cup", "rice"),
])
def test_spoken_fractional_portions_leave_clean_search_identity(query, quantity, unit, identity):
    assert food_lookup._clean_component(query) == (quantity, unit, identity)


@pytest.mark.parametrize("query", [
    "93/7 ground beef",
    "93 7 ground beef",
    "93-7 ground beef",
    ".93 7 ground beef",
    "93 / 7 ground beef",
    "ninety three seven ground beef",
    "ninety-three seven ground beef",
])
def test_ground_beef_ratio_normalization(query):
    assert food_lookup._clean_component(query) == (None, None, "93/7 ground beef")


@pytest.mark.parametrize(("query", "expected"), [
    ("80 20 ground beef", "80/20 ground beef"),
    ("85 15 ground beef", "85/15 ground beef"),
    ("90 10 ground beef", "90/10 ground beef"),
    ("96 4 ground beef", "96/4 ground beef"),
])
def test_common_ground_beef_ratios_normalize(query, expected):
    assert food_lookup._clean_component(query) == (None, None, expected)


def test_mixed_fraction_is_not_normalized_as_a_ratio():
    assert food_lookup._clean_component("2 1/2 cups white rice") == (
        2.5, "cups", "white rice"
    )


async def test_spoken_ratio_resolves_against_full_normalized_identity(monkeypatch):
    _enable_fatsecret(monkeypatch)

    async def fake_request(data):
        if data.get("method") == "foods.search":
            assert data["search_expression"] == "93/7 ground beef"
            return _fatsecret_search_hit("111", "93/7 Ground Beef")
        return _fatsecret_get_hit(
            "93/7 Ground Beef", description="4 oz", grams=113.4,
            calories=170, protein=23, carbs=0, fat=8, fiber=0,
        )

    monkeypatch.setattr(food_lookup, "_fatsecret_request", fake_request)
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", lambda _query: asyncio.sleep(0, result=None))
    found = await food_lookup.resolve_food("ninety three seven ground beef")
    assert found["name"] == "93/7 Ground Beef"
    assert found["source"] == "FatSecret: 111"


@pytest.mark.parametrize(("query", "factor"), [
    ("half a cup of rice", 0.5 * 236.588 / 100),
    ("2 1/2 cups rice", 2.5 * 236.588 / 100),
    ("1 1/2 tbsp olive oil", 1.5 * 14.787 / 100),
])
async def test_spoken_fractional_portions_scale_macros(monkeypatch, query, factor):
    identity = "olive oil" if "oil" in query else "rice"

    async def catalog(name):
        assert name == identity
        return {"name": identity, "macros_per_100g": {
            "calories": 100, "protein": 10, "carbs": 10, "fat": 10, "fiber": 10,
        }, "source": "Catalog: test"}

    result = await food_lookup.resolve_food(query, catalog_lookup=catalog)
    assert result["macros_per_serving"]["calories"] == pytest.approx(100 * factor, abs=0.01)


async def test_composite_with_one_unresolved_component_sums_the_rest_and_flags_it(monkeypatch):
    monkeypatch.delenv("FATSECRET_ATTRIBUTION_ENABLED", raising=False)

    def handler(request):
        terms = request.url.params.get("search_terms", "").casefold()
        if "banana" in terms:
            return httpx.Response(200, json={"products": [{
                "code": "4011", "product_name": "Bananas, raw",
                "nutriments": {"energy-kcal_100g": 89, "proteins_100g": 1.09,
                               "carbohydrates_100g": 22.84, "fat_100g": 0.33, "fiber_100g": 2.6},
            }]})
        return httpx.Response(200, json={"products": []})

    mock_transport(monkeypatch, handler)
    result = await food_lookup.resolve_food("1 medium banana and some unobtainium paste")
    assert result is not None
    assert "UNRESOLVED: some unobtainium paste" in result["source"]
    assert result["macros_per_serving"]["calories"] > 0


def test_split_components_respects_parenthetical_boundaries():
    assert food_lookup._split_components(
        "6 oz ground beef (93/7, estimated) and 1 medium sweet potato"
    ) == ["6 oz ground beef (93/7, estimated)", "1 medium sweet potato"]
    assert food_lookup._split_components("93/7 ground beef") == ["93/7 ground beef"]


def test_extract_quantity_keeps_mid_string_fraction_but_strips_leading_unit():
    assert food_lookup._extract_quantity("93/7 ground beef") == (None, None, "93/7 ground beef")
    assert food_lookup._extract_quantity("6 oz ground beef") == (6.0, "oz", "ground beef")
    assert food_lookup._extract_quantity("1 medium sweet potato") == (1.0, "medium", "sweet potato")


async def test_saved_preset_resolves_before_providers(monkeypatch):
    class PresetStore:
        async def fetch_presets(self):
            return [{"name": "Fairlife 30g Shake", "calories": 150, "protein": 30,
                     "carbs": 3, "fat": 2.5, "fiber": 0}]

        async def lookup_catalog(self, _query):
            return None

    async def forbidden(_query):
        raise AssertionError("external provider must not be called")

    monkeypatch.setattr(srv, "_client", PresetStore())
    monkeypatch.setattr(food_lookup, "search_fatsecret", forbidden)
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", forbidden)
    srv._resolved_food_cache.clear()
    token = bind_user(uuid4())
    try:
        result = await srv.resolve_food("fairlife shake")
    finally:
        reset_user(token)
    assert result["name"] == "Fairlife 30g Shake"
    assert result["macros_per_serving"]["protein"] == 30


@pytest.mark.parametrize("source", ["known", "catalog"])
async def test_other_local_food_sources_short_circuit_providers(monkeypatch, source):
    catalog_hit = {"name": "local oats", "macros_per_serving": {
        "calories": 100, "protein": 4, "carbs": 18, "fat": 2, "fiber": 3,
    }, "source": "Catalog: local oats"}

    class LocalStore:
        async def fetch_presets(self): return []
        async def lookup_catalog(self, query):
            return catalog_hit if source == "catalog" and query == "local oats" else None

    async def forbidden(_query):
        raise AssertionError("external provider must not be called")

    monkeypatch.setattr(srv, "_client", LocalStore())
    monkeypatch.setattr(food_lookup, "search_fatsecret", forbidden)
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", forbidden)
    srv._resolved_food_cache.clear()
    token = bind_user(uuid4())
    try:
        result = await srv.resolve_food("Barebells" if source == "known" else "local oats")
    finally:
        reset_user(token)
    assert result is not None


async def test_provider_searches_start_concurrently_and_prefer_higher_trust(monkeypatch):
    _enable_fatsecret(monkeypatch)
    both_started = asyncio.Event()
    started = set()

    async def result(provider, query):
        started.add(provider)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.2)
        return {"name": "Ground Beef 93/7", "macros_per_serving": {
            "calories": 170, "protein": 23, "carbs": 0, "fat": 8, "fiber": 0,
        }, "source": f"{provider}: {query}"}

    monkeypatch.setattr(food_lookup, "search_fatsecret", lambda query: result("FatSecret", query))
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", lambda query: result("OpenFoodFacts", query))
    found = await food_lookup.resolve_food("93/7 ground beef")
    assert started == {"FatSecret", "OpenFoodFacts"}
    assert found["source"].startswith("FatSecret:")


async def test_slow_higher_trust_provider_does_not_hold_fast_valid_result(monkeypatch):
    _enable_fatsecret(monkeypatch)

    async def slow_fatsecret(_query):
        await asyncio.sleep(1)
        return None

    async def fast_openfoodfacts(_query):
        await asyncio.sleep(0.01)
        return {"name": "Ground Beef 93/7", "macros_per_serving": {
            "calories": 170, "protein": 23, "carbs": 0, "fat": 8, "fiber": 0,
        }, "source": "OpenFoodFacts: fast"}

    monkeypatch.setattr(food_lookup, "search_fatsecret", slow_fatsecret)
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", fast_openfoodfacts)
    started = time.monotonic()
    found = await food_lookup.resolve_food("93/7 ground beef")
    elapsed = time.monotonic() - started
    assert found["source"] == "OpenFoodFacts: fast"
    assert elapsed < 0.4


@pytest.mark.parametrize(("spoken", "normalized"), [
    ("a hundred grams of sweet potato", "100 grams of sweet potato"),
    ("two hundred grams of sweet potato", "200 grams of sweet potato"),
    ("a hundred and fifty grams of sweet potato", "150 grams of sweet potato"),
    ("twenty grams of strawberries", "20 grams of strawberries"),
    ("one thousand grams of sweet potato", "1000 grams of sweet potato"),
])
def test_spelled_out_quantities_normalize_before_unit_stripping(spoken, normalized):
    assert food_lookup._normalize_query_text(spoken) == normalized


@pytest.mark.parametrize("query", [
    "six ounces of 93/7 ground beef",
    "6 oz 93 7 ground beef",
    "ninety three seven ground beef, six ounces",
    "a hundred grams of sweet potato",
    "100 grams of sweet potato",
    "sweet potato",
    "one sweet potato",
    "8 oz steak",
    "an apple",
    "strawberries",
    "2 eggs",
])
async def test_generic_whole_food_acceptance_never_calls_a_provider(monkeypatch, query):
    async def forbidden(_query):
        raise AssertionError("generic whole food must not call a provider")
    monkeypatch.setattr(food_lookup, "search_fatsecret", forbidden)
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", forbidden)
    found = food_lookup.resolve_generic_whole_food(query)
    assert found is not None
    assert found["source"].startswith("Generic:")
    assert found["basis"]
    assert found["macros_per_serving"]["calories"] > 0


def test_generic_whole_food_does_not_capture_brands_composites_or_mixed_fractions():
    for query in ("Big Mac", "Chick-Fil-A sandwich", "Quaker oats"):
        assert food_lookup.resolve_generic_whole_food(query) is None
    rice = food_lookup.resolve_generic_whole_food("2 1/2 cups white rice")
    assert rice is not None
    assert food_lookup._clean_component("2 1/2 cups white rice")[:2] == (2.5, "cups")


async def test_resolved_cache_is_ttl_bounded_and_tenant_keyed(monkeypatch):
    calls = 0

    class EmptyStore:
        async def fetch_presets(self): return []
        async def lookup_catalog(self, _query): return None

    async def provider(_query, **_kwargs):
        nonlocal calls
        calls += 1
        return dict(BANANA_HIT)

    monkeypatch.setattr(srv, "_client", EmptyStore())
    monkeypatch.setattr(food_lookup, "resolve_food", provider)
    monkeypatch.setattr(srv, "_RESOLVED_CACHE_MAX", 2)
    srv._resolved_food_cache.clear()
    first_tenant, second_tenant = uuid4(), uuid4()
    token = bind_user(first_tenant)
    try:
        await srv.resolve_food("food one")
        await srv.resolve_food("food one")
        assert calls == 1
        key = next(iter(srv._resolved_food_cache))
        timestamp, value = srv._resolved_food_cache[key]
        srv._resolved_food_cache[key] = (timestamp - srv._RESOLVED_CACHE_TTL_SECONDS - 1, value)
        await srv.resolve_food("food one")
        assert calls == 2
        await srv.resolve_food("food two")
        await srv.resolve_food("food three")
        assert len(srv._resolved_food_cache) == 2
    finally:
        reset_user(token)
    token = bind_user(second_tenant)
    try:
        await srv.resolve_food("food three")
    finally:
        reset_user(token)
    assert calls == 5
