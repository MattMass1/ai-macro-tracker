"""Network-free tests for the free food-database lookup cascade."""
import json
import os
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

USDA_PAYLOAD = {"foods": [{
    "fdcId": 171705,
    "description": "Bananas, raw",
    "foodNutrients": [
        {"nutrientId": 1008, "value": 89.0},
        {"nutrientId": 1003, "value": 1.09},
        {"nutrientId": 1005, "value": 22.84},
        {"nutrientId": 1004, "value": 0.33},
        {"nutrientId": 1079, "value": 2.6},
    ],
}]}

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
    "source": "USDA FDC: 171705",
}


@pytest.fixture(autouse=True)
def disable_live_food_classifier(monkeypatch):
    """Keep cascade tests network-free unless they explicitly mock classification."""
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)


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
    assert await food_lookup.search_openfoodfacts_by_code("737628064502") == {
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


async def test_usda_hit_parses_macros_and_source(monkeypatch):
    monkeypatch.setenv("USDA_API_KEY", "demo-key")

    def handler(request):
        assert request.method == "GET"
        assert request.url.host == "api.nal.usda.gov"
        assert request.url.params["api_key"] == "demo-key"
        assert request.url.params["query"] == "banana"
        assert request.url.params["dataType"] == "Foundation,SR Legacy"
        return httpx.Response(200, json=USDA_PAYLOAD)

    calls = mock_transport(monkeypatch, handler)
    assert await food_lookup.resolve_food("banana") == BANANA_HIT
    assert len(calls) == 1  # OpenFoodFacts is never contacted on a USDA hit


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        ("whole", ["usda"]),
        ("branded", ["off"]),
        ("restaurant", ["tavily"]),
        ("unknown", ["usda"]),
    ],
)
async def test_classifier_routes_matching_tier_first(monkeypatch, classification, expected):
    calls = []

    async def fake_classify(_query):
        return classification

    def search(name):
        async def fake(_query):
            calls.append(name)
            return BANANA_HIT
        return fake

    monkeypatch.setattr(food_lookup, "classify_food", fake_classify)
    monkeypatch.setattr(food_lookup, "search_usda", search("usda"))
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", search("off"))
    monkeypatch.setattr(food_lookup, "search_tavily", search("tavily"))

    assert await food_lookup.resolve_food("food") == BANANA_HIT
    assert calls == expected


async def test_classifier_without_token_degrades_to_unknown(monkeypatch):
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)
    assert await food_lookup.classify_food("Nutella") == "unknown"


async def test_classified_first_tier_miss_runs_complete_fallback(monkeypatch):
    calls = []

    async def fake_classify(_query):
        return "restaurant"

    def search(name, result=None):
        async def fake(_query):
            calls.append(name)
            return result
        return fake

    monkeypatch.setattr(food_lookup, "classify_food", fake_classify)
    monkeypatch.setattr(food_lookup, "search_usda", search("usda"))
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", search("off", BANANA_HIT))
    monkeypatch.setattr(food_lookup, "search_tavily", search("tavily"))

    assert await food_lookup.resolve_food("food") == BANANA_HIT
    assert calls == ["tavily", "usda", "off"]


async def test_classification_can_be_disabled(monkeypatch):
    calls = []

    async def unexpected(_query):
        raise AssertionError("classifier should not run")

    def search(name, result=None):
        async def fake(_query):
            calls.append(name)
            return result
        return fake

    monkeypatch.setattr(food_lookup, "classify_food", unexpected)
    monkeypatch.setattr(food_lookup, "search_usda", search("usda"))
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", search("off", BANANA_HIT))
    monkeypatch.setattr(food_lookup, "search_tavily", search("tavily"))

    assert await food_lookup.resolve_food("food", classify=False) == BANANA_HIT
    assert calls == ["usda", "off"]


