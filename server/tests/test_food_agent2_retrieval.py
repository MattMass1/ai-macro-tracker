"""food_agent2 retrieval: every lane resolves, estimates or names what was not counted.

Offline. ``FakeSources`` stands in for the live providers. USDA rows here are
synthetic but use the real search-response shape.
"""

from __future__ import annotations

import asyncio

import pytest

from food_agent.contracts import FoodIdentity, Portion
from food_agent2 import match
from food_agent2.retrieval import Retrieval

COUNT_3 = Portion(kind="count", amount=3, text="3")
ONE = Portion(kind="count", amount=1, text="1")
NONE = Portion(kind="unspecified", text="")


def identity(name, brand=None, kind="generic", variant=None, preparation=None):
    return FoodIdentity(name=name, brand=brand, kind=kind, variant=variant, preparation=preparation)


def fatsecret(name, brand, food_id="1", serving="1 serving", kcal=300.0):
    return {"name": name, "brand_name": brand, "source": f"FatSecret: {food_id}", "serving_size": serving,
            "macros_per_serving": {"calories": kcal, "protein": 20.0, "carbs": 30.0, "fat": 10.0, "fiber": 2.0},
            "attribution": {"provider": "FatSecret", "external_id": food_id, "cache_allowed": False,
                            "verification_state": "provider_search_match", "serving_basis": "per_serving",
                            "attribution_text": "Nutrition data from FatSecret"}}


def curated_egg():
    return {"name": "chicken egg", "source": "Generic: chicken egg, per 100 g, raw; source: UK CoFID",
            "serving_size": "Generic: chicken egg, per 100 g, raw; source: UK CoFID - 52.7 g",
            "macros_per_100g": {"calories": 131, "protein": 12.6, "carbs": 0.7, "fat": 9, "fiber": 0},
            "macros_per_serving": {"calories": 69, "protein": 6.6, "carbs": 0.4, "fat": 4.7, "fiber": 0},
            "count_grams": {"small": 45}, "attribution": {"cache_allowed": True}}


def usda_row(description, fdc=1, data_type="Survey (FNDDS)", kcal=270.0, fiber=1.0, carbs=50.0, measures=()):
    nutrients = {1008: kcal, 1003: 10.0, 1005: carbs, 1004: 2.0}
    if fiber is not None:
        nutrients[1079] = fiber
    return {"fdcId": fdc, "description": description, "dataType": data_type,
            "foodNutrients": [{"nutrientId": k, "value": v} for k, v in nutrients.items()],
            "foodMeasures": [{"disseminationText": t, "gramWeight": g} for t, g in measures]}


class FakeSources:
    def __init__(self, exact=None, rows=None, hits=None, foods=None, web=None, web_delay=0.0):
        self.exact_rows, self.row_map, self.hit_map = exact or {}, rows or {}, hits or {}
        self.foods, self.web_map, self.web_delay = foods or {}, web or {}, web_delay
        self.calls: list[tuple[str, str]] = []

    async def exact(self, query, lane):
        self.calls.append(("exact", query.casefold()))
        return self.exact_rows.get(query.casefold())

    async def rows(self, query, require_all):
        self.calls.append(("rows", query.casefold()))
        return self.row_map.get(query.casefold(), [])

    async def hits(self, query):
        self.calls.append(("hits", query.casefold()))
        return self.hit_map.get(query.casefold(), [])

    async def food(self, food_id, query, hit, prefer=""):
        self.calls.append(("food", str(food_id)))
        return self.foods.get(str(food_id))

    async def web(self, query):
        self.calls.append(("web", query.casefold()))
        if self.web_delay:
            await asyncio.sleep(self.web_delay)
        return self.web_map.get(query.casefold())


def web_estimate(name, kcal=400.0):
    return {"name": name, "source": "WEB ESTIMATE (unverified): example.com; per 1 serving",
            "estimate_provenance": "server_web_estimate", "serving_size": "1 serving",
            "macros_per_serving": {"calories": kcal, "protein": 15.0, "carbs": 40.0, "fat": 18.0, "fiber": 3.0},
            "attribution": {"provider": "OpenAI web_search", "cache_allowed": False,
                            "verification_state": "unverified_web_estimate"}}


