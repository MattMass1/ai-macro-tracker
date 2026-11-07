"""Serving units: a slice of bread, a cup of milk, a tablespoon of peanut butter.

Regression for the live report (2026-11-07): "a slice of sourdough bread" matched
USDA "Bread, french or vienna (includes sourdough)". The row had no single plain
"1 slice" measure, so the amount fell back to 100 g, the agent asked "how much?"
with 4 / 6 / 8 oz chips, and 6 oz (170 g, 463 kcal, 18.4 g protein) was logged.

Offline. USDA rows are synthetic in the real search-response shape; the measure
wording mirrors FDC's household-measure style, the weights are test values.
"""

from __future__ import annotations

import pytest

import serving_units
import usda_lookup
from food_agent.contracts import DraftItemState, FoodIdentity, Portion
from food_agent.drafts import item_label
from food_agent.find_food import to_resolved_food, with_serving_unit
from food_agent.portion import multiplier
from food_agent2.retrieval import Retrieval
from food_agent2.turn import portion_choices

from test_food_agent2_flow import Rig, add, ident, portion
from test_food_agent2_retrieval import FakeSources, usda_row

SOURDOUGH_ROW = usda_row(
    "Bread, french or vienna (includes sourdough)", fdc=18029, data_type="SR Legacy", kcal=272.0, carbs=51.9,
    fiber=2.2, measures=[('1 slice, medium (2-1/2" x 2" x 1-3/4")', 32), ('1 slice, large (4" x 2-1/2" x 1-3/4")', 64),
                         ("1 oz", 28.35)])
MILK_ROW = usda_row("Milk, whole, 3.25% milkfat, with added vitamin D", fdc=3, data_type="SR Legacy", kcal=61.0,
                    carbs=4.8, fiber=0.0, measures=[("1 cup", 244), ("1 quart", 976)])
BANANA_ROW = usda_row("Bananas, raw", fdc=4, data_type="SR Legacy", kcal=89.0, carbs=22.8, fiber=2.6,
                      measures=[('1 medium (7" to 7-7/8" long)', 118), ('1 large (8" to 8-7/8" long)', 136),
                                ("1 cup, sliced", 150)])


def web_per_100g(name, kcal=270.0):
    return {"name": name, "source": "WEB ESTIMATE (unverified): example.com; per 100 g",
            "estimate_provenance": "server_web_estimate",
            "macros_per_100g": {"calories": kcal, "protein": 10.0, "carbs": 50.0, "fat": 2.0, "fiber": 2.0},
            "attribution": {"provider": "OpenAI web_search", "cache_allowed": False,
                            "verification_state": "unverified_web_estimate"}}


def identity(name, kind="generic"):
    return FoodIdentity(name=name, kind=kind)


def count(amount, text):
    return Portion(kind="count", amount=amount, text=text)


async def resolve(name, amount, rows):
    sources = FakeSources(rows=rows)
    retrieval = Retrieval(sources)
    found = await retrieval.find(identity(name), amount, retrieval.scope())
    assert found.food is not None
    mult = multiplier(amount, found.food.serving_basis, identity(name))
    return found.food, (float(mult) if mult is not None else None)


def label(name, amount, food, mult):
    return item_label(DraftItemState(item_ref="i1", identity=identity(name), portion=amount, resolved=food,
                                     multiplier=mult))


# ---------------------------------------------------------------- the reported case


@pytest.mark.parametrize("words", ["a slice", "1 slice", "1 piece"])
async def test_a_slice_of_sourdough_is_one_slice_not_a_question_in_ounces(words):
    amount = count(1, words)
    food, mult = await resolve("sourdough bread", amount, {"sourdough bread": [SOURDOUGH_ROW]})
    assert food.serving_basis.count_noun == "slice" and food.serving_basis.grams == 32
    assert mult == 1
    assert food.panel.calories * mult == pytest.approx(87.0, abs=0.1), "one 32 g slice, not 170 g"
    assert food.panel.protein * mult < 5
    assert label("sourdough bread", amount, food, mult) == "Sourdough bread (1 slice)"


