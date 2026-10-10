"""Regressions for brand-wide substitutions and explicit weighed portions."""
import httpx
import pytest

from test_food_lookup import food_lookup, srv, bind_user, reset_user, uuid4


class EmptyStore:
    async def fetch_presets(self):
        return []

    async def lookup_catalog(self, query):
        return None


@pytest.mark.parametrize("query,protein", [
    ("Barebells Soft Caramel Choco protein bar", 16),
    ("Fairlife Core Power Elite chocolate shake", 42),
    ("Barebells", 16),
    ("Fairlife", 42),
])
async def test_brand_names_do_not_bypass_product_resolution(monkeypatch, query, protein):
    monkeypatch.setattr(srv, "_client", EmptyStore())
    srv._resolved_food_cache.clear()
    async def no_official(query):
        return None
    async def provider(query, **kwargs):
        return {"name": query, "source": "FatSecret: synthetic-product",
                "macros_per_serving": {"calories": 220, "protein": protein,
                                       "carbs": 20, "fat": 8, "fiber": 3}}
    monkeypatch.setattr(food_lookup.cofid_lookup, "lookup", no_official)
    monkeypatch.setattr(food_lookup, "resolve_food", provider)
    token = bind_user(uuid4())
    try:
        found = await srv.resolve_food(query)
    finally:
        reset_user(token)
    assert found["name"] == query
    assert found["source"] == "FatSecret: synthetic-product"
    assert found["macros_per_serving"]["protein"] == protein


@pytest.mark.parametrize("names", [
    ["Fairlife 30g Shake", "Fairlife 42g Shake"],
    ["Fairlife 42g Shake", "Fairlife 30g Shake"],
])
async def test_ambiguous_preset_does_not_choose_first_product(monkeypatch, names):
    class Presets(EmptyStore):
        async def fetch_presets(self):
            return [{"name": name, "calories": 150, "protein": 30,
                     "carbs": 3, "fat": 2.5, "fiber": 0} for name in names]
    monkeypatch.setattr(srv, "_client", Presets())
    srv._resolved_food_cache.clear()
    async def miss(*args, **kwargs): return None
    monkeypatch.setattr(food_lookup.cofid_lookup, "lookup", miss)
    monkeypatch.setattr(food_lookup, "resolve_food", miss)
    token = bind_user(uuid4())
    try:
        found = await srv.resolve_food("Fairlife shake")
    finally:
        reset_user(token)
    assert found is None


@pytest.mark.parametrize("query", [
    "Barebells Soft Caramel Choco", "Fairlife Core Power Elite chocolate",
    "Moe's chocolate chip cookie and a banana",
])
async def test_parser_delivers_full_product_identity_to_model(monkeypatch, query):
    async def empty(*args): return []
    async def targets(*args): return {}
    async def response(token, payload):
        assert payload["messages"][1]["content"] == query
        return httpx.Response(200, request=httpx.Request("POST", "https://example.test"),
                              json={"choices": [{"message": {"content": "[]"}}]})
    monkeypatch.setattr(srv, "fetch_meals", empty)
    monkeypatch.setattr(srv, "fetch_presets", empty)
    monkeypatch.setattr(srv, "fetch_targets", targets)
    monkeypatch.setattr(srv, "_openai_access_token", lambda: "synthetic")
    monkeypatch.setattr(srv, "_post_openai_chat", response)
    await srv.parse_chat_message(query)


@pytest.mark.parametrize("query,calories,carbs", [
    ("100 g of sweet potato", 87, 21.3),
    ("115 g sweet potato", 100.05, 24.495),
])
def test_weighed_sweet_potato_scales_source_panel(query, calories, carbs):
    found = food_lookup.resolve_generic_whole_food(query)
    assert found["macros_per_serving"]["calories"] == pytest.approx(calories, abs=.01)
    assert found["macros_per_serving"]["carbs"] == pytest.approx(carbs, abs=.01)


@pytest.mark.parametrize("query", ["7 oz. of 93/7 beef", "7 oz 93/7 beef", "93/7 beef 7 oz."])
async def test_ounces_with_punctuation_scale_beef_without_losing_lean_ratio(query):
    async def catalog(identity):
        if identity != "93/7 beef":
            return None
        return {"name": identity, "source": "Catalog: synthetic beef",
                "macros_per_100g": {"calories": 200, "protein": 25,
                                     "carbs": 0, "fat": 10, "fiber": 0}}
    found = await food_lookup.resolve_food(query, catalog_lookup=catalog, allow_web=False)
    assert found is not None
    assert found["applied_quantity"] == 7
    assert found["applied_unit"] == "oz"
    assert found["macros_per_serving"]["calories"] == 396.89
    assert found["macros_per_serving"]["protein"] == 49.61


@pytest.mark.parametrize("query,calories,protein", [
    ("100 g of sweet potato", 87, 1.2),
    ("115 g sweet potato", 100.05, 1.38),
    ("7 oz. of 93/7 beef", 396.89, 49.61),
])
async def test_voice_persists_exact_weighted_totals_once(monkeypatch, query, calories, protein):
    from test_live_coach import FakeVoiceStore
    class Store(FakeVoiceStore):
        async def lookup_catalog(self, identity):
            if identity != "93/7 beef": return None
            return {"name": identity, "source": "Catalog: synthetic beef",
                    "macros_per_100g": {"calories": 200, "protein": 25,
                                         "carbs": 0, "fat": 10, "fiber": 0}}
    fake = Store()
    monkeypatch.setattr(srv, "_client", fake)
    srv._resolved_food_cache.clear()
    handlers = srv._voice_tool_handlers()
    token = bind_user(uuid4())
    try:
        args = {"description": query, "meal_type": "Dinner"}
        result = await handlers["log_meal"]("weighed-fixture", args)
        replay = await handlers["log_meal"]("weighed-fixture", args)
    finally:
        reset_user(token)
    assert result["status"] == "committed"
    assert result["logged"]["calories"] == calories
    assert result["logged"]["protein"] == protein
    assert replay["logged"]["calories"] == calories
    assert fake.insert_count == 1