async def find(sources, ident, portion=ONE, **kw):
    retrieval = Retrieval(sources)
    scope = retrieval.scope()
    first = await retrieval.find(ident, portion, scope)
    if first.status == "needs_web" and kw.get("web", True):
        return await retrieval.find_on_web(ident, portion, scope)
    return first


# ---------------------------------------------------------------- match guards


@pytest.mark.parametrize("asked,candidate", [
    ("grilled chicken club sandwich", "Grilled Chicken Club w/ American"),
    ("chipotle crunchwrap slider", "Creamy Chipotle Crispy Chicken Crunchwrap Slider"),
    ("marshmallow peanut road", "Marshmallow Rocky Road Protein Bar"),
])
def test_real_near_matches_are_close_enough(asked, candidate):
    assert match.closeness(asked, candidate) >= match.MIN_CLOSENESS
    assert match.conflict(asked, candidate) is None


@pytest.mark.parametrize("asked,candidate,reason", [
    ("salmon fillet", "Tuna Fillet", "species"),
    ("grilled chicken sandwich", "Grilled Beef Sandwich", "species"),
    ("80/20 ground beef", "93/7 ground beef", "ratio"),
    ("salted caramel protein bar", "Chocolate Protein Bar", "flavor"),
    ("diet coke", "Coke", "variant"),
    ("double cheeseburger", "Cheeseburger", "variant"),
])
def test_hard_guards_are_never_swapped(asked, candidate, reason):
    assert match.conflict(asked, candidate) == reason


def test_cooked_panel_is_refused_for_a_food_weighed_raw():
    assert match.cook_conflict("raw chicken breast", "Chicken breast, roasted") is True
    assert match.cook_conflict("chicken breast", "Chicken breast, roasted") is False


def test_typos_still_count_as_the_same_word():
    assert match.closeness("crispy chipolte slider", "Crispy Chipotle Slider") == pytest.approx(1.0)


def test_unrelated_food_is_not_close():
    assert match.closeness("ahi tuna bowl", "Blueberry Muffin") < match.MIN_CLOSENESS


# ---------------------------------------------------------------- failure class 3: plain foods


async def test_preparation_words_do_not_lose_a_plain_food():
    sources = FakeSources(exact={"eggs": curated_egg()})
    found = await find(sources, identity("eggs", preparation="sunny side up"), COUNT_3)
    assert found.status == "verified"
    assert found.food.display_name.casefold() == "sunny side up eggs"
    assert "chicken egg" in (found.note or "")


async def test_raw_weighed_food_never_strips_to_a_cooked_row():
    cooked = {**curated_egg(), "name": "chicken breast, roasted"}
    sources = FakeSources(exact={"chicken breast": cooked})
    grams = Portion(kind="mass", amount=200, unit="g", text="200 g")
    found = await find(sources, identity("chicken breast", preparation="raw"), grams, web=False)
    assert found.status != "verified"


async def test_generic_lane_takes_the_closest_usda_row_and_says_so():
    rows = [usda_row("Bagel, with fruit other than raisins", fdc=7, measures=[("1 bagel", 105)])]
    sources = FakeSources(rows={"blueberry bagel": rows})
    found = await find(sources, identity("blueberry bagel"))
    assert found.status == "verified"
    assert found.food.display_name.casefold() == "blueberry bagel"
    assert found.food.food_id == "USDA FDC:7"
    assert "Bagel, with fruit" in found.note


async def test_usda_retry_on_the_head_noun_when_all_words_find_nothing():
    rows = [usda_row("Pizza, cheese, thin crust", fdc=9, measures=[("1 slice", 107)])]
    sources = FakeSources(rows={"pizza": rows})
    found = await find(sources, identity("New York style pizza"))
    assert found.status == "verified" and found.food.food_id == "USDA FDC:9"


async def test_complete_usda_row_beats_a_row_with_unknown_fiber():
    rows = [usda_row("Sourdough bread", fdc=1, fiber=None), usda_row("Bread, sourdough", fdc=2)]
    sources = FakeSources(rows={"sourdough bread": rows})
    found = await find(sources, identity("sourdough bread"))
    assert found.food.food_id == "USDA FDC:2"