async def test_classifier_failure_uses_original_order(monkeypatch):
    calls = []

    async def failed_classify(_query):
        raise httpx.ReadTimeout("slow")

    def search(name, result=None):
        async def fake(_query):
            calls.append(name)
            return result
        return fake

    monkeypatch.setattr(food_lookup, "classify_food", failed_classify)
    monkeypatch.setattr(food_lookup, "search_usda", search("usda"))
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", search("off", BANANA_HIT))
    monkeypatch.setattr(food_lookup, "search_tavily", search("tavily"))

    assert await food_lookup.resolve_food("food") == BANANA_HIT
    assert calls == ["usda", "off"]


async def test_usda_miss_falls_through_to_openfoodfacts(monkeypatch):
    monkeypatch.setenv("USDA_API_KEY", "demo-key")

    def handler(request):
        if request.url.host == "api.nal.usda.gov":
            return httpx.Response(200, json={"foods": []})
        assert request.url.host == "world.openfoodfacts.org"
        assert request.url.params["search_terms"] == "rice noodles"
        return httpx.Response(200, json=OFF_PAYLOAD)

    calls = mock_transport(monkeypatch, handler)
    result = await food_lookup.resolve_food("rice noodles")
    assert result == {
        "name": "Rice noodles",
        "macros_per_100g": {"calories": 355.0, "protein": 7.1, "carbs": 78.6,
                            "fat": 1.2, "fiber": 1.8},
        "source": "OpenFoodFacts: 737628064502",
    }
    assert [c.url.host for c in calls] == ["api.nal.usda.gov", "world.openfoodfacts.org"]


async def test_both_databases_missing_returns_none(monkeypatch):
    monkeypatch.setenv("USDA_API_KEY", "demo-key")

    def handler(request):
        if request.url.host == "api.nal.usda.gov":
            return httpx.Response(200, json={"foods": []})
        return httpx.Response(200, json={"products": []})

    mock_transport(monkeypatch, handler)
    assert await food_lookup.resolve_food("unicorn steak") is None


async def test_missing_usda_key_skips_straight_to_openfoodfacts(monkeypatch):
    monkeypatch.delenv("USDA_API_KEY", raising=False)

    def handler(request):
        assert request.url.host == "world.openfoodfacts.org"
        return httpx.Response(200, json=OFF_PAYLOAD)

    calls = mock_transport(monkeypatch, handler)
    result = await food_lookup.resolve_food("rice noodles")
    assert result["source"] == "OpenFoodFacts: 737628064502"
    assert [c.url.host for c in calls] == ["world.openfoodfacts.org"]


