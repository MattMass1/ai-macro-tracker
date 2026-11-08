"""Chain menu items said the way people say them.

Regression for the live report (2026-11-08): "Dominos personal pizza" and "Dominos 10 inch
cheese pizza whole pie" both ended "Not counted ... What was it, so I can add it?".
Causes: "pizza" in the brand "Domino's Pizza" erased the food word, "personal" never
matched the chain's "Small", `10"` never matched "10 inch", "whole pie" counted as part
of the name, the provider search was only tried with the full phrase, and the web check
needed every one of those words on the page.

Offline. Provider names are written in FatSecret's style; they are test data, not
captured provider records.
"""

from __future__ import annotations

import pytest

import food_lookup
import food_words
import web_nutrition_lookup
from food_agent.contracts import FoodIdentity, Portion
from food_agent2 import match
from food_agent2.retrieval import Retrieval, search_queries

from test_food_agent2_flow import Rig, add, ident, portion
from test_food_agent2_retrieval import FakeSources, fatsecret

BRAND = "Domino's Pizza"
MENU = [("1", 'Pepperoni Pizza - Small (10")', "1 slice", 250.0),
        ("2", 'Cheese Pizza - Small (10")', "1 slice", 200.0),
        ("3", 'Cheese Pizza - Large (14")', "1 slice", 290.0),
        ("4", "Cinnamon Bread Twists", "1 piece", 110.0)]


def dominos_sources(searchable=("domino's pizza",)):
    """FatSecret search is literal: only the plain phrases find anything."""
    hits = [{"food_id": food_id, "food_name": name, "brand_name": BRAND} for food_id, name, _s, _k in MENU]
    foods = {food_id: fatsecret(name, BRAND, food_id, serving=serving, kcal=kcal)
             for food_id, name, serving, kcal in MENU}
    return FakeSources(hits={text: hits for text in searchable}, foods=foods)


def dominos(name, variant=None):
    return FoodIdentity(name=name, brand="Domino's", kind="restaurant", variant=variant)


async def find(identity, amount, sources):
    retrieval = Retrieval(sources)
    return await retrieval.find(identity, amount, retrieval.scope())


# ---------------------------------------------------------------- the reported dialogue


class ServingSources(FakeSources):
    """Detail records in FatSecret's raw shape, read by the real adapter with the user's amount words."""

    def __init__(self, raw, **kw):
        super().__init__(**kw)
        self.raw = raw

    async def food(self, food_id, query, hit, prefer=""):
        self.calls.append(("food", str(food_id)))
        body = self.raw.get(str(food_id))
        return food_lookup._fatsecret_result(query, str(food_id), body, hit, prefer=prefer) if body else None


async def test_dominos_personal_pizza_is_the_whole_small_cheese_pizza():
    base = dominos_sources()
    raw = {"2": fatsecret_food(("1 slice", 200), ("1 pizza", 1200))}
    rig = Rig(ServingSources(raw, hits=base.hit_map))
    rig.script.push(add((ident("Personal pizza", "Domino's", "restaurant"), portion())))
    result = await rig.say("Dominos personal pizza")
    assert result.preview is not None, result.reply
    assert 'Cheese Pizza - Small (10")' in result.reply and "Not counted" not in result.reply
    assert result.preview.lines[0].nutrients.calories == 1200, "the pizza, not its first listed slice"


async def test_a_pizza_sold_only_by_the_slice_asks_how_many_slices():
    rig = Rig(dominos_sources())
    rig.script.push(add((ident("Personal pizza", "Domino's", "restaurant"), portion())))
    first = await rig.say("Dominos personal pizza")
    assert first.preview is None and "Not counted" not in first.reply
    assert first.quick_replies[:3] == ("1 slice", "2 slices", "4 slices")
    rig.script.push({"type": "revise_item", "item_ref": "i1", "identity": None, "clear_fields": [],
                     "portion": portion("count", 6, text="6 slices"), "modifications": None, "supplied": None})
    second = await rig.say("6 slices")
    assert second.preview.lines[0].nutrients.calories == 1200