async def test_typical_dish_needs_no_network():
    sources = FakeSources()
    found = await find(sources, identity("breakfast burrito with eggs, bacon, cheese", kind="homemade"))
    assert found.status == "estimated"
    assert "estimate" in found.food.macro_source.casefold()
    assert [c for c in sources.calls if c[0] in ("rows", "hits", "web")] == []


async def test_single_staple_is_a_labeled_typical_serving():
    found = await find(FakeSources(), identity("cheese"), NONE)
    assert found.status == "estimated" and found.food.panel.calories > 0


# ---------------------------------------------------------------- failure classes 1, 2: near matches


async def test_named_product_matches_the_closest_real_one():
    hits = {"chick-fil-a grilled chicken club sandwich": [
        {"food_id": "11", "food_name": "Grilled Chicken Club w/ American", "brand_name": "Chick-fil-A"},
        {"food_id": "12", "food_name": "Chicken Biscuit", "brand_name": "Chick-fil-A"}]}
    sources = FakeSources(hits=hits, foods={"11": fatsecret("Grilled Chicken Club w/ American", "Chick-fil-A", "11")})
    found = await find(sources, identity("Grilled Chicken Club Sandwich", "Chick-fil-A", "restaurant"))
    assert found.status == "verified"
    assert found.food.display_name == "Chick-fil-A Grilled Chicken Club w/ American"
    assert found.note.startswith("matched to ")
    assert ("web", "chick-fil-a grilled chicken club sandwich") not in sources.calls


async def test_a_different_brand_is_never_the_closest_match():
    hits = {"barebells salty peanut": [{"food_id": "2", "food_name": "Salty Peanut Bar", "brand_name": "Quest"}]}
    sources = FakeSources(hits=hits, foods={"2": fatsecret("Salty Peanut Bar", "Quest", "2")})
    found = await find(sources, identity("Salty Peanut", "Barebells", "packaged"), web=False)
    assert found.status == "needs_web" or found.status == "not_counted"


async def test_hard_guard_skips_a_conflicting_flavor_and_takes_the_next():
    hits = {"barebells caramel cashew bar": [
        {"food_id": "3", "food_name": "Vanilla Bar", "brand_name": "Barebells"},
        {"food_id": "4", "food_name": "Caramel Cashew", "brand_name": "Barebells"}]}
    sources = FakeSources(hits=hits, foods={"4": fatsecret("Caramel Cashew", "Barebells", "4"),
                                            "3": fatsecret("Vanilla Bar", "Barebells", "3")})
    found = await find(sources, identity("Caramel Cashew Bar", "Barebells", "packaged"))
    assert found.food.external_id == "4"


async def test_brand_in_the_identity_routes_a_generic_kind_to_the_branded_lane():
    sources = FakeSources(hits={"barebells cookie crisp": []})
    await find(sources, identity("Cookie Crisp", "Barebells", "generic"), web=False)
    assert ("hits", "barebells cookie crisp") in sources.calls
    assert not [c for c in sources.calls if c[0] == "rows"]


async def test_whole_food_query_never_reaches_fatsecret():
    sources = FakeSources()
    await find(sources, identity("sticky rice"), web=False)
    assert not [c for c in sources.calls if c[0] in ("hits", "food")]


# ---------------------------------------------------------------- estimates and not counted


async def test_any_miss_ends_in_a_labeled_web_estimate():
    sources = FakeSources(web={"gochujang sauce": web_estimate("gochujang sauce", 60)})
    found = await find(sources, identity("gochujang sauce"))
    assert found.status == "estimated"
    assert "estimate" in found.food.macro_source.casefold()


async def test_nothing_anywhere_is_not_counted_with_a_reason():
    found = await find(FakeSources(), identity("mystery stew"))
    assert found.status == "not_counted" and found.reason


async def test_web_query_drops_a_category_word_the_page_will_not_repeat():
    sources = FakeSources(web={"chick-fil-a grilled chicken club": web_estimate("x")})
    found = await find(sources, identity("Grilled Chicken Club Sandwich", "Chick-fil-A", "restaurant"))
    assert found.status == "estimated"


