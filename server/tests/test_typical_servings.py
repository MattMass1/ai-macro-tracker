"""Natural meal talk with no measurements: typical-serving estimates.

"I'm having eggs and a breakfast burrito with eggs, sausage and cheese" or
"a salad with bacon and cheese" logs in one turn, with no question about
weights. Dishes are built from their listed contents at a typical serving
(small / medium / large), every number is server arithmetic over pinned USDA
SR Legacy rows, and the receipt says it is an estimate and what it assumed.

No database and no network: the real MealDrafts engine on the in-memory
draft store from test_meal_flow_v2, with a fake resolver.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_SHARED_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

from datetime import datetime  # noqa: E402
from uuid import uuid4

import pytest

import dish_estimates
import domain
from auth import bind_user, reset_user
from meal_drafts import MEAL_DRAFT_INSTRUCTIONS, MEAL_DRAFT_TOOL, MealDrafts
from test_meal_flow_v2 import FakeDraftStore

BREAKFAST = datetime(2026, 10, 25, 8, 15, tzinfo=domain.LOCAL_TZ)
WEB_BURRITO = {"name": "breakfast burrito", "macro_source": "WEB ESTIMATE (unverified): example.test; per 1 serving",
               "calories": 300.0, "protein": 12.0, "carbs": 30.0, "fat": 14.0, "fiber": 1.0,
               "estimate_provenance": "server_web_estimate",
               "attribution": {"source_host": "example.test", "cache_allowed": False}}
LABEL_BURRITO = {"name": "Brand breakfast burrito", "macro_source": "FatSecret: Brand breakfast burrito",
                 "calories": 310.0, "protein": 13.0, "carbs": 33.0, "fat": 13.0, "fiber": 2.0,
                 "attribution": {"provider": "FatSecret", "cache_allowed": True}}


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

    eng = MealDrafts(store=store, resolve=resolve, validate_values=srv._validated_meal_values,
                     clock=lambda: BREAKFAST)
    scope = bind_user(uuid4())
    try:
        yield {"engine": eng, "store": store, "calls": calls, "foods": foods}
    finally:
        reset_user(scope)


def _kcal(key, grams):
    return dish_estimates._I[key][2][0] * grams / 100


async def test_eggs_and_a_breakfast_burrito_with_listed_contents_log_in_one_turn(engine):
    eng, store = engine["engine"], engine["store"]
    result = await eng.handle({"add": [
        {"description": "eggs"},
        {"description": "breakfast burrito with eggs, sausage and cheese", "size": "large"}]})
    assert result["status"] == "committed", result
    assert len(store.commits) == 1
    text = result["confirmation"]
    assert "?" not in text  # nothing asked
    assert "2 eggs" in text and "assumed 2, a typical serving" in text
    assert "1 large breakfast burrito with eggs, sausage and cheese (est., typical large recipe)" in text
    assert "built from: flour tortilla, scrambled egg, pork sausage, cheddar cheese" in text
    # The listed contents describe this burrito: no lookup of the dish name.
    assert not any("burrito" in call for call in engine["calls"])
    # Server arithmetic over the pinned rows: large = 117 g tortilla, contents x1.4.
    burrito = _kcal("flour tortilla", 117) + sum(_kcal(k, dish_estimates._I[k][3] * 1.4)
                                                 for k in ("egg", "sausage", "cheddar"))
    eggs = _kcal("egg", 122)
    assert result["logged"]["calories"] == pytest.approx(round(burrito + eggs, 2), abs=0.05)
    components = store.commits[0][3]["components"]
    assert {c["estimate_provenance"] for c in components} == {"typical_serving_estimate"}


async def test_salad_with_bacon_and_cheese_is_estimated_and_says_dressing_is_not_counted(engine):
    result = await engine["engine"].handle({"add": [{"description": "salad with bacon and cheese"}]})
    assert result["status"] == "committed", result
    text = result["confirmation"]
    assert "(est., typical medium recipe)" in text
    assert "assumed medium; built from: romaine lettuce, bacon, cheddar cheese; no dressing counted" in text
    expected = _kcal("lettuce", 94) + _kcal("bacon", 16) + _kcal("cheddar", 28)
    assert result["logged"]["calories"] == pytest.approx(round(expected, 2), abs=0.05)


@pytest.mark.parametrize("dish", ["breakfast burrito", "salad with chicken and ranch", "burrito",
                                  "omelette with ham and cheese", "quesadilla", "turkey sandwich"])
def test_small_medium_large_scale_in_order(dish):
    kcal = [dish_estimates.estimate({"description": dish, "size": s})["calories"]
            for s in ("small", "medium", "large")]
    assert kcal[0] < kcal[1] < kcal[2]


def test_size_word_in_the_description_counts_as_stated():
    said = dish_estimates.estimate({"description": "large breakfast burrito"})
    field = dish_estimates.estimate({"description": "breakfast burrito", "size": "large"})
    assert said["calories"] == field["calories"]
    assert "assumed medium" not in said["assumption"]
    assert "assumed medium" in dish_estimates.estimate({"description": "breakfast burrito"})["assumption"]


def test_negation_extra_and_counted_pieces():
    plain = dish_estimates.estimate({"description": "salad with chicken and cheese"})
    none = dish_estimates.estimate({"description": "salad with chicken, no cheese"})
    extra = dish_estimates.estimate({"description": "salad with chicken and extra cheese"})
    assert none["calories"] < plain["calories"] < extra["calories"]
    assert "cheddar" not in none["assumption"]
    three = dish_estimates.estimate({"description": "3 egg omelette"})
    two = dish_estimates.estimate({"description": "2 egg omelette"})
    assert three["built_from"][0]["grams"] == 183 and two["built_from"][0]["grams"] == 122


def test_breakfast_burrito_listing_only_fillings_still_has_egg():
    est = dish_estimates.estimate({"description": "breakfast burrito with bacon and cheese"})
    assert [p["label"] for p in est["built_from"]] == ["flour tortilla", "scrambled egg", "bacon", "cheddar cheese"]
    no_egg = dish_estimates.estimate({"description": "breakfast burrito with bacon, no egg"})
    assert "scrambled egg" not in [p["label"] for p in no_egg["built_from"]]


async def test_unknown_listed_content_is_named_not_counted_never_guessed(engine):
    result = await engine["engine"].handle({"add": [
        {"description": "breakfast burrito with chorizo and cheese"}]})
    assert result["status"] == "committed", result
    assert "not counted: chorizo" in result["confirmation"]


async def test_web_guess_at_a_bare_dish_name_loses_to_the_typical_recipe(engine):
    engine["foods"]["breakfast burrito"] = WEB_BURRITO
    result = await engine["engine"].handle({"add": [{"description": "breakfast burrito"}]})
    assert result["status"] == "committed", result
    assert "(est., typical medium recipe)" in result["confirmation"]
    assert "typical recipe: flour tortilla, scrambled egg" in result["confirmation"]
    assert "example.test" not in result["confirmation"]


async def test_a_real_source_for_a_bare_dish_name_still_wins(engine):
    engine["foods"]["breakfast burrito"] = LABEL_BURRITO
    result = await engine["engine"].handle({"add": [{"description": "breakfast burrito"}]})
    assert result["status"] == "committed", result
    assert result["logged"]["calories"] == LABEL_BURRITO["calories"]
    assert "typical" not in result["confirmation"]


async def test_a_stated_amount_is_kept_and_a_singular_is_one(engine):
    eng = engine["engine"]
    result = await eng.handle({"add": [{"description": "an egg"}, {"description": "bacon", "quantity": 4,
                                                                    "unit": "slices"}]})
    assert result["status"] == "committed", result
    text = result["confirmation"]
    assert "a typical serving" not in text  # "an egg" is one egg, "4 slices" is four: nothing assumed
    expected = _kcal("egg", 61) + _kcal("bacon", 32)
    assert result["logged"]["calories"] == pytest.approx(round(expected, 2), abs=0.05)


async def test_weight_of_a_dish_scales_the_recipe(engine):
    result = await engine["engine"].handle({"add": [
        {"description": "breakfast burrito with eggs and cheese", "quantity": 250, "unit": "g"}]})
    assert result["status"] == "committed", result
    est = dish_estimates.estimate({"description": "breakfast burrito with eggs and cheese",
                                   "quantity": 250, "unit": "g"})
    assert sum(p["grams"] for p in est["built_from"]) == pytest.approx(250, abs=0.5)
    assert "scaled to 250 g" in result["confirmation"]


async def test_correcting_the_dish_contents_amends_the_same_entry(engine):
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle({"add": [{"description": "breakfast burrito with eggs and cheese"}]})
    assert first["status"] == "committed", first
    fixed = await eng.handle({"amend_ref": first["draft_ref"], "edit": [
        {"item_ref": "item1", "description": "breakfast burrito with eggs, cheese and bacon", "size": "large"}]})
    assert fixed["status"] == "amended", fixed
    assert fixed["entry_id"] == first["entry_id"] and len(store.entries) == 1
    assert fixed["logged"]["calories"] > first["logged"]["calories"]
    assert "bacon" in fixed["confirmation"]


async def test_kill_switch_restores_previous_behavior(engine, monkeypatch):
    monkeypatch.setenv("TYPICAL_SERVING_ESTIMATES", "off")
    result = await engine["engine"].handle({"add": [{"description": "salad with bacon and cheese"}]})
    assert result["status"] != "committed"
    assert "no nutrition source found" in result["confirmation"]


async def test_saved_estimate_survives_a_later_turn(engine):
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle({"add": [{"description": "breakfast burrito with sausage and cheese"},
                                      {"description": "chicken", "quantity": 6, "unit": "oz"}]})
    assert first["status"] == "needs_detail"  # weighed chicken still asks raw/cooked once
    assert "raw or cooked" in first["question"]
    engine["foods"]["cooked chicken"] = {"name": "chicken", "macro_source": "USDA FDC 171534: chicken",
                                         "calories": 255.0, "protein": 51.0, "carbs": 0.0, "fat": 5.0,
                                         "fiber": 0.0, "basis": "cooked"}
    done = await eng.handle({"draft_ref": first["draft_ref"], "edit": [{"item_ref": "item2",
                                                                        "preparation": "cooked"}]})
    assert done["status"] == "committed", done
    assert "(est., typical medium recipe)" in done["confirmation"]
    assert len(store.commits) == 1


@pytest.mark.parametrize("text", ["egg salad", "tuna salad", "chicken salad", "pizza", "big mac", "chorizo"])
def test_not_a_recipe_dish_returns_none(text):
    assert dish_estimates.estimate({"description": text}) is None


def test_every_source_stays_short_for_the_composite_validator():
    long = dish_estimates.estimate({"description": "large burrito with rice, black beans, chicken, steak, cheese, "
                                                   "sour cream, guacamole, salsa, lettuce, corn and peppers"})
    assert len(long["macro_source"]) <= 300 and long["macro_source"].startswith("BOUNDED ESTIMATE,")


def test_instructions_send_a_dish_and_its_contents_as_one_item_and_never_ask_for_amounts():
    assert "never ask for weights, sizes or amounts" in MEAL_DRAFT_INSTRUCTIONS
    assert "salad with bacon and cheese" in MEAL_DRAFT_INSTRUCTIONS
    assert "breakfast burrito with sausage and cheese" in MEAL_DRAFT_TOOL["description"]