async def test_tavily_clean_nutrition_hit_parses_macros(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handler(request):
        assert request.method == "POST"
        assert request.url == httpx.URL(food_lookup.TAVILY_SEARCH_URL)
        payload = json.loads(request.content)
        assert payload == {
            "api_key": "tavily-key",
            "query": "McDonald's grilled chicken sandwich",
            "search_depth": "basic",
            "max_results": 3,
            "include_answer": False,
        }
        return httpx.Response(200, json={"results": [{
            "title": "McDonald's Grilled Chicken Sandwich Nutrition",
            "url": "https://example.com/mcdonalds-chicken",
            "content": (
                "Per 100g: Calories 220, Protein 18g, Carbohydrates 22g, "
                "Fat 7.5g, Fiber 1.5g."
            ),
        }]})

    mock_transport(monkeypatch, handler)
    assert await food_lookup.search_tavily("McDonald's grilled chicken sandwich") == {
        "name": "McDonald's Grilled Chicken Sandwich Nutrition",
        "macros_per_100g": {
            "calories": 220.0,
            "protein": 18.0,
            "carbs": 22.0,
            "fat": 7.5,
            "fiber": 1.5,
        },
        "source": "Tavily: https://example.com/mcdonalds-chicken",
    }


def test_tavily_per_bar_panel_is_normalized_to_100g():
    result = food_lookup._tavily_result("Barebells protein bar", {
        "title": "Barebells Protein Bar Nutrition",
        "url": "https://example.com/barebells",
        "content": (
            "Per 55g bar: Calories 200, Protein 20g, Carbs 18g, "
            "Fat 8g, Fiber 3g."
        ),
    })

    assert result is not None
    assert result["macros_per_100g"]["calories"] == pytest.approx(363.64)
    macros, _source = food_lookup.portion_from_grams(result, 55)
    assert macros["calories"] == pytest.approx(200.0, abs=0.01)


def test_tavily_us_serving_size_with_weight_is_normalized_to_100g():
    result = food_lookup._tavily_result("55g Barebells protein bar", {
        "title": "Barebells Protein Bar Nutrition",
        "url": "https://example.com/barebells-us",
        "content": (
            "Serving Size 1 bar (55g), Calories 200, Protein 20g, "
            "Carbs 18g, Fat 8g, Fiber 3g."
        ),
    })

    assert result is not None
    assert result["serving_size"] == "1 bar (55g)"
    assert result["macros_per_serving"]["calories"] == 200.0
    macros, _source = food_lookup.portion_from_grams(result, 55)
    assert macros["calories"] == pytest.approx(200.0, abs=0.01)


def test_tavily_us_weightless_serving_supports_only_whole_item_query():
    panel = {
        "title": "Barebells Nutrition",
        "url": "https://example.com/barebells-us",
        "content": (
            "Serving: 1 bar, Calories 200, Protein 20g, Carbs 18g, "
            "Fat 8g, Fiber 3g."
        ),
    }
    result = food_lookup._tavily_result("log 1 Barebells", panel)

    assert result is not None
    assert result["basis"] == "serving"
    assert result["serving"] == "1 bar"
    assert result["macros_per_serving"]["calories"] == 200.0
    assert food_lookup.portion_from_grams(result, 55) is None
    assert food_lookup._tavily_result("log one Barebells", panel) is not None
    assert food_lookup._tavily_result("log 1/2 Barebells", panel) is None
    assert food_lookup._tavily_result("log 1.5 Barebells", panel) is None

    assert food_lookup._tavily_result("55g Barebells", {
        "title": "Barebells Nutrition",
        "content": (
            "Serving Size 1 bar, Calories 200, Protein 20g, Carbs 18g, "
            "Fat 8g, Fiber 3g."
        ),
    }) is None


def test_tavily_dual_column_panel_keeps_values_with_item_basis():
    result = food_lookup._tavily_result("Barebells protein bar", {
        "title": "Barebells Protein Bar Nutrition",
        "url": "https://example.com/barebells",
        "content": (
            "Per 55g bar: Calories 200, Protein 20g, Carbs 18g, "
            "Fat 8g, Fiber 3g. Per 100g: Calories 364, Protein 36.4g, "
            "Carbs 32.7g, Fat 14.5g, Fiber 5.5g."
        ),
    })

    assert result is not None
    macros, _source = food_lookup.portion_from_grams(result, 55)
    assert macros["calories"] == pytest.approx(200.0, abs=0.01)


def test_tavily_conflicting_dual_column_panel_returns_none():
    assert food_lookup._tavily_result("Barebells protein bar", {
        "title": "Barebells Protein Bar Nutrition",
        "content": (
            "Per 55g bar: Calories 200, Protein 20g, Carbs 18g, "
            "Fat 8g, Fiber 3g. Per 100g: Calories 500, Protein 36.4g, "
            "Carbs 32.7g, Fat 14.5g, Fiber 5.5g."
        ),
    }) is None


def test_tavily_per_serving_without_weight_returns_none():
    assert food_lookup._tavily_result("Barebells protein bar", {
        "title": "Barebells Protein Bar Nutrition",
        "content": (
            "Per serving: Calories 200, Protein 20g, Carbs 18g, "
            "Fat 8g, Fiber 3g."
        ),
    }) is None


def test_tavily_per_100g_panel_is_accepted_as_is():
    result = food_lookup._tavily_result("Greek yogurt", {
        "title": "Greek Yogurt Nutrition",
        "url": "https://example.com/yogurt",
        "content": (
            "Per 100 grams: Calories 97, Protein 9g, Carbs 4g, "
            "Fat 5g, Fiber 0g."
        ),
    })

    assert result is not None
    assert result["macros_per_100g"] == {
        "calories": 97.0, "protein": 9.0, "carbs": 4.0,
        "fat": 5.0, "fiber": 0.0,
    }


def test_tavily_ambiguous_panel_without_basis_returns_none():
    assert food_lookup._tavily_result("Niche Cafe Bowl", {
        "title": "Niche Cafe Bowl nutrition",
        "content": "Calories: 310 Protein: 20g Carbs: 35g Fat: 9g Fiber: 6g",
    }) is None


async def test_tavily_result_without_nutrition_patterns_returns_none(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    mock_transport(monkeypatch, lambda _request: httpx.Response(200, json={
        "results": [{"title": "McDonald's menu", "content": "Browse our latest menu."}]
    }))
    assert await food_lookup.search_tavily("McDonald's burger") is None


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.ReadTimeout("slow")])
async def test_tavily_error_or_timeout_returns_none(monkeypatch, response):
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handler(_request):
        if isinstance(response, Exception):
            raise response
        return response

    mock_transport(monkeypatch, handler)
    assert await food_lookup.search_tavily("restaurant meal") is None