async def test_a_provider_error_is_a_miss_not_a_crash():
    class Broken(FakeSources):
        async def exact(self, query, lane):
            raise ConnectionError("synthetic")

    sources = Broken(web={"eggs": web_estimate("eggs")})
    assert (await find(sources, identity("eggs"))).status == "estimated"


# ---------------------------------------------------------------- failure class 9: fan-out


async def test_same_query_in_one_turn_is_one_provider_call():
    sources = FakeSources(hits={"barebells bar": []})
    retrieval = Retrieval(sources)
    scope = retrieval.scope()
    for _ in range(3):
        await retrieval.find(identity("Bar", "Barebells", "packaged"), ONE, scope)
    assert sources.calls.count(("hits", "barebells bar")) == 1


async def test_per_turn_cap_stops_network_calls():
    sources = FakeSources()
    retrieval = Retrieval(sources, max_calls=3)
    scope = retrieval.scope()
    for number in range(8):
        await retrieval.find(identity(f"Food {number}", "Acme", "packaged"), ONE, scope)
    assert len([c for c in sources.calls if c[0] != "exact"]) <= 3


# ---------------------------------------------------------------- the unit the user named


@pytest.mark.parametrize("words,amount,expected", [("1 tablespoon", 1, 1.0), ("0.5 tbsp", 0.5, 0.5)])
async def test_a_spoon_of_a_curated_fat_counts_without_a_question(words, amount, expected):
    import food_lookup
    from food_agent.portion import multiplier
    spoon = Portion(kind="volume", amount=amount, unit="tbsp", text=words)
    sources = FakeSources(exact={"butter": food_lookup.resolve_generic_whole_food("butter")})
    found = await find(sources, identity("butter"), spoon)
    assert found.food is not None and found.food.serving_basis.grams == pytest.approx(14.2)
    assert float(multiplier(spoon, found.food.serving_basis, identity("butter"))) == pytest.approx(expected)
    assert found.food.panel.calories == pytest.approx(105.6, abs=0.2), "one tablespoon, not 100 g"


async def test_pieces_of_a_curated_food_use_its_piece_weight():
    import food_lookup
    from food_agent.portion import multiplier
    pieces = Portion(kind="count", amount=2, text="2 pieces")
    sources = FakeSources(exact={"bacon": food_lookup.resolve_generic_whole_food("bacon")})
    found = await find(sources, identity("bacon"), pieces)
    assert found.food.serving_basis.grams == pytest.approx(8)
    assert float(multiplier(pieces, found.food.serving_basis, identity("bacon"))) == pytest.approx(2)


def test_a_piece_of_toast_is_the_rows_slice_unless_a_piece_is_listed():
    import usda_lookup
    toast = {"description": "toasted sourdough toast", "foodMeasures": [
        {"disseminationText": "1 slice", "gramWeight": 32}]}
    assert usda_lookup._portion(toast, "1 piece of sourdough toast") == ("1 slice", 32.0)
    toast["foodMeasures"].append({"disseminationText": "1 piece", "gramWeight": 40})
    assert usda_lookup._portion(toast, "1 piece of sourdough toast") == ("1 piece", 40.0)


async def test_one_fatsecret_food_asked_for_twice_in_a_minute_is_one_request(monkeypatch):
    import food_lookup
    calls = []

    async def request(data):
        calls.append(data["food_id"])
        return {"food": {"food_id": data["food_id"], "food_name": "Royal Crispy Wrap"}} if data["food_id"] == "7" else None

    monkeypatch.setattr(food_lookup, "_fatsecret_request", request)
    food_lookup.clear_search_cache()
    first = await food_lookup._fatsecret_food("7")
    first["food_name"] = "changed by a caller"
    assert (await food_lookup._fatsecret_food("7"))["food_name"] == "Royal Crispy Wrap"
    assert calls == ["7"]
    assert await food_lookup._fatsecret_food("8") is None and await food_lookup._fatsecret_food("8") is None
    assert calls == ["7", "8", "8"], "a failed request is never remembered"
