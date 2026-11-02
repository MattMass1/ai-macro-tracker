"""Logging friction seen in Matt's own coach chats (2026-10-27 .. 2026-11-01).

Each test replays the shape of a real exchange where logging took several
turns, logged the wrong amount, or dead-ended. Provider responses are
synthetic fixtures (FatSecret- and web-estimate-shaped); no live call is made.

1. "3 eggs cooked in 1 table spoon of olive oil" asked for the amount, then
   logged 1,106 kcal: one "standard serving" of olive oil was 100 g.
2. "Half of a McDonald's quarter pounder" logged the whole burger (520 kcal):
   a leading "half" was never read as a quantity.
3. "Did you mean Smoothie King Choc-A-Laka - 12oz?" -> "Yes" -> "I still
   couldn't find nutrition": the yes was not bound to the option offered.
4. "Closest I found ... Should I log it?" -> "Yes" -> "I still couldn't find":
   the yes searched the web again instead of logging what it had shown.
5. "Small mini size packet m and ms" was split into "... packet m" + "ms".
"""
import asyncio
from uuid import uuid4

import pytest

from auth import bind_user, reset_user
from test_food_lookup import _fatsecret_get_hit
from test_live_coach import FakeVoiceStore, _import_server, _web_estimate


def _fresh(monkeypatch):
    srv = _import_server(monkeypatch)
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)
    srv._resolved_food_cache.clear()
    srv._web_estimate_confirmations.clear()
    srv._meal_questions.clear()
    srv.food_lookup.clear_search_cache()
    monkeypatch.setattr(srv.food_lookup.cofid_lookup, "lookup",
                        lambda _q: asyncio.sleep(0, result=None))
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)
    return srv, fake


def _no_web(monkeypatch, srv):
    calls = []

    async def lookup(query, **_kwargs):
        calls.append(query)
        return None

    monkeypatch.setattr(srv.food_lookup.web_nutrition_lookup, "lookup", lookup)
    return calls


def _provider(monkeypatch, srv, items):
    """FatSecret-shaped fixture: every search returns all ``items``; each
    item is (food_id, food_name, brand_name, serving, grams, kcal, p, c, f, fib)."""
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: "oauth1")
    by_id = {item[0]: item for item in items}
    searches = []

    async def request(data):
        if data["method"] == "foods.search":
            searches.append(data["search_expression"])
            return {"foods": {"food": [
                {"food_id": food_id, "food_name": name, "brand_name": brand}
                for food_id, name, brand, *_rest in items]}}
        food_id, name, brand, serving, grams, kcal, protein, carbs, fat, fiber = by_id[data["food_id"]]
        body = _fatsecret_get_hit(name, description=serving, grams=grams, calories=kcal,
                                  protein=protein, carbs=carbs, fat=fat, fiber=fiber)
        body["food"]["brand_name"] = brand
        body["food"]["food_id"] = food_id
        return body

    monkeypatch.setattr(srv.food_lookup, "_fatsecret_request", request)
    return searches


async def _turns(srv, session, *calls):
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        return [await handlers["log_meal"](f"canvas:{session}:t{index}", args)
                for index, args in enumerate(calls, start=1)]
    finally:
        reset_user(token)


# 1. Spoon measures for cooking fats ------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("portion", ["1 table spoon", "one table spoon", "1 tablespoon", "1 tbsp"])
async def test_eggs_cooked_in_a_tablespoon_of_olive_oil_log_in_one_turn(monkeypatch, portion):
    srv, fake = _fresh(monkeypatch)
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    _no_web(monkeypatch, srv)
    [result] = await _turns(srv, "OIL", {"components": [
        {"description": "3 eggs"},
        {"description": "olive oil", "portion": portion},
    ]})
    assert result["status"] == "committed", result
    # 3 x 52.7 g egg at 131 kcal/100 g + 13.5 g olive oil at 899 kcal/100 g.
    assert result["logged"]["calories"] == pytest.approx(207.1 + 121.4, abs=1)
    assert result["logged"]["fat"] == pytest.approx(14.2 + 13.5, abs=0.5)
    assert fake.insert_count == 1