async def test_cascade_reaches_tavily_only_after_usda_and_off_miss(monkeypatch):
    monkeypatch.setenv("USDA_API_KEY", "demo-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handler(request):
        if request.url.host == "api.nal.usda.gov":
            return httpx.Response(200, json={"foods": []})
        if request.url.host == "world.openfoodfacts.org":
            return httpx.Response(200, json={"products": []})
        return httpx.Response(200, json={"results": [{
            "title": "Niche Cafe Bowl nutrition",
            "url": "https://example.com/bowl",
            "content": (
                "Per 100g: Calories: 310 Protein: 20g Carbs: 35g "
                "Fat: 9g Fiber: 6g"
            ),
        }]})

    calls = mock_transport(monkeypatch, handler)
    result = await food_lookup.resolve_food("Niche Cafe Bowl")
    assert result is not None
    assert result["source"] == "Tavily: https://example.com/bowl"
    assert [call.url.host for call in calls] == [
        "api.nal.usda.gov", "world.openfoodfacts.org", "api.tavily.com"
    ]


@pytest.mark.parametrize("usda_body, off_body", [
    # Nutrients are a string, not a list of dicts.
    ({"foods": [{"fdcId": 1, "description": "x", "foodNutrients": "garbage"}]},
     {"products": [{"code": "1", "product_name": "y",
                    "nutriments": {"energy-kcal_100g": "abc"}}]}),
    # Wrong top-level shapes.
    ({"foods": ["not-a-dict"]}, {"products": [{"nutriments": None}]}),
    ({"totalHits": 0}, {}),
    # Negative and absurd per-100g values are rejected.
    ({"foods": [{"fdcId": 2, "description": "x",
                 "foodNutrients": [{"nutrientId": 1008, "value": -5}]}]},
     {"products": [{"code": "2", "product_name": "y",
                    "nutriments": {"energy-kcal_100g": 99999}}]}),
    # Valid macros but no source id — must not persist "USDA FDC: None" /
    # "OpenFoodFacts: " as macro_source.
    ({"foods": [{"description": "Bananas, raw",
                 "foodNutrients": USDA_PAYLOAD["foods"][0]["foodNutrients"]}]},
     {"products": [{"code": "  ", "product_name": "Rice noodles",
                    "nutriments": OFF_PAYLOAD["products"][0]["nutriments"]}]}),
])
async def test_malformed_payloads_degrade_to_none(monkeypatch, usda_body, off_body):
    monkeypatch.setenv("USDA_API_KEY", "demo-key")

    def handler(request):
        body = usda_body if request.url.host == "api.nal.usda.gov" else off_body
        return httpx.Response(200, json=body)

    mock_transport(monkeypatch, handler)
    assert await food_lookup.resolve_food("banana") is None


async def test_http_errors_and_non_json_degrade_to_none(monkeypatch):
    monkeypatch.setenv("USDA_API_KEY", "demo-key")

    def handler(request):
        if request.url.host == "api.nal.usda.gov":
            return httpx.Response(500)
        return httpx.Response(200, text="<html>not json</html>")

    mock_transport(monkeypatch, handler)
    assert await food_lookup.resolve_food("banana") is None


async def test_blank_query_makes_no_network_calls(monkeypatch):
    monkeypatch.setenv("USDA_API_KEY", "demo-key")
    calls = mock_transport(monkeypatch, lambda _request: httpx.Response(500))
    assert await food_lookup.resolve_food("   ") is None
    assert calls == []


def test_portion_from_grams_scales_database_macros():
    macros, source = food_lookup.portion_from_grams(BANANA_HIT, 150)
    factor = 150 / 100.0
    assert macros == {key: round(value * factor, 2)
                      for key, value in BANANA_HIT["macros_per_100g"].items()}
    assert source == "USDA FDC: 171705 — Bananas, raw, 150 g"


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
                "meal": "Lunch", "note": "USDA FDC: 170002",
            }]),
        }),
    ]

    async def fake_post(_token, payload):
        calls.append(payload["messages"][:])
        return responses.pop(0)

    async def fake_resolve(name):
        assert name == "sunchoke"
        return {**BANANA_HIT, "source": "USDA FDC: 170002"}

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)

    items, _, _ = await srv.parse_chat_message("150g sunchoke")

    assert len(calls) == 2
    assert calls[1][-1]["role"] == "tool"
    assert json.loads(calls[1][-1]["content"])["source"] == "USDA FDC: 170002"
    assert items[0]["note"] == "USDA FDC: 170002"


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
                       '"meal":"Snack","note":"USDA FDC: fabricated"}]',
        }),
    ]

    async def fake_post(_token, _payload):
        return responses.pop(0)

    async def fake_resolve(_name):
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
                 "meal": "Lunch", "note": "USDA FDC: 170002"},
                {"name": "mystery stew", "calories": 300, "protein": 12,
                 "carbs": 35, "fat": 12, "fiber": 4,
                 "basis": "per_serving", "sourced_from": "lookup",
                 "meal": "Lunch", "note": "USDA FDC: 170002"},
            ]),
        }),
    ]

    async def fake_post(_token, _payload):
        return responses.pop(0)

    async def fake_resolve(_name):
        return {**BANANA_HIT, "source": "USDA FDC: 170002"}

    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)
    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)

    items, _, _ = await srv.parse_chat_message("150g sunchoke and mystery stew")

    assert items[0]["note"] == "USDA FDC: 170002"
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
    assert captured["tools"][0]["function"]["name"] == "lookup_food"
    assert items[0]["sourced_from"] == "known"


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

    async def fake_resolve(name):
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

    async def fake_resolve(query):
        assert query == "greek yogurt"
        return BANANA_HIT

    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    handler = srv._coach_tool_handlers()["lookup_food"]

    # Fails closed like every other tool: no tenant context, no lookup.
    with pytest.raises(RuntimeError):
        await handler({"query": "greek yogurt"})

    token = bind_user(uuid4())
    try:
        assert (await handler({"query": "greek yogurt"}))["source"] == "USDA FDC: 171705"

        async def fake_miss(_query):
            return None

        monkeypatch.setattr(food_lookup, "resolve_food", fake_miss)
        result = await srv._coach_tool_handlers()["lookup_food"]({"query": "mystery"})
        assert result["result"] == "not found"
    finally:
        reset_user(token)


