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


async def test_food_path_upgrades_parser_estimate_via_free_lookup(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)
    seen = {}

    async def fake_parse(_message):
        return [{"name": "banana", "calories": 105, "protein": 2, "carbs": 25,
                 "fat": 1, "fiber": 3, "grams": 150, "meal": "Snack",
                 "note": "ESTIMATE"}], None

    async def fake_resolve(query):
        assert query == "banana"
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
    assert seen["macro_source"] == "USDA FDC: 171705 — Bananas, raw, 150 g"
    # The user's 150 g portion scales the per-100g database macros — never the
    # parser's estimated calories.
    assert seen["calories"] == round(89.0 * 1.5, 2)
    assert seen["protein"] == round(1.09 * 1.5, 2)
    assert seen["allow_estimate"] is True
    payload = json.loads(response.body)
    assert payload["logged"][0]["name"] == "banana"


async def test_food_path_keeps_flagged_estimate_on_miss_or_missing_grams(monkeypatch):
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

    # Grams stated but both databases miss: the flagged estimate stands.
    response = await srv.api_chat(chat_request({"message": "400g smoothie"}))
    assert response.status_code == 200
    assert seen["macro_source"] == "ESTIMATE"
    assert seen["calories"] == 300.0
    assert lookups == ["mystery smoothie"]

    # No grams stated: nothing to scale honestly, so no lookup fires at all.
    items[0].pop("grams")
    response = await srv.api_chat(chat_request({"message": "smoothie"}))
    assert response.status_code == 200
    assert seen["macro_source"] == "ESTIMATE"
    assert lookups == ["mystery smoothie"]


async def test_food_path_skips_lookup_for_sourced_items(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(srv, "_client", fake)

    async def fake_parse(_message):
        return [{"name": "Barebells", "calories": 200, "protein": 20, "carbs": 21,
                 "fat": 7, "fiber": 0, "meal": "Snack", "note": "known food"}], None

    async def unexpected_resolve(_query):
        pytest.fail("sourced items must not trigger a database lookup")

    async def fake_write_meal(name, calories, protein, carbs, fat, macro_source,
                              meal, day_value, allow_estimate=False, fiber=0):
        return {"logged": {"name": name, "calories": calories, "protein": protein,
                           "carbs": carbs, "fat": fat, "fiber": fiber, "meal": meal}}

    async def fake_day_payload(_day, ensure=None):
        return {"totals": {"calories": 200}, "targets": {"calories": 2000}}

    monkeypatch.setattr(srv, "parse_chat_message", fake_parse)
    monkeypatch.setattr(food_lookup, "resolve_food", unexpected_resolve)
    monkeypatch.setattr(srv, "write_meal", fake_write_meal)
    monkeypatch.setattr(srv, "day_payload", fake_day_payload)

    response = await srv.api_chat(chat_request({"message": "a Barebells bar"}))
    assert response.status_code == 200
