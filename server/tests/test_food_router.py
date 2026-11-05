"""Source routing by food kind, the USDA whole-food lane, and the lane C guards.

RED tests cut from the 2026-11-04 live run (results-laneC-20261104-194917):
"3 sunny side up eggs" became a branded frozen meal, "Was it X or Y?" in 19
turns, "Something went wrong" after a verified save, "Yes" handed to the old
coach mid-draft, and "Green appld" logging nothing.
Offline only: every provider is a fake.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("APP_SHARED_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://fixture.invalid/test")

import httpx
import pytest

import food_lookup
import usda_lookup
from food_agent import contracts as c
from food_agent.coordinator import FoodAgent
from food_agent.drafts import MemoryDrafts
from food_agent.find_food import FoodFinder, ROUTE_KIND
from food_agent.ports import FindResult

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import food_agent_bench as bench  # noqa: E402

# ---------------------------------------------------------------- USDA lane

def _fdc(fdc_id, data_type, description, kcal, protein, carbs, fat, fiber, measures=()):
    nutrients = [{"nutrientId": i, "value": v} for i, v in
                 ((1008, kcal), (1003, protein), (1005, carbs), (1004, fat), (1079, fiber)) if v is not None]
    return {"fdcId": fdc_id, "dataType": data_type, "description": description, "foodNutrients": nutrients,
            "foodMeasures": [{"disseminationText": t, "gramWeight": g} for t, g in measures]}


EGG_FNDDS = _fdc(2707152, "Survey (FNDDS)", "Egg, whole, raw", 143, 12.4, 0.96, 9.96, 0,
                 measures=(("1 egg", 50), ("1 cup", 245)))
EGG_SR = _fdc(171287, "SR Legacy", "Egg, whole, raw, fresh", 143, 12.56, 0.72, 9.51, 0.0)
DUCK_EGG = _fdc(172189, "SR Legacy", "Egg, duck, whole, fresh, raw", 185, 12.8, 1.45, 13.8, 0.0)
NO_FIBER = _fdc(999, "Foundation", "Egg, bread, whole", 200, 5, 30, 2, None)


ROTISSERIE = _fdc(2705963, "Survey (FNDDS)", "Chicken breast, rotisserie, skin eaten", 175, 27, 0.09, 7, 0)
RAW_BREAST = _fdc(171077, "SR Legacy", "Chicken, broilers or fryers, breast, meat only, raw", 120, 22.5, 0, 2.6, 0.0)
FOUNDATION_BREAST = {"fdcId": 331960, "dataType": "Foundation", "description": "Chicken, breast, boneless, skinless, raw",
                     "foodNutrients": [{"nutrientId": 1003, "value": 22.5}, {"nutrientId": 1005, "value": 0},
                                       {"nutrientId": 1004, "value": 1.9}, {"nutrientId": 1079, "value": 0},
                                       {"nutrientId": 2048, "value": 106}]}  # no 1008 in search rows
GRILLED = _fdc(2705960, "Survey (FNDDS)", "Chicken breast, grilled with sauce, skin eaten", 200, 26, 3, 8, 0)


def test_select_prefers_fewest_extra_words_then_fndds_and_skips_unknown_fiber():
    chosen = usda_lookup.select("egg", [DUCK_EGG, EGG_SR, EGG_FNDDS, NO_FIBER])
    assert chosen["fdcId"] == 2707152  # FNDDS "Egg, whole, raw" over SR "..., fresh" over duck
    assert usda_lookup.select("duck egg", [DUCK_EGG, EGG_SR]) is DUCK_EGG
    assert usda_lookup.select("bread", [NO_FIBER]) is None  # carbs with unknown fiber is never 0
    assert usda_lookup.select("quinoa", [EGG_SR]) is None  # every query word must be present


def test_select_prefers_the_plain_raw_row_over_an_unasked_preparation():
    chosen = usda_lookup.select("chicken breast", [ROTISSERIE, GRILLED, FOUNDATION_BREAST, RAW_BREAST])
    assert chosen["fdcId"] == 331960  # boneless skinless raw: no form words, Atwater energy fills calories
    assert usda_lookup.to_result(chosen)["macros_per_100g"]["calories"] == 106
    assert usda_lookup.select("grilled chicken breast", [ROTISSERIE, GRILLED, RAW_BREAST]) is None


def test_to_result_is_resolver_shaped_cc0_and_portioned_by_household_measure():
    found = usda_lookup.to_result(EGG_FNDDS)
    assert found["attribution"]["provider"] == "USDA FDC" and found["attribution"]["cache_allowed"] is True
    assert found["attribution"]["verification_state"] == "official_source_exact_row"
    assert found["serving_size"] == "1 egg (50 g)" and found["macros_per_serving"]["calories"] == 71.5
    assert found["macros_per_100g"]["protein"] == 12.4
    assert found["source"].startswith("USDA FDC Survey (FNDDS): 2707152")
    bare = usda_lookup.to_result(EGG_SR)
    assert bare["serving_size"] == "100 g" and bare["attribution"]["serving_grams"] == 100.0


async def test_lookup_uses_the_api_key_header_and_caches(monkeypatch):
    monkeypatch.setenv("USDA_API_KEY", "k")
    usda_lookup.clear_cache()
    calls = []

    async def post(params):
        calls.append(params)
        return httpx.Response(200, json={"foods": [EGG_SR, EGG_FNDDS]})
    first = await usda_lookup.lookup("egg", post=post)
    second = await usda_lookup.lookup("Egg", post=post)
    assert first["name"] == "Egg, whole, raw" and second == first
    assert len(calls) == 1 and calls[0]["dataType"] == usda_lookup.DATA_TYPES
    assert "Branded" not in calls[0]["dataType"]


async def test_lookup_without_key_or_on_error_is_none_never_raises(monkeypatch):
    monkeypatch.delenv("USDA_API_KEY", raising=False)
    usda_lookup.clear_cache()
    assert await usda_lookup.lookup("egg") is None
    monkeypatch.setenv("USDA_API_KEY", "k")

    async def boom(params):
        raise httpx.ConnectError("down")
    assert await usda_lookup.lookup("egg", post=boom) is None


# ---------------------------------------------------------------- routing

def test_route_kind_is_code_checked_not_model_trusted():
    assert food_lookup.route_kind("eggs", "generic") == "generic"
    assert food_lookup.route_kind("toast", "homemade") == "homemade"
    assert food_lookup.route_kind("chick fil a grilled chicken", "generic") == "restaurant"
    assert food_lookup.route_kind("Cheetos", "packaged") == "packaged"
    assert food_lookup.route_kind("eggs", None) is None
    assert food_lookup.route_kind("eggs", "history") == "other"


@pytest.fixture
def lanes(monkeypatch):
    """Every provider recorded; FatSecret returns a branded frozen meal for anything."""
    seen = {"fatsecret": [], "usda": [], "cofid": [], "web": []}
    branded = {"name": "Sunny Side Egg with Tomato", "brand_name": "Trifecta",
               "macros_per_serving": {"calories": 300, "protein": 20, "carbs": 10, "fat": 20, "fiber": 2},
               "serving_size": "1 meal", "source": "FatSecret: 1",
               "attribution": {"provider": "FatSecret", "external_id": "1",
                               "verification_state": "provider_exact_identity", "cache_allowed": False}}

    async def fatsecret(query, **_):
        seen["fatsecret"].append(query)
        return dict(branded)

    async def usda(query, **_):
        seen["usda"].append(query)
        return usda_lookup.to_result(EGG_FNDDS) if "egg" in query else None

    async def cofid(query):
        seen["cofid"].append(query)
        return None

    async def web(query):
        seen["web"].append(query)
        return {"name": query, "source": "WEB ESTIMATE (unverified): x; per 1 serving",
                "estimate_provenance": "server_web_estimate", "serving_size": "1 serving",
                "macros_per_serving": {"calories": 100, "protein": 1, "carbs": 2, "fat": 3, "fiber": 0},
                "attribution": {"provider": "openai_web_search", "verification_state": "unverified_web_estimate",
                                "cache_allowed": False}}
    monkeypatch.setattr(food_lookup, "_fatsecret_provider_mode", lambda: "oauth1")
    monkeypatch.setattr(food_lookup, "search_fatsecret", fatsecret)
    monkeypatch.setattr(usda_lookup, "lookup", usda)
    monkeypatch.setattr(food_lookup.cofid_lookup, "lookup", cofid)
    monkeypatch.setattr(food_lookup.web_nutrition_lookup, "lookup", web)
    return seen


async def test_whole_food_never_reaches_fatsecret(lanes):
    found = await food_lookup.resolve_food("sunny side up eggs", atomic=True, kind="generic")
    assert found["attribution"]["provider"] == "USDA FDC", found
    assert lanes["fatsecret"] == [] and lanes["usda"]


async def test_curated_pinned_row_beats_usda_for_an_exact_whole_food(lanes):
    found = await food_lookup.resolve_food("eggs", atomic=True, kind="generic")
    assert found["source"].startswith("Generic: chicken egg")
    assert lanes["usda"] == [] and lanes["fatsecret"] == []


async def test_whole_food_miss_goes_to_web_estimate_not_fatsecret(lanes):
    found = await food_lookup.resolve_food("sourdough toast", atomic=True, kind="homemade")
    assert found["estimate_provenance"] == "server_web_estimate"
    assert lanes["fatsecret"] == [] and lanes["web"] == ["sourdough toast"]


async def test_restaurant_and_packaged_go_to_fatsecret_and_skip_cofid(lanes):
    found = await food_lookup.resolve_food("Trifecta sunny side egg", atomic=True, kind="packaged")
    assert found["attribution"]["provider"] == "FatSecret"
    assert lanes["usda"] == [] and lanes["cofid"] == []


async def test_unknown_kind_keeps_the_old_cascade(lanes):
    await food_lookup.resolve_food("sunny side up eggs", atomic=True)
    # Unchanged legacy order: CoFID, then FatSecret over the query variants; USDA is lane-only.
    assert lanes["cofid"] == ["sunny side up eggs"] and lanes["fatsecret"] and lanes["usda"] == []


async def test_finder_passes_the_item_kind_to_the_resolver():
    kinds = []

    async def resolve(query):
        kinds.append(ROUTE_KIND.get())
        return None

    async def suggest(query):
        return []
    finder = FoodFinder(resolve, suggest=suggest, is_ambiguous=lambda _q: False)
    await finder.find(c.FoodIdentity(name="eggs", brand=None, kind="generic"), c.Portion(kind="count", amount=3, text="3"))
    assert kinds == ["generic"] and ROUTE_KIND.get() is None


# ---------------------------------------------------------------- never "which one"

def _fs(food_id, name, brand, kcal=390):
    return {"name": name, "brand_name": brand, "serving_size": "1 sandwich (209 g)", "source": f"FatSecret: {food_id}",
            "macros_per_serving": {"calories": kcal, "protein": 28, "carbs": 44, "fat": 12, "fiber": 4},
            "attribution": {"provider": "FatSecret", "external_id": food_id, "serving_grams": 209,
                            "verification_state": "provider_exact_identity", "cache_allowed": False}}


async def test_close_variants_pick_the_closest_and_say_matched_to_never_ask():
    table = {"chick-fil-a grilled chicken sandwich": _fs("a", "Grilled Chicken Sandwich", "Chick-fil-A"),
             "chick-fil-a grilled chicken club sandwich": _fs("b", "Grilled Chicken Club Sandwich", "Chick-fil-A", 520)}

    async def resolve(query):
        return table.get(query.casefold())

    async def suggest(query):
        return ["Chick-fil-A Grilled Chicken Sandwich", "Chick-fil-A Grilled Chicken Club Sandwich"]
    finder = FoodFinder(resolve, suggest=suggest, is_ambiguous=lambda _q: True)
    result = await finder.find(c.FoodIdentity(name="Grilled Chicken Club", brand="Chick-fil-A", kind="restaurant"),
                               c.Portion(kind="unspecified", text=""))
    assert result.status == "matched", result
    assert result.food.external_id == "b" and result.note.startswith("matched to Chick-fil-A Grilled Chicken Club")
    assert result.question is None


async def test_nothing_close_is_no_match_not_a_variant_list():
    async def resolve(query):
        return None

    async def suggest(query):
        return ["Bojangles Grilled Chicken Club", "Chick-fil-A Grilled Chicken Club"]
    finder = FoodFinder(resolve, suggest=suggest, is_ambiguous=lambda _q: True)
    result = await finder.find(c.FoodIdentity(name="Grilled Chicken Club", brand="Chick-fil-A", kind="restaurant"),
                               c.Portion(kind="unspecified", text=""))
    assert result.status == "no_match"
    assert not result.question or "Was it" not in result.question


async def test_green_apple_matches_the_curated_apple():
    from food_lookup import resolve_generic_whole_food
    apple = resolve_generic_whole_food("apple")

    async def resolve(query):
        return apple

    async def suggest(query):
        return []
    finder = FoodFinder(resolve, suggest=suggest, is_ambiguous=lambda _q: False)
    result = await finder.find(c.FoodIdentity(name="apple", brand=None, variant="green", kind="generic"),
                               c.Portion(kind="count", amount=1, text="1"))
    assert result.status == "matched", result


# ---------------------------------------------------------------- honest readback

def test_receipt_accepts_a_composite_macro_source():
    row = {"id": "e1", "entry_revision": 1, "name": "chicken egg (3) + bagel", "calories": 457, "protein": 28.9,
           "carbs": 49, "fat": 16.2, "fiber": 3, "macro_source": "Composite: " + "x" * 600}
    receipt = c.Receipt.from_saved_row(row, verified=True)
    assert receipt.verified and receipt.macro_source.startswith("Composite:")


# ---------------------------------------------------------------- "yes" stays in the draft

class _Script:
    def __init__(self):
        self.queue = []

    def push(self, *actions):
        self.queue.append({"schema_version": c.SCHEMA_VERSION, "actions": list(actions)})

    async def __call__(self, message, context):
        return c.AgentTurn.model_validate(self.queue.pop(0))


class _NoMatchFinder:
    async def find(self, identity, portion):
        return FindResult("no_match", None, question=f"One slice of what?")


async def test_short_answer_to_an_open_question_is_never_handed_to_the_old_path():
    drafts, script = MemoryDrafts(), _Script()
    agent = FoodAgent(drafts, script, _NoMatchFinder(), None)
    script.push({"type": "add_items", "items": [{"item_ref": None, "identity": {
        "name": "New York style pizza", "brand": None, "variant": None, "kind": "generic", "preparation": None},
        "portion": {"kind": "count", "amount": 1.5, "unit": None, "text": "1.5 slices"}, "modifications": []}]})
    first = await agent.turn("1.5 slices of New York style pizza", session_ref="s", turn_ref="t1")
    assert first.question == "One slice of what?"
    script.push({"type": "not_food"})
    reply = await agent.turn("Yes", session_ref="s", turn_ref="t2")
    assert reply.handed_off is False and reply.question
    script.push({"type": "not_food"})
    chat = await agent.turn("how is my protein looking for the week so far", session_ref="s", turn_ref="t3")
    assert chat.handed_off is True  # a real non-food message still leaves


# ---------------------------------------------------------------- scorer

def test_scorer_requires_explicit_evidence_for_reference_qualifiers_and_brands():
    egg = {"name": "egg", "brand": None, "variant": None, "preparation": None}
    assert bench.identity_matches(egg, "chicken egg")
    assert not bench.identity_matches(egg, "Egg, whole, raw")
    assert not bench.identity_matches(egg, "Egg Salad")
    assert not bench.identity_matches(egg, "Egg, duck, whole, fresh, raw")
    bagel = {"name": "blueberry bagel", "brand": None, "variant": None, "preparation": None}
    assert not bench.identity_matches(bagel, "Thomas' Blueberry Bagel")
    assert bench.identity_matches({**bagel, "accepted_names": ["Thomas' Blueberry Bagel"]},
                                  "Thomas' Blueberry Bagel")
    assert not bench.identity_matches(bagel, "Thomas' Mini Blueberry Bagel")
    branded = {"name": "Quarter Pounder", "brand": "McDonald's", "variant": None, "preparation": None}
    assert not bench.identity_matches(branded, "McDonald's Quarter Pounder with Cheese")


def test_scorer_does_not_infer_item_counts_from_unspecified_servings():
    gold = {"portion": {"kind": "count", "amount": 2, "unit": "wrap"}}
    saved = {"multiplier": 2.0, "serving_basis": {"unit": "count", "amount": 1.0, "count_noun": "serving",
                                                   "text": "1 serving", "basis": "per_serving"}}
    assert bench.consumed_matches(gold, saved) is None
    assert bench.consumed_matches(gold, {**saved, "multiplier": 1.0}) is None
    counted = {**saved, "serving_basis": {"unit": "count", "amount": 1, "count_noun": "wrap", "text": "1 wrap"}}
    assert bench.consumed_matches(gold, counted) is True
