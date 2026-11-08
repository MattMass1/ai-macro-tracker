"""Foods as people mean them: eggs are chicken eggs, and a correction is understood.

Regression for the live report (2026-11-08): "3 cooked eggs" was logged as "Egg, duck,
whole, fresh, cooked" (3 x 70 g duck eggs, 470 kcal), and "ITS A CHICKEN EGG" answered
"I couldn't find Cooked Chicken eggs". Causes: "cooked" was not a preparation word, so the
curated chicken egg was never tried; the row ranking penalized an added preparation but
not an added animal, so the duck row won; and USDA never writes "chicken" on an egg row,
so the corrected name found nothing.

Offline. USDA rows are synthetic in the real search-response shape.
"""

from __future__ import annotations

import pytest

import food_lookup
import food_words
import serving_units
from food_agent.contracts import FoodIdentity, Portion
from food_agent.portion import multiplier
from food_agent2 import match, menu_pick
from food_agent2.retrieval import Retrieval

from test_food_agent2_flow import Rig, add, ident, portion, revise
from test_food_agent2_retrieval import FakeSources, usda_row

DUCK = usda_row("Egg, duck, whole, fresh, cooked", fdc=1, data_type="SR Legacy", kcal=223.0, carbs=1.5, fiber=0.0,
                measures=[("1 egg", 70)])
QUAIL = usda_row("Egg, quail, whole, fresh, raw", fdc=4, data_type="SR Legacy", kcal=158.0, carbs=0.4, fiber=0.0,
                 measures=[("1 egg", 9)])
FRIED = usda_row("Egg, whole, cooked, fried", fdc=2, data_type="SR Legacy", kcal=196.0, carbs=0.8, fiber=0.0,
                 measures=[("1 large", 46)])
BOILED = usda_row("Egg, whole, cooked, hard-boiled", fdc=3, data_type="SR Legacy", kcal=155.0, carbs=1.1,
                  fiber=0.0, measures=[("1 large", 50), ("1 medium", 44), ("1 cup, chopped", 136)])
EGG_ROWS = [DUCK, QUAIL, FRIED, BOILED]
THREE = Portion(kind="count", amount=3, text="3")


def eggs(name="eggs", preparation="cooked"):
    return FoodIdentity(name=name, kind="generic", preparation=preparation)


async def find(identity, sources, picker=None, amount=THREE):
    retrieval = Retrieval(sources, picker=picker)
    return await retrieval.find(identity, amount, retrieval.scope())


def curated():
    return {"eggs": food_lookup.resolve_generic_whole_food("eggs")}


# ---------------------------------------------------------------- the reported dialogue


async def test_three_cooked_eggs_are_chicken_eggs_not_duck_eggs():
    rig = Rig(FakeSources(exact=curated(), rows={"cooked eggs": EGG_ROWS, "eggs": EGG_ROWS}))
    rig.script.push(add((ident("eggs", preparation="cooked"), portion("count", 3, text="3"))))
    result = await rig.say("3 cooked eggs")
    assert result.preview is not None, result.reply
    assert "duck" not in result.reply.casefold()
    assert result.preview.lines[0].nutrients.calories < 300, "three chicken eggs, not 470 kcal of duck egg"


async def test_its_a_chicken_egg_is_understood_as_a_correction():
    rig = Rig(FakeSources(rows={"cooked eggs": [DUCK, FRIED, BOILED]}))
    rig.script.push(add((ident("eggs", preparation="cooked"), portion("count", 3, text="3"))))
    await rig.say("3 cooked eggs")
    rig.script.push(revise("i1", identity={"name": "chicken eggs", "brand": None, "variant": None, "kind": None,
                                           "preparation": None}))
    result = await rig.say("ITS A CHICKEN EGG")
    assert result.preview is not None and "couldn't find" not in result.reply, result.reply
    assert "duck" not in result.reply.casefold()


# ---------------------------------------------------------------- database rows


async def test_without_a_curated_egg_the_duck_row_is_never_used():
    found = await find(eggs(), FakeSources(rows={"cooked eggs": EGG_ROWS}))
    assert found.food is not None and "duck" not in found.food.attribution["matched_name"].casefold()
    assert "quail" not in found.food.attribution["matched_name"].casefold()


async def test_three_eggs_from_a_database_row_are_three_large_eggs():
    found = await find(eggs(preparation="hard boiled"), FakeSources(rows={"hard boiled eggs": [BOILED]}))
    assert found.food.serving_basis.grams == 50
    assert float(multiplier(THREE, found.food.serving_basis, eggs())) == 3


async def test_the_picker_chooses_among_rows_that_are_all_the_food():
    picker_calls = []

    async def picker(said, candidates):
        picker_calls.append((said, candidates))
        return menu_pick.Pick("3")

    found = await find(eggs(), FakeSources(rows={"cooked eggs": EGG_ROWS}), picker=picker)
    assert found.food.attribution["matched_name"] == "Egg, whole, cooked, hard-boiled"
    offered = {c["id"] for c in picker_calls[0][1]}
    assert offered == {"2", "3"}, "duck and quail are a different food and are never offered"


async def test_a_single_eligible_row_needs_no_model():
    calls = []

    async def picker(said, candidates):
        calls.append(candidates)
        return menu_pick.NO_PICK

    found = await find(eggs(preparation="hard boiled"), FakeSources(rows={"hard boiled eggs": [DUCK, BOILED]}),
                       picker=picker)
    assert found.food is not None and calls == []


async def test_saying_the_other_animal_gets_that_animal():
    found = await find(FoodIdentity(name="duck eggs", kind="generic", preparation="cooked"),
                       FakeSources(rows={"cooked duck eggs": EGG_ROWS}))
    assert "duck" in found.food.attribution["matched_name"].casefold()


# ---------------------------------------------------------------- the word rules


@pytest.mark.parametrize("asked,row,different", [
    ("eggs", "Egg, duck, whole, fresh, cooked", True),
    ("cooked eggs", "Egg, quail, whole, fresh, raw", True),
    ("eggs", "Egg, whole, cooked, hard-boiled", False),
    ("duck eggs", "Egg, duck, whole, fresh, cooked", False),
    ("bacon", "Bacon, turkey, unprepared", True),
    ("bacon", "Pork, cured, bacon, cooked", False),
    ("turkey bacon", "Bacon, turkey, unprepared", False),
])
def test_another_animal_is_another_food(asked, row, different):
    assert (match.conflict(asked, row) == "species") is different


@pytest.mark.parametrize("text,expected", [
    ("chicken eggs", "eggs"), ("cooked chicken egg", "cooked egg"), ("hen's eggs", "eggs"),
    ("cow's milk", "milk"), ("chicken breast", "chicken breast"), ("chicken and eggs", "chicken and eggs"),
])
def test_the_default_kind_goes_unsaid(text, expected):
    assert food_words.default_kind(text) == expected


def test_cooked_is_a_preparation_word_for_counted_foods():
    assert match.without_preparation("cooked eggs") == "eggs"


@pytest.mark.parametrize("name", ["eggs", "hard boiled eggs", "scrambled eggs", "chicken eggs"])
def test_eggs_are_counted_as_large_eggs(name):
    assert serving_units.for_food(name) is serving_units.LARGE_EGG


@pytest.mark.parametrize("name", ["egg whites", "egg salad", "egg roll", "eggplant"])
def test_egg_dishes_are_not_eggs(name):
    assert serving_units.for_food(name) is not serving_units.LARGE_EGG