@pytest.mark.asyncio
async def test_olive_oil_with_no_amount_is_one_tablespoon_and_says_so(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    _no_web(monkeypatch, srv)
    [result] = await _turns(srv, "OIL2", {"components": [
        {"description": "3 eggs"}, {"description": "olive oil"}]})
    assert result["status"] == "committed", result
    assert result["logged"]["calories"] == pytest.approx(207.1 + 121.4, abs=1)
    assert "1 tablespoon" in result["confirmation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("portion, grams", [("0.5 tbsp", 7.1), ("1 serving", 14.2), ("2 tsp", 9.4)])
async def test_butter_portions_are_spoon_sized_never_100_g(monkeypatch, portion, grams):
    srv, fake = _fresh(monkeypatch)
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    _no_web(monkeypatch, srv)
    [result] = await _turns(srv, "BUTTER", {"components": [
        {"description": "butter", "portion": portion}]})
    assert result["status"] == "committed", result
    assert result["logged"]["calories"] == pytest.approx(744 * grams / 100, abs=1)


def test_a_stated_spoon_amount_is_not_reported_as_an_assumption():
    from food_lookup import resolve_generic_whole_food
    stated = resolve_generic_whole_food("1 tablespoon olive oil")
    assert stated is not None and not str(stated.get("assumption") or "").startswith("assumed")
    assumed = resolve_generic_whole_food("olive oil")
    assert assumed is not None and "1 tablespoon" in assumed["assumption"]


# 2. A leading "half" is a quantity --------------------------------------------

_QPC = [
    ("qpc", "Quarter Pounder with Cheese", "McDonald's", "1 sandwich", 219, 520, 30, 42, 26, 2),
    ("dqpc", "Double Quarter Pounder with Cheese", "McDonald's", "1 sandwich", 300, 740, 48, 43, 42, 2),
    ("qpcd", "Quarter Pounder with Cheese Deluxe", "McDonald's", "1 sandwich", 250, 590, 30, 45, 33, 3),
]


def test_leading_fraction_words_parse_as_a_serving_quantity():
    from food_lookup import _clean_component
    for text in ("Half of a McDonald's quarter pounder", "half a McDonald's quarter pounder",
                 "half McDonald's quarter pounder", "1/2 McDonald's quarter pounder"):
        quantity, unit, identity = _clean_component(text)
        assert (quantity, unit) == (0.5, "servings"), text
        assert identity == "McDonald's quarter pounder", text
    assert _clean_component("a quarter of a pizza")[:2] == (0.25, "servings")
    # Not fractions of something else.
    assert _clean_component("half and half")[0] is None
    assert _clean_component("half chicken") == (None, None, "half chicken")
    for name in ("Quarter Pounder with Cheese", "quarter pounder", "a quarter pounder",
                 "half pound burger", "Half lb burger"):
        assert _clean_component(name)[0] is None, name
    # Half OF a named Quarter Pounder is still a half.
    assert _clean_component("half a quarter pounder")[:2] == (0.5, "servings")
    from food_lookup import leading_amount
    assert leading_amount("Quarter pounder with cheese") == ""
    assert leading_amount("Half of a McDonald's quarter pounder") == "Half of a"
    assert leading_amount("6 oz Smoothie King Choc-A-Laka") == "6 oz"


@pytest.mark.asyncio
async def test_half_of_a_named_restaurant_item_logs_half(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _QPC)
    _no_web(monkeypatch, srv)
    [result] = await _turns(srv, "QPC1", {"description": "Half of a McDonald's Quarter Pounder with Cheese"})
    assert result["status"] == "committed", result
    assert result["logged"]["calories"] == 260
    assert result["logged"]["protein"] == 15


@pytest.mark.asyncio
async def test_half_survives_the_which_one_question(monkeypatch):
    """Production 2026-10-30: 'Half of a McDonald's quarter pounder' -> which
    one? -> 'Quarter pounder with cheese' -> logged 520 (the whole burger)."""
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _QPC)
    _no_web(monkeypatch, srv)
    first, second = await _turns(
        srv, "QPC2",
        {"description": "Half of a McDonald's quarter pounder"},
        {"description": "Quarter pounder with cheese"})
    assert first["status"] == "needs_clarification", first
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 260
    assert fake.insert_count == 1


# 3. "Yes" to "Did you mean X?" logs X ------------------------------------------

_SMOOTHIE = [
    ("cal12", "Choc-A-Laka - 12oz", "Smoothie King", "12 fl oz", 340, 352, 13, 52, 11, 3),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["Yes", "yes that one", "Smoothie King Choc-A-Laka 12oz"])
async def test_yes_to_the_one_option_offered_logs_that_option(monkeypatch, answer):
    """Production 2026-10-29: four rounds of 'Did you mean Smoothie King
    Choc-A-Laka - 12oz?' -> 'Yes' -> 'I still couldn't find nutrition'."""
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _SMOOTHIE)
    _no_web(monkeypatch, srv)
    first, second = await _turns(
        srv, "SK1",
        {"description": "One Choco Laka Smoothie from Smoothie King"},
        {"description": answer})
    assert first["status"] == "needs_clarification", first
    assert first["options"] == ["Smoothie King Choc-A-Laka - 12oz"]
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 352
    assert fake.insert_count == 1