async def test_food_path_does_not_repeat_parser_lookup(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def fake_parse(_message):
        return [{"name": "sliced banana", "calories": 105, "protein": 2, "carbs": 25,
                 "fat": 1, "fiber": 3, "grams": 150, "meal": "Snack",
                 "note": "ESTIMATE"}], None

    async def fake_resolve(query):
        assert query == "sliced banana"
        return BANANA_HIT

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0):
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
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def fake_parse(_message):
        return [{"name": "sliced banana", "calories": 105, "protein": 2,
                 "carbs": 25, "fat": 1, "fiber": 3, "quantity": 1,
                 "grams": 100, "meal": "Snack", "note": "ESTIMATE"}], None

    async def fake_resolve(query, *, whole_item=False):
        assert query == "sliced banana"
        assert whole_item is False
        return BANANA_HIT

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0):
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
    fake = FakeStore()
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
                              meal, day_value, allow_estimate=False, fiber=0):
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
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}
    lookup_queries = []

    async def fake_parse(_message):
        return [{"name": "Chipotle chicken burrito bowl", "calories": 600,
                 "protein": 40, "carbs": 80, "fat": 20, "fiber": 2,
                 "grams": None, "quantity": 1, "meal": "Lunch",
                 "basis": "per_serving", "sourced_from": "cascade"}], None

    async def fake_usda(query):
        lookup_queries.append(("usda", query))
        return {
            "name": "Barebells Protein Bar",
            "macros_per_100g": {
                "calories": 364,
                "protein": 36,
                "carbs": 33,
                "fat": 15,
                "fiber": 5,
            },
            "source": "USDA FDC: 123456",
        }

    async def fake_off(query):
        lookup_queries.append(("off", query))
        return None

    async def fake_tavily(query):
        lookup_queries.append(("tavily", query))
        return food_lookup._tavily_result("1 Chipotle chicken burrito bowl", {
            "title": "Chipotle Chicken Burrito Bowl Nutrition",
            "url": "https://example.com/chipotle-bowl",
            "content": (
                "Serving size 1 bowl Calories 700 Protein 50g "
                "Carbohydrate 70g Fat 24g Fiber 10g"
            ),
        })

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0):
        seen.update(calories=calories, macro_source=macro_source)
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 700}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(food_lookup, "search_usda", fake_usda)
    monkeypatch.setattr(food_lookup, "search_openfoodfacts", fake_off)
    monkeypatch.setattr(food_lookup, "search_tavily", fake_tavily)
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

    async def fake_resolve_multiple(query, *, whole_item=False):
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
    async def fake_resolve(_query):
        return BANANA_HIT

    monkeypatch.setattr(food_lookup, "resolve_food", fake_resolve)
    assert await srv._upgrade_estimate(
        "mystery bar", None, whole_item=True
    ) is None


