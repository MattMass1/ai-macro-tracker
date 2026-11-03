"""WP4: find_food wraps the existing resolvers behind the FoodFinder port.

Offline only. Every resolver here is a fake that returns dicts in the shape
the real resolver returns (FatSecret detail rows as built by
``food_lookup._fatsecret_result``, web estimates as built by
``web_nutrition_lookup``, generic whole foods from the real offline
``food_lookup.resolve_generic_whole_food``).

Covers the tool-level half of the real failures: the query carries the food
identity only (never "half" or "6 oz"), a named product that differs only
cosmetically is "matched to X", a different variant is never a match, and
fractions of a serving always work.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from food_agent import portion as p
from food_agent.contracts import FoodIdentity, Portion, ResolvedFood
from food_agent.find_food import FoodFinder, identity_query, is_estimate
from food_agent.ports import FindResult
from food_agent.ports import FoodFinder as FoodFinderPort

NUMBERED = re.compile(r"(^|\s)\d+\s*[.)](\s|$)")


# ---------------------------------------------------------------- fixtures

def _fatsecret(food_id: str, name: str, brand: str, serving: str, grams: float | None,
               kcal: float, protein: float, carbs: float, fat: float,
               fiber: float | None = 2.0, exact: bool = True) -> dict:
    """Shape of ``food_lookup._fatsecret_result``."""
    macros = {"calories": kcal, "protein": protein, "carbs": carbs, "fat": fat}
    if fiber is not None:
        macros["fiber"] = fiber
    result = {
        "name": name, "brand_name": brand, "macros_per_serving": macros,
        "serving_size": serving, "source": f"FatSecret: {food_id}",
        "attribution": {
            "provider": "FatSecret", "external_id": food_id,
            "verification_state": "provider_exact_identity" if exact else "provider_search_match",
            "source_url": None, "serving_grams": grams, "serving_basis": "per_serving",
            "confidence": 0.9, "cache_allowed": False,
            "attribution_text": "Nutrition data from FatSecret", "evidence_hash": "0" * 64,
        },
    }
    if grams:
        result["macros_per_100g"] = {k: round(v * 100 / grams, 2) for k, v in macros.items()}
    return result


QPC = _fatsecret("qpc", "Quarter Pounder with Cheese", "McDonald's", "1 sandwich", 219,
                 520, 30, 42, 26, 2)
DQPC = _fatsecret("dqpc", "Double Quarter Pounder with Cheese", "McDonald's", "1 sandwich", 300,
                  740, 48, 43, 42, 2)
QPCD = _fatsecret("qpcd", "Quarter Pounder with Cheese Deluxe", "McDonald's", "1 sandwich", 250,
                  590, 30, 45, 33, 3)
CHOC12 = _fatsecret("cal12", "Choc-A-Laka - 12oz", "Smoothie King", "12 fl oz", 340,
                    352, 13, 52, 11, 3)
CHOC12_NO_GRAMS = _fatsecret("cal12", "Choc-A-Laka - 12oz", "Smoothie King", "12 fl oz", None,
                             352, 13, 52, 11, 3)
POPEYES = _fatsecret("pcs", "Chicken Sandwich", "Popeyes", "1 sandwich", 230, 700, 28, 50, 42, 2)
CFA_GRILLED = _fatsecret("cfag", "Grilled Chicken Sandwich", "Chick-fil-A", "1 sandwich", 209,
                         390, 28, 44, 12, 4)


def _web_estimate(name: str, kcal: float, protein: float, carbs: float, fat: float,
                  fiber: float | None) -> dict:
    """Shape of ``web_nutrition_lookup.lookup`` for a per-serving source."""
    return {
        "name": name, "preparation": None,
        "source": "WEB ESTIMATE (unverified): example.com; per 1 serving",
        "basis": "per 1 serving", "assumption": "one source-defined serving; item count unknown",
        "estimate_provenance": "server_web_estimate", "carbohydrate_definition": "unknown",
        "attribution": {
            "provider": "openai_web_search", "source_type": "provider_web_search",
            "source_host": "example.com", "serving_basis": "per_serving",
            "items_per_serving_assumption": 1, "carbohydrate_definition": "unknown",
            "verification_state": ("unverified_web_estimate" if fiber is not None
                                   else "incomplete_unverified_web_estimate"),
            "cache_allowed": False, "evidence_hash": "1" * 64,
        },
        "macros_per_serving": {"calories": kcal, "protein": protein, "carbs": carbs,
                               "fat": fat, "fiber": fiber},
        "serving_size": "1 serving",
    }


class Resolver:
    """Records every query; answers from a table or a single fixed result."""

    def __init__(self, table: dict[str, dict | None] | None = None, *, default: dict | None = None,
                 error: Exception | None = None):
        self.table = {k.casefold(): v for k, v in (table or {}).items()}
        self.default = default
        self.error = error
        self.queries: list[str] = []

    async def __call__(self, query: str):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.table.get(query.casefold(), self.default)


def _suggest(options: list[str]):
    calls: list[str] = []

    async def suggest(query: str):
        calls.append(query)
        return list(options)
    suggest.calls = calls  # type: ignore[attr-defined]
    return suggest


def _finder(resolver, options: list[str] | None = None, ambiguous: bool = False) -> FoodFinder:
    return FoodFinder(resolver, suggest=_suggest(options or []), is_ambiguous=lambda _q: ambiguous)


def _identity(name: str, brand: str | None = None, kind: str = "restaurant",
              variant: str | None = None, preparation: str | None = None) -> FoodIdentity:
    return FoodIdentity(name=name, brand=brand, kind=kind, variant=variant, preparation=preparation)


HALF = Portion(kind="serving_fraction", amount=0.5, text="half")
ONE = Portion(kind="count", amount=1, text="1")


def _assert_plain_question(result: FindResult) -> None:
    assert result.question, result
    assert not NUMBERED.search(result.question), result.question
    assert "\n" not in result.question
    assert result.question.count("?") == 1, result.question


# ---------------------------------------------------------------- port shape

def test_finder_satisfies_the_port():
    assert isinstance(_finder(Resolver()), FoodFinderPort)


# ---------------------------------------------------------------- real failure 2

async def test_quarter_pounder_with_cheese_matches_and_query_has_no_portion_words():
    resolver = Resolver(default=QPC)
    result = await _finder(resolver).find(
        _identity("Quarter Pounder with Cheese", brand="McDonald's"), HALF)

    assert result.status == "matched", result
    assert result.note == "matched to McDonald's Quarter Pounder with Cheese"
    assert result.question is None
    food = result.food
    assert isinstance(food, ResolvedFood)
    assert food.food_id == "FatSecret:qpc" and food.provider == "FatSecret"
    assert food.external_id == "qpc"
    assert food.display_name == "McDonald's Quarter Pounder with Cheese"
    assert food.macro_source == "FatSecret: qpc"
    assert food.panel.calories == 520 and food.panel.protein == 30 and food.panel.fiber == 2
    basis = food.serving_basis
    assert (basis.basis, basis.amount, basis.unit, basis.count_noun, basis.grams) == \
        ("per_serving", 1.0, "count", "sandwich", 219.0)
    assert p.multiplier(HALF, basis) == Decimal("0.5")
    assert p.scale_panel(food.panel, p.multiplier(HALF, basis)).calories == 260

    assert resolver.queries == ["McDonald's Quarter Pounder with Cheese"]
    for query in resolver.queries:
        assert "half" not in query.casefold()


async def test_double_quarter_pounder_is_not_a_match_for_a_quarter_pounder():
    resolver = Resolver(default=DQPC)
    result = await _finder(resolver).find(
        _identity("Quarter Pounder with Cheese", brand="McDonald's"), HALF)
    assert result.status != "matched"
    assert result.food is None
    if result.status == "needs_detail":
        _assert_plain_question(result)
        assert "Double" in result.question


async def test_plain_quarter_pounder_does_not_match_the_cheese_or_deluxe_variant():
    for candidate in (QPC, QPCD):
        result = await _finder(Resolver(default=candidate)).find(
            _identity("Quarter Pounder", brand="McDonald's"), HALF)
        assert result.status != "matched", candidate["name"]
        assert result.food is None


async def test_close_variants_with_no_exact_hit_ask_one_plain_question():
    """The real resolver returns nothing for close variants and lists them as
    name options; the finder asks once, never as a numbered list."""
    resolver = Resolver(default=None)
    finder = _finder(resolver, options=["McDonald's Quarter Pounder with Cheese",
                                         "McDonald's Double Quarter Pounder with Cheese",
                                         "McDonald's Quarter Pounder with Cheese Deluxe"],
                     ambiguous=True)
    result = await finder.find(_identity("Quarter Pounder", brand="McDonald's"), HALF)
    assert result.status == "needs_detail" and result.food is None
    _assert_plain_question(result)


# ---------------------------------------------------------------- real failure 3

async def test_choco_laka_is_matched_to_choc_a_laka_and_six_oz_is_half():
    resolver = Resolver(default=CHOC12)
    six_oz = Portion(kind="mass", amount=6, unit="oz", text="6 oz instead of 12")
    result = await _finder(resolver).find(
        _identity("Choco Laka smoothie", brand="Smoothie King"), six_oz)

    assert result.status == "matched", result
    assert result.note == "matched to Smoothie King Choc-A-Laka"
    food = result.food
    assert food.display_name == "Smoothie King Choc-A-Laka"
    assert food.food_id == "FatSecret:cal12"
    assert (food.serving_basis.amount, food.serving_basis.unit) == (12.0, "fl_oz")
    assert p.multiplier(six_oz, food.serving_basis) == Decimal("0.5")
    for query in resolver.queries:
        assert not re.search(r"\d|\boz\b|instead", query, re.IGNORECASE), query


async def test_named_product_found_only_through_name_options_is_matched():
    """Production path: the strict resolver returns None for "Choco Laka"; the
    one cosmetic option is resolved by its own name and reported as matched."""
    option = "Smoothie King Choc-A-Laka - 12oz"
    resolver = Resolver({option: CHOC12}, default=None)
    finder = _finder(resolver, options=[option])
    result = await finder.find(_identity("Choco Laka", brand="Smoothie King"), HALF)
    assert result.status == "matched", result
    assert result.note == "matched to Smoothie King Choc-A-Laka"
    assert resolver.queries == ["Smoothie King Choco Laka", option]


async def test_portion_words_baked_into_the_name_are_never_searched():
    identity = _identity("Choc-A-Laka smoothie (6 oz instead of 12 oz)", brand="Smoothie King")
    query = identity_query(identity)
    assert "6" not in query and "oz" not in query.casefold() and "instead" not in query
    assert "Choc-A-Laka" in query and "Smoothie King" in query
    assert identity_query(_identity("half of a Quarter Pounder", brand="McDonald's")) == \
        "McDonald's Quarter Pounder"


async def test_volume_only_serving_with_a_gram_amount_asks_in_the_serving_unit():
    resolver = Resolver(default=CHOC12_NO_GRAMS)
    grams = Portion(kind="mass", amount=200, unit="g", text="200 g")
    result = await _finder(resolver).find(_identity("Choc-A-Laka", brand="Smoothie King"), grams)
    assert result.status == "needs_detail"
    _assert_plain_question(result)
    assert "fl oz" in result.question
    assert "raw" not in result.question.casefold()
    # The product is known; only the amount is missing. It is kept, not re-searched.
    assert result.food is not None and result.food.food_id == "FatSecret:cal12"


# ---------------------------------------------------------------- unknown nutrients

async def test_unknown_fiber_stays_none_and_web_estimate_keeps_provenance():
    estimate = _web_estimate("ahi tuna poke bowl", 610, 38, 70, 18, None)
    result = await _finder(Resolver(default=estimate)).find(
        _identity("ahi tuna poke bowl", kind="generic"), ONE)
    assert result.status == "matched", result
    assert result.food.panel.fiber is None
    assert result.food.panel.calories == 610
    assert "ESTIMATE" in result.food.macro_source
    assert is_estimate(result.food)


async def test_missing_fiber_key_is_unknown_not_zero():
    row = _fatsecret("qpc", "Quarter Pounder with Cheese", "McDonald's", "1 sandwich", 219,
                     520, 30, 42, 26, fiber=None)
    result = await _finder(Resolver(default=row)).find(
        _identity("Quarter Pounder with Cheese", brand="McDonald's"), HALF)
    assert result.status == "matched"
    assert result.food.panel.fiber is None
    assert not is_estimate(result.food)


# ---------------------------------------------------------------- identity guards

async def test_brand_mismatch_is_rejected():
    result = await _finder(Resolver(default=POPEYES)).find(
        _identity("Chicken Sandwich", brand="Chick-fil-A"), ONE)
    assert result.status != "matched"
    assert result.food is None


async def test_brand_spelling_is_cosmetic():
    result = await _finder(Resolver(default=CFA_GRILLED)).find(
        _identity("grilled chicken sandwich", brand="Chick fil A"), ONE)
    assert result.status == "matched", result
    assert result.note == "matched to Chick-fil-A Grilled Chicken Sandwich"


async def test_flavor_and_zero_sugar_are_identity():
    vanilla = _fatsecret("v", "Vanilla Protein Shake", "Fairlife", "1 bottle", 340, 150, 30, 4, 2)
    result = await _finder(Resolver(default=vanilla)).find(
        _identity("Chocolate Protein Shake", brand="Fairlife", kind="packaged"), ONE)
    assert result.status != "matched" and result.food is None

    coke = _fatsecret("c", "Coca-Cola", "Coca-Cola", "1 can (12 fl oz)", None, 140, 0, 39, 0)
    result = await _finder(Resolver(default=coke)).find(
        _identity("Coca-Cola Zero Sugar", brand="Coca-Cola", kind="packaged"), ONE)
    assert result.status != "matched" and result.food is None


async def test_plural_and_punctuation_are_cosmetic():
    nuggets = _fatsecret("n", "Chicken McNuggets (10 piece)", "McDonald's", "1 serving", 162,
                         410, 23, 26, 24, 2)
    result = await _finder(Resolver(default=nuggets)).find(
        _identity("chicken mcnugget", brand="McDonalds", variant="10 piece"), ONE)
    assert result.status == "matched", result


# ---------------------------------------------------------------- generic foods

async def test_three_eggs_count_works_against_one_egg():
    from food_lookup import resolve_generic_whole_food

    resolver = Resolver(default=resolve_generic_whole_food("eggs"))
    three = Portion(kind="count", amount=3, text="3")
    result = await _finder(resolver).find(_identity("eggs", kind="generic"), three)
    assert result.status == "matched", result
    assert resolver.queries == ["eggs"]
    basis = result.food.serving_basis
    assert basis.grams == pytest.approx(52.7)
    assert p.multiplier(three, basis) == Decimal("3")
    assert result.food.panel.protein == pytest.approx(12.6 * 0.527, abs=0.01)


async def test_bare_count_of_eggs_against_a_two_egg_serving_is_matched():
    """R-2: "2" eggs against a "2 large eggs" serving is one serving, not a question."""
    row = _fatsecret("eg2", "Eggs", "", "2 large eggs", 100, 143, 12.6, 0.7, 9.5, 0)
    two = Portion(kind="count", amount=2, text="2")
    result = await _finder(Resolver(default=row)).find(_identity("eggs", kind="generic"), two)
    assert result.status == "matched", result
    assert result.question is None
    assert p.multiplier(two, result.food.serving_basis,
                        _identity("eggs", kind="generic")) == Decimal("1")


async def test_weighed_chicken_without_raw_or_cooked_asks_raw_or_cooked():
    from food_lookup import resolve_generic_whole_food

    grams = Portion(kind="mass", amount=200, unit="g", text="200 g")
    resolver = Resolver(default=resolve_generic_whole_food("chicken breast"))
    result = await _finder(resolver).find(_identity("chicken breast", kind="generic"), grams)
    assert result.status == "needs_detail", result
    _assert_plain_question(result)
    assert "raw or cooked" in result.question
    assert resolver.queries == ["chicken breast"]


async def test_weighed_chicken_said_raw_is_matched_against_the_raw_row():
    from food_lookup import resolve_generic_whole_food

    grams = Portion(kind="mass", amount=200, unit="g", text="200 g")
    resolver = Resolver(default=resolve_generic_whole_food("raw chicken breast"))
    result = await _finder(resolver).find(
        _identity("chicken breast", kind="generic", preparation="raw"), grams)
    assert result.status == "matched", result
    assert p.multiplier(grams, result.food.serving_basis) == Decimal("2")


async def test_cooked_weight_never_uses_a_raw_panel():
    from food_lookup import resolve_generic_whole_food

    grams = Portion(kind="mass", amount=200, unit="g", text="200 g")
    resolver = Resolver(default=resolve_generic_whole_food("chicken breast"))
    result = await _finder(resolver).find(
        _identity("chicken breast", kind="generic", preparation="cooked"), grams)
    assert result.status != "matched"
    assert result.food is None


async def test_counted_grilled_chicken_sandwich_never_asks_raw_or_cooked():
    estimate = _web_estimate("grilled chicken sandwich", 420, 32, 40, 12, 3)
    result = await _finder(Resolver(default=estimate)).find(
        _identity("grilled chicken sandwich", kind="generic"), ONE)
    assert result.status == "matched", result
    assert result.question is None

    result = await _finder(Resolver(default=CFA_GRILLED)).find(
        _identity("Grilled Chicken Sandwich", brand="Chick-fil-A"), ONE)
    assert result.status == "matched" and result.question is None


async def test_restaurant_item_by_weight_never_asks_raw_or_cooked():
    grams = Portion(kind="mass", amount=110, unit="g", text="110 g")
    result = await _finder(Resolver(default=QPC)).find(
        _identity("Quarter Pounder with Cheese", brand="McDonald's"), grams)
    assert result.status == "matched", result
    assert p.multiplier(grams, result.food.serving_basis) == Decimal("0.5023")


# ---------------------------------------------------------------- no match

async def test_resolver_none_is_an_honest_no_match():
    resolver = Resolver(default=None)
    result = await _finder(resolver).find(_identity("Grandma's lasagna", kind="homemade"), ONE)
    assert result.status == "no_match"
    assert result.food is None
    assert result.note and "couldn't find" in result.note
    assert "lasagna" in result.note
    if result.question:
        _assert_plain_question(result)


async def test_resolver_error_is_no_match_not_a_crash():
    resolver = Resolver(error=RuntimeError("provider down"))
    result = await _finder(resolver).find(_identity("Big Mac", brand="McDonald's"), ONE)
    assert result.status == "no_match" and result.food is None
    assert "provider down" not in (result.note or "")


async def test_food_id_is_stable_without_an_external_id():
    preset = {"name": "Morning oats", "source": "Meal Preset: Morning oats",
              "macros_per_serving": {"calories": 380, "protein": 25, "carbs": 50,
                                     "fat": 9, "fiber": 7}}
    finder = _finder(Resolver(default=preset))
    first = await finder.find(_identity("Morning oats", kind="history"), ONE)
    second = await finder.find(_identity("morning oats", kind="history"), ONE)
    assert first.status == "matched", first
    assert first.food.food_id == second.food.food_id
    assert first.food.provider == "Meal Preset"
    other = await _finder(Resolver(default={**preset, "name": "Evening oats",
                                            "source": "Meal Preset: Evening oats"})).find(
        _identity("Evening oats", kind="history"), ONE)
    assert other.food.food_id != first.food.food_id


async def test_per_100g_only_source_with_no_amount_asks_how_much():
    off = {"name": "Greek yogurt", "source": "OpenFoodFacts barcode: 123",
           "macros_per_100g": {"calories": 97, "protein": 9, "carbs": 3.9, "fat": 5, "fiber": 0},
           "attribution": {"provider": "OpenFoodFacts", "external_id": "123",
                           "serving_basis": "per_100g", "verification_state": "exact_identifier"}}
    unspecified = Portion(kind="unspecified", text="")
    result = await _finder(Resolver(default=off)).find(
        _identity("Greek yogurt", kind="packaged"), unspecified)
    assert result.status == "needs_detail"
    _assert_plain_question(result)
    assert result.food is not None and result.food.serving_basis.basis == "per_100g"
