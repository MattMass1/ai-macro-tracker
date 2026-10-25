"""The intent layer: restaurant, packaged or homemade decides the source.

* restaurant ("Big Mac", "at Chipotle") -> looked up online as that place's item
* packaged ("Kodiak waffles") -> looked up as that product
* homemade ("I made a breakfast burrito") -> typical recipe, never searched

Real MealDrafts engine, in-memory store, fake resolver that records queries.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_SHARED_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

from datetime import datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

import domain  # noqa: E402
import meal_intent  # noqa: E402
from auth import bind_user, reset_user  # noqa: E402
from meal_drafts import MEAL_DRAFT_INSTRUCTIONS, MEAL_DRAFT_TOOL, MealDrafts  # noqa: E402
from test_meal_flow_v2 import FakeDraftStore  # noqa: E402

NOON = datetime(2026, 10, 25, 12, 30, tzinfo=domain.LOCAL_TZ)


def panel(name, kcal, source=None, **extra):
    return {"name": name, "macro_source": source or f"Restaurant menu: {name}", "calories": float(kcal),
            "protein": 20.0, "carbs": 40.0, "fat": 20.0, "fiber": 2.0, **extra}


WEB = dict(estimate_provenance="server_web_estimate",
           attribution={"source_host": "example.test", "verification_state": "unverified_web_estimate",
                        "cache_allowed": False})


@pytest.fixture
def engine(monkeypatch):
    import server as srv
    monkeypatch.delenv("TYPICAL_SERVING_ESTIMATES", raising=False)
    store, calls, foods = FakeDraftStore(), [], {}

    async def resolve(component):
        description = str(component["description"]).casefold()
        calls.append(description)
        found = foods.get(description)
        return dict(found) if found else None

    eng = MealDrafts(store=store, resolve=resolve, validate_values=srv._validated_meal_values, clock=lambda: NOON)
    scope = bind_user(uuid4())
    try:
        yield {"engine": eng, "store": store, "calls": calls, "foods": foods}
    finally:
        reset_user(scope)


# --------------------------------------------------------------------------- #
# Routing rules
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,lane,place", [
    ("Big Mac from McDonald's", "restaurant", "McDonald's"),
    ("mcdonalds breakfast burrito", "restaurant", "McDonald's"),
    ("Chick-fil-A nuggets", "restaurant", "Chick-fil-A"),
    ("chipotle burrito bowl", "restaurant", "Chipotle"),
    ("homemade breakfast burrito", "homemade", None),
    ("leftover chicken stir fry", "homemade", None),
    ("breakfast burrito", None, None),
    ("kodiak waffles", None, None),  # a brand needs the model's tag (or a lookup), not a guess
])
def test_inferred_lane_from_words_alone(text, lane, place):
    assert meal_intent.route(text, None, None)[:2] == (lane, place)


def test_model_choice_wins_over_words():
    assert meal_intent.route("burrito bowl", "restaurant", "Chipotle") == ("restaurant", "Chipotle", "model")
    assert meal_intent.route("my mcdonalds-style burger", "homemade", None) == ("homemade", None, "model")
    assert meal_intent.clean_place("mcdonalds") == "McDonald's"


# --------------------------------------------------------------------------- #
# Lanes through the engine
# --------------------------------------------------------------------------- #

async def test_restaurant_item_is_looked_up_under_the_place_name(engine):
    engine["foods"]["mcdonald's big mac"] = panel("Big Mac", 590)
    result = await engine["engine"].handle({"add": [
        {"description": "Big Mac", "source": "restaurant", "place": "McDonald's"}]})
    assert result["status"] == "committed", result
    assert engine["calls"] == ["mcdonald's big mac"]
    assert result["logged"]["calories"] == 590
    assert "Big Mac (McDonald's)" in result["confirmation"]


async def test_restaurant_breakfast_burrito_uses_the_menu_not_a_home_recipe(engine):
    engine["foods"]["mcdonald's breakfast burrito"] = panel("Sausage Burrito", 310)
    result = await engine["engine"].handle({"add": [
        {"description": "breakfast burrito with eggs, sausage and cheese", "source": "restaurant",
         "place": "McDonald's"}]})
    assert result["status"] == "committed", result
    # Listed contents don't turn a menu item into a home recipe.
    assert "typical" not in result["confirmation"]
    assert result["logged"]["calories"] == 310
    assert engine["calls"][-1] == "mcdonald's breakfast burrito"


async def test_restaurant_web_estimate_is_kept_and_disclosed(engine):
    engine["foods"]["local diner breakfast burrito"] = panel("breakfast burrito", 650,
                                                             "WEB ESTIMATE (unverified): example.test", **WEB)
    result = await engine["engine"].handle({"add": [
        {"description": "breakfast burrito", "source": "restaurant", "place": "Local Diner"}]})
    assert result["status"] == "committed", result
    assert result["logged"]["calories"] == 650
    assert "(est., example.test)" in result["confirmation"]


async def test_restaurant_with_no_numbers_anywhere_falls_back_to_a_disclosed_recipe(engine):
    result = await engine["engine"].handle({"add": [
        {"description": "breakfast burrito", "size": "large", "source": "restaurant", "place": "Local Diner"}]})
    assert result["status"] == "committed", result
    assert "no published numbers found for Local Diner, so this is a typical recipe" in result["confirmation"]


async def test_homemade_dish_is_never_searched_online(engine):
    engine["foods"]["breakfast burrito"] = panel("Brand breakfast burrito", 300, "FatSecret: 1",
                                                 attribution={"provider": "FatSecret", "cache_allowed": True})
    result = await engine["engine"].handle({"add": [
        {"description": "breakfast burrito", "source": "homemade"}]})
    assert result["status"] == "committed", result
    assert engine["calls"] == []
    assert "(est., typical medium recipe)" in result["confirmation"]


async def test_homemade_plain_ingredient_still_uses_the_food_database(engine):
    engine["foods"]["chicken breast"] = panel("chicken breast", 165, "USDA FDC 171477: chicken")
    result = await engine["engine"].handle({"add": [
        {"description": "chicken breast", "quantity": 1, "source": "homemade"}]})
    assert result["status"] == "committed", result
    assert engine["calls"] == ["chicken breast"]


async def test_packaged_product_is_looked_up_by_maker_and_name(engine):
    engine["foods"]["kodiak power waffles"] = panel("Kodiak Power Waffles", 280, "FatSecret: 99",
                                                    attribution={"provider": "FatSecret", "cache_allowed": True})
    result = await engine["engine"].handle({"add": [
        {"description": "power waffles", "quantity": 2, "source": "packaged", "place": "Kodiak"}]})
    assert result["status"] == "committed", result
    assert engine["calls"] == ["kodiak power waffles"]
    assert "power waffles (Kodiak)" in result["confirmation"]


async def test_chain_in_the_words_routes_without_a_tag(engine):
    engine["foods"]["chick-fil-a nuggets"] = panel("Chick-fil-A Nuggets 8 Count", 250)
    result = await engine["engine"].handle({"add": [{"description": "Chick-fil-A nuggets"}]})
    assert result["status"] == "committed", result
    assert engine["calls"] == ["chick-fil-a nuggets"]


async def test_mixed_meal_routes_each_food_separately(engine):
    engine["foods"]["starbucks iced latte"] = panel("Iced Caffe Latte", 130)
    result = await engine["engine"].handle({"add": [
        {"description": "eggs", "source": "homemade"},
        {"description": "breakfast burrito with sausage and cheese", "source": "homemade"},
        {"description": "iced latte", "source": "restaurant", "place": "Starbucks"}]})
    assert result["status"] == "committed", result
    # The home-cooked dish was never searched; plain eggs use the food database.
    assert not any("burrito" in c for c in engine["calls"]) and "starbucks iced latte" in engine["calls"]
    text = result["confirmation"]
    assert "iced latte (Starbucks)" in text and "(est., typical medium recipe)" in text


async def test_correcting_where_it_came_from_amends_the_same_entry(engine):
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle({"add": [{"description": "breakfast burrito", "source": "homemade"}]})
    assert first["status"] == "committed", first
    engine["foods"]["panera breakfast burrito"] = panel("Panera breakfast burrito", 480)
    fixed = await eng.handle({"amend_ref": first["draft_ref"], "edit": [
        {"item_ref": "item1", "source": "restaurant", "place": "Panera"}]})
    assert fixed["status"] == "amended", fixed
    assert fixed["entry_id"] == first["entry_id"] and len(store.entries) == 1
    assert fixed["logged"]["calories"] == 480


async def test_description_edit_keeps_the_models_lane(engine):
    eng = engine["engine"]
    engine["foods"]["mcdonald's mcchicken"] = panel("McChicken", 400)
    engine["foods"]["mcdonald's mcdouble"] = panel("McDouble", 400)
    first = await eng.handle({"add": [{"description": "mcchicken", "source": "restaurant", "place": "McDonald's"}]})
    fixed = await eng.handle({"amend_ref": first["draft_ref"], "edit": [
        {"item_ref": "item1", "description": "mcdouble"}]})
    assert fixed["status"] == "amended", fixed
    assert engine["calls"][-1] == "mcdonald's mcdouble"


async def test_bad_source_or_place_is_rejected_before_anything_is_saved(engine):
    from meal_drafts import DraftInputError
    eng, store = engine["engine"], engine["store"]
    with pytest.raises(DraftInputError):
        await eng.handle({"add": [{"description": "burger", "source": "fast food"}]})
    with pytest.raises(DraftInputError):
        await eng.handle({"add": [{"description": "burger", "place": "x" * 80}]})
    assert store.drafts == {}


def test_instructions_and_schema_carry_the_three_lanes():
    add = MEAL_DRAFT_TOOL["input_schema"]["properties"]["add"]["items"]["properties"]
    assert add["source"]["enum"] == ["restaurant", "packaged", "homemade"] and "place" in add
    for phrase in ('source "restaurant" + place', 'source "packaged"', 'source "homemade"', "never ask the user"):
        assert phrase in MEAL_DRAFT_INSTRUCTIONS