@pytest.mark.asyncio
async def test_yes_to_an_option_keeps_the_half_from_the_first_message(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _SMOOTHIE)
    _no_web(monkeypatch, srv)
    first, second = await _turns(
        srv, "SK2",
        {"description": "half a Choco Laka Smoothie from Smoothie King"},
        {"description": "Yes"})
    assert first["status"] == "needs_clarification", first
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 176


_CRUNCHWRAP = [
    ("cs", "Crunchwrap Supreme", "Taco Bell", "1 item", 254, 530, 15, 74, 20, 6),
    ("bcb", "Breakfast Crunchwrap - Bacon", "Taco Bell", "1 item", 220, 640, 20, 46, 41, 2),
]


@pytest.mark.asyncio
async def test_repeating_the_food_after_a_pick_question_logs_the_first_option(monkeypatch):
    """Production 2026-11-01: 'Taco Bell Crunchwrap' -> 'did you mean
    Crunchwrap Supreme or ...?' -> 'Taco Bell Crunchwrap' -> 'I still couldn't
    find nutrition'. The one question is spent: take the first option, say so."""
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _CRUNCHWRAP)
    _no_web(monkeypatch, srv)
    first, second = await _turns(
        srv, "TB1",
        {"description": "Taco Bell Crunchwrap"},
        {"description": "Taco Bell Crunchwrap"})
    assert first["status"] == "needs_clarification", first
    assert first["options"][0] == "Taco Bell Crunchwrap Supreme"
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 530
    assert "Crunchwrap Supreme" in second["confirmation"]
    assert "assumed" in second["confirmation"]


@pytest.mark.asyncio
async def test_naming_a_later_option_logs_that_option(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _CRUNCHWRAP)
    _no_web(monkeypatch, srv)
    _first, second = await _turns(
        srv, "TB2",
        {"description": "Taco Bell Crunchwrap"},
        {"description": "the bacon breakfast one"})
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 640


@pytest.mark.asyncio
async def test_no_to_a_did_you_mean_never_logs(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _SMOOTHIE)
    _no_web(monkeypatch, srv)
    _first, second = await _turns(
        srv, "SK3",
        {"description": "One Choco Laka Smoothie from Smoothie King"},
        {"description": "No"})
    assert second["status"] != "committed"
    assert fake.insert_count == 0


# 4. "Yes" to a shown web estimate logs exactly what was shown ----------------

@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["Yes", "Taco Bell Crunchwrap", "yes log it"])
async def test_yes_to_a_web_estimate_logs_what_was_shown_without_searching_again(monkeypatch, answer):
    srv, fake = _fresh(monkeypatch)
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    calls = []

    async def flaky(query, **_kwargs):
        calls.append(query)
        # The live web search is not repeatable: the second search finds nothing.
        return _web_estimate(query=query, basis_unit="serving",
                             food_name="Taco Bell Crunchwrap") if len(calls) == 1 else None

    monkeypatch.setattr(srv.food_lookup.web_nutrition_lookup, "lookup", flaky)
    first, second = await _turns(
        srv, "WEB1",
        {"description": "Taco Bell Crunchwrap"},
        {"description": answer})
    assert first["reason"] == "web_estimate_confirmation", first
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 120
    assert len(calls) == 1
    assert fake.insert_count == 1


# 5. Names that contain "and" -----------------------------------------------------

def test_m_and_ms_is_one_food():
    from food_lookup import _split_components
    for text in ("Small mini size packet m and ms", "fun size M&Ms", "a pack of m & m's",
                 "peanut M and M's"):
        assert len(_split_components(text)) == 1, text
    assert len(_split_components("eggs and toast")) == 2


@pytest.mark.asyncio
async def test_half_survives_when_the_answer_names_the_brand_and_item(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _QPC)
    _no_web(monkeypatch, srv)
    _first, second = await _turns(
        srv, "QPC3",
        {"description": "Half of a McDonald's quarter pounder"},
        {"description": "McDonald's Quarter Pounder with Cheese"})
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 260


_SOURDOUGH = [
    ("sd", "Sourdough Bread", "", "1 slice", 50, 144, 5.9, 27.2, 1.0, 1.2),
    ("fv", "French or Vienna Bread (Includes Sourdough)", "", "1 slice", 25, 68, 2.2, 13, 0.6, 0.6),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["It's home made sour dough bread just estimate", "just estimate"])
async def test_just_estimate_takes_the_first_option(monkeypatch, answer):
    """Production 2026-10-28: 'did you mean Sourdough Bread, ...?' -> 'It's
    home made sour dough bread just estimate' -> asked again, then refused."""
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _SOURDOUGH)
    _no_web(monkeypatch, srv)
    first, second = await _turns(
        srv, "SD1",
        {"description": "homemade sourdough bread"},
        {"description": answer})
    assert first["status"] == "needs_clarification", first
    assert first["options"][0] == "Sourdough Bread"
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 144