async def test_the_whole_dialogue_previews_one_slice_and_logs_it():
    rig = Rig(FakeSources(rows={"sourdough bread": [SOURDOUGH_ROW]}))
    rig.script.push(add((ident("sourdough bread"), portion("count", 1, text="a slice"))))
    first = await rig.say("a slice of sourdough bread")
    assert first.question is None and first.preview is not None
    assert first.reply.startswith("Sourdough bread (1 slice): ")
    assert first.preview.lines[0].nutrients.calories == pytest.approx(87.0, abs=0.1)
    await rig.say("Yes")
    assert rig.entries[0]["name"] == "Sourdough bread (1 slice)"
    assert rig.entries[0]["calories"] == pytest.approx(87.0, abs=0.1)


async def test_a_count_with_no_unit_is_slices_of_bread():
    food, mult = await resolve("sourdough", count(2, "2"), {"sourdough": [SOURDOUGH_ROW]})
    assert mult == 2 and label("sourdough", count(2, "2"), food, mult) == "Sourdough (2 slices)"


async def test_a_row_with_no_clear_slice_uses_the_pinned_typical_slice():
    row = usda_row("Bread, sourdough", fdc=7, kcal=270.0,
                   measures=[("1 slice, thin", 20), ("1 slice, thick", 50), ("1 oz", 28.35)])
    food, mult = await resolve("sourdough bread", count(1, "a slice"), {"sourdough bread": [row]})
    assert food.serving_basis.grams == serving_units.SLICE.grams and mult == 1
    assert food.attribution["serving_unit_assumed"] is True
    assert "typical" in food.attribution["serving_unit_source"]


async def test_a_weight_stays_a_weight_and_another_unit_is_left_alone():
    rows = {"sourdough bread": [SOURDOUGH_ROW]}
    weighed, mult = await resolve("sourdough bread", Portion(kind="mass", amount=2, unit="oz", text="2 oz"), rows)
    assert weighed.serving_basis.basis == "per_100g" and mult == pytest.approx(0.567, abs=0.001)
    loaf, mult = await resolve("sourdough bread", count(1, "a loaf"), rows)
    assert "serving_unit" not in loaf.attribution and mult is None


# ---------------------------------------------------------------- milk, fruit, spreads


async def test_milk_with_no_amount_is_one_cup_and_a_bare_8_oz_is_fluid():
    rows = {"milk": [MILK_ROW]}
    food, mult = await resolve("milk", Portion(kind="unspecified", text=""), rows)
    assert food.serving_basis.unit == "cup" and food.serving_basis.grams is None and mult == 1
    assert food.panel.calories == pytest.approx(148.8, abs=0.1)
    assert label("milk", Portion(kind="unspecified", text=""), food, mult) == "Milk (1 cup)"
    food, mult = await resolve("milk", Portion(kind="mass", amount=8, unit="oz", text="8 oz"), rows)
    assert mult == pytest.approx(1.0, abs=0.001), "8 oz of milk is a cup, not 227 g"


async def test_a_banana_is_one_medium_banana():
    amount = count(1, "a banana")
    food, mult = await resolve("banana", amount, {"banana": [BANANA_ROW]})
    assert food.serving_basis.grams == 118 and mult == 1
    assert label("banana", amount, food, mult) == "Banana (1 medium)"


async def test_a_tablespoon_of_peanut_butter_counts_without_a_question():
    row = usda_row("Peanut butter, smooth style, without salt", fdc=5, kcal=598.0, measures=[("2 tbsp", 32)])
    spoon = Portion(kind="volume", amount=1, unit="tbsp", text="1 tbsp")
    food, mult = await resolve("peanut butter", spoon, {"peanut butter": [row]})
    assert food.serving_basis.grams == 16 and mult == 1
    assert food.panel.calories == pytest.approx(95.7, abs=0.1)


# ---------------------------------------------------------------- the family table