async def test_food_path_derives_sources_from_exact_real_data_matches(monkeypatch):
    fake = FakeStore()
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

    async def fake_resolve(query, *, whole_item=False):
        lookup_queries.append((query, whole_item))
        return None

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0):
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
    assert lookup_queries == [("bareBELLS", False)]
    assert seen == [
        ("ESTIMATE", 200.0, 20.0, 21.0, 7.0, 0.0),
        ("Known food: Banana", 210.0, 2.0, 54.0, 0.0, 6.0),
        ("Known food: sweet potato", 172.0, 3.2, 40.2, 0.2, 6.0),
        ("Known food: 93/7 ground beef", 340.0, 44.0, 0.0, 17.6, 0.0),
        ("ESTIMATE", 200.0, 8.0, 24.0, 8.0, 1.0),
        ("ESTIMATE", 200.0, 8.0, 24.0, 8.0, 1.0),
        ("ESTIMATE", 300.0, 12.0, 30.0, 14.0, 1.0),
    ]


async def test_food_path_weightless_preset_grams_use_cascade(monkeypatch):
    monkeypatch.setattr(srv, "_client", FakeStore())
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

    async def fake_resolve(name):
        assert name == "chili"
        return {
            "name": "Chili with beans",
            "macros_per_100g": {
                "calories": 120, "protein": 8, "carbs": 10,
                "fat": 5, "fiber": 3,
            },
            "source": "USDA FDC: 999999",
        }

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0):
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
        "macro_source": "USDA FDC: 999999 — Chili with beans, 100 g",
    }