async def test_dominos_10_inch_cheese_pizza_whole_pie_is_found():
    found = await find(dominos("10 inch cheese pizza", variant="whole pie"),
                       Portion(kind="count", amount=1, text="whole pie"), dominos_sources())
    assert found.status == "verified"
    assert found.food.display_name.endswith('Cheese Pizza - Small (10")')


async def test_a_stated_size_is_never_swapped_for_another():
    found = await find(dominos("large cheese pizza"), Portion(kind="count", amount=1, text="1 slice"),
                       dominos_sources())
    assert found.food.display_name.endswith('Cheese Pizza - Large (14")')
    found = await find(dominos("16 inch cheese pizza"), Portion(kind="count", amount=1, text="1 slice"),
                       dominos_sources())
    assert found.status == "needs_web", "no 16 inch pizza on the menu: never a 10 or 14 inch one"


def test_searches_go_from_the_phrase_to_brand_and_food():
    queries = search_queries(dominos("10 inch cheese pizza"), "Domino's 10 inch cheese pizza")
    assert queries == ["Domino's 10 inch cheese pizza", "Domino's cheese pizza", "Domino's pizza"]


# ---------------------------------------------------------------- the word rules


@pytest.mark.parametrize("said,shown", [
    ("personal pizza", 'Cheese Pizza - Small (10")'),
    ("10 inch cheese pizza", 'Cheese Pizza - Small (10")'),
    ("10in cheese pizza whole pie", 'Cheese Pizza - Small (10")'),
])
def test_brand_words_do_not_erase_the_food(said, shown):
    assert match.closeness(said, shown, BRAND) >= match.MIN_CLOSENESS
    assert match.conflict(said, shown) is None


@pytest.mark.parametrize("text,expected", [
    ('Cheese Pizza - Small (10")', 'Cheese Pizza - Small (10 inch )'),
    ("10-inch cheese pizza, whole pie", "10 inch cheese pizza, pizza"),
    ("Personal Pan Pizza", "small Pan Pizza"),
    ("whole milk", "whole milk"), ("apple pie", "apple pie"), ("whole wheat bread", "whole wheat bread"),
])
def test_food_words(text, expected):
    assert food_words.normalize(text) == expected


# ---------------------------------------------------------------- the provider serving


def fatsecret_food(*servings):
    return {"food_id": "2", "food_name": 'Cheese Pizza - Small (10")', "brand_name": BRAND,
            "servings": {"serving": [
                {"serving_description": description, "calories": str(kcal), "protein": "50", "carbohydrate": "140",
                 "fat": "40", "fiber": "6"} for description, kcal in servings]}}


def test_a_whole_pie_uses_the_whole_pizza_serving_even_over_1000_kcal():
    food = fatsecret_food(("1 slice", 200), ("1 pizza", 1200))
    hit = {"food_id": "2", "food_name": food["food_name"], "brand_name": BRAND}
    whole = food_lookup._fatsecret_result("Domino's cheese pizza", "2", food, hit, prefer="whole pie")
    assert whole["serving_size"] == "1 pizza" and whole["macros_per_serving"]["calories"] == 1200
    sliced = food_lookup._fatsecret_result("Domino's cheese pizza", "2", food, hit, prefer="2 slices")
    assert sliced["serving_size"] == "1 slice"
    assert food_lookup._fatsecret_result("Domino's cheese pizza", "2", food, hit)["serving_size"] == "1 slice"


# ---------------------------------------------------------------- the web check


def test_a_page_saying_small_10_inch_is_the_personal_10_inch_pizza_asked_for():
    raw = {"food_name": 'Domino\'s Hand Tossed Cheese Pizza, Small (10")', "preparation": "as served",
           "basis_amount": 1, "basis_unit": "serving", "serving_grams": None, "calories": 200, "protein": 8,
           "carbs": 25, "fat": 7, "fiber": 1, "portion_assumption": "1 slice", "source_url": "https://example.com/n",
           "source_title": "Nutrition", "evidence_excerpt": "Small cheese pizza slice 200 calories"}
    result = web_nutrition_lookup._result_from_evidence(raw, "Domino's personal 10 inch cheese pizza",
                                                         url_allowed=lambda url: True, provider="test")
    assert result["macros_per_serving"]["calories"] == 200