@pytest.mark.parametrize("name,unit", [
    ("sourdough bread", "slice"), ("sourdough", "slice"), ("toasted sourdough", "slice"), ("rye bread", "slice"),
    ("whole wheat bread", "slice"), ("toast", "slice"), ("milk", "cup"), ("2% milk", "cup"), ("oat milk", "cup"),
    ("chocolate milk", "cup"), ("peanut butter", "tbsp"), ("bananas", "banana"), ("apple", "apple"),
])
def test_foods_with_a_serving_unit(name, unit):
    assert serving_units.for_food(name).unit == unit


@pytest.mark.parametrize("name", [
    "avocado toast", "french toast", "texas toast", "garlic bread", "banana bread", "cornbread", "flatbread",
    "peanut butter and jelly sandwich", "toast with butter", "milkshake", "coconut milk", "bread bowl",
    "chocolate peanut butter", "grilled chicken", "Dave's Killer Bread",
])
def test_foods_without_one(name):
    assert serving_units.for_food(name) is None


@pytest.mark.parametrize("text,kind,unit,expected", [
    ("a slice", "count", None, True), ("2", "count", None, True), ("", "unspecified", None, True),
    ("half a slice", "serving_fraction", None, True), ("2 oz", "mass", "oz", False), ("a loaf", "count", None, False),
    ("1 cup", "volume", "cup", False),
])
def test_when_the_bread_unit_applies(text, kind, unit, expected):
    assert serving_units.applies(serving_units.SLICE, text, kind, unit) is expected


def test_row_measure_prefers_plain_then_medium_and_never_guesses_between_sizes():
    family = serving_units.SLICE
    plain = [{"disseminationText": "1 slice", "gramWeight": 28}, {"disseminationText": "1 slice, medium", "gramWeight": 32}]
    assert serving_units.row_measure(plain, family) == ("1 slice", 28.0)
    sized = [{"disseminationText": "1 slice, small", "gramWeight": 16}, {"disseminationText": "1 slice, large", "gramWeight": 64}]
    assert serving_units.row_measure(sized, family) is None
    two_mediums = [{"disseminationText": "1 slice, medium", "gramWeight": 30},
                   {"disseminationText": "1 medium slice", "gramWeight": 34}]
    assert serving_units.row_measure(two_mediums, family) is None


# ---------------------------------------------------------------- other sources and the question


def test_a_per_100g_web_estimate_of_bread_is_counted_in_slices():
    food = to_resolved_food(web_per_100g("sourdough bread"))
    assert food is not None and food.serving_basis.basis == "per_100g"
    sliced = with_serving_unit(food, "sourdough bread", count(1, "a slice"))
    assert sliced.serving_basis.count_noun == "slice" and sliced.serving_basis.grams == serving_units.SLICE.grams
    assert sliced.panel.calories == pytest.approx(270.0 * 0.29, abs=0.1)
    assert with_serving_unit(food, "sourdough bread", Portion(kind="mass", amount=2, unit="oz", text="2 oz")) is food


async def test_refresh_keeps_the_serving_unit_the_amount_was_counted_in():
    sources = FakeSources(web={"sourdough bread": web_per_100g("sourdough bread")})
    retrieval = Retrieval(sources)
    shown = await retrieval.find_on_web(identity("sourdough bread"), count(1, "a slice"), retrieval.scope())
    assert shown.food.serving_basis.count_noun == "slice"
    item = DraftItemState(item_ref="i1", identity=identity("sourdough bread"), portion=count(1, "a slice"),
                          resolved=shown.food.model_copy(update={"panel": shown.food.panel.model_copy(
                              update={"calories": None})}), multiplier=1.0)
    fresh = await retrieval.refresh(item, retrieval.scope())
    assert fresh.serving_basis == shown.food.serving_basis


def test_the_how_much_question_offers_the_foods_own_unit():
    per_100g = usda_lookup.for_portion(usda_lookup.to_result(SOURDOUGH_ROW), "sourdough bread", "a loaf", "count")
    basis = to_resolved_food(per_100g).serving_basis
    assert portion_choices(basis, "sourdough bread") == ("1 slice", "2 slices", "3 slices")
    assert portion_choices(basis, "milk") == ("1/2 cup", "1 cup", "2 cups")
    assert portion_choices(basis, "chicken breast") == ("4 oz", "6 oz", "8 oz")
