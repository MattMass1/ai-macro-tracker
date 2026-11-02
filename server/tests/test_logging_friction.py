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
                 "1/2 McDonald's quarter pounder"):
        quantity, unit, identity = _clean_component(text)
        assert (quantity, unit) == (0.5, "servings"), text
        assert identity == "McDonald's quarter pounder", text
    assert _clean_component("a quarter of a pizza")[:2] == (0.25, "servings")
    # Not fractions of something else.
    assert _clean_component("half and half")[0] is None
    assert _clean_component("half chicken") == (None, None, "half chicken")
    # A bare leading "half" is part of many names (Half Moon cookie, half &
    # half); only "half a", "half of", "a half" and "1/2" are fractions.
    assert _clean_component("half McDonald's quarter pounder")[0] is None
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


# Review round 1 (independent reviewer, 2026-11-01) ------------------------------

def _web_once(monkeypatch, srv):
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    calls = []

    async def lookup(query, **_kwargs):
        calls.append(query)
        return _web_estimate(query=query, basis_unit="serving", food_name=query)

    monkeypatch.setattr(srv.food_lookup.web_nutrition_lookup, "lookup", lookup)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, calories", [
    ("2 Taco Bell Crunchwraps", 240), ("half a Taco Bell Crunchwrap", 60),
    ("1/2 taco bell crunchwrap", 60)])
async def test_a_new_amount_in_the_yes_is_not_dropped(monkeypatch, answer, calories):
    srv, fake = _fresh(monkeypatch)
    _web_once(monkeypatch, srv)
    first, second = await _turns(
        srv, "R1", {"description": "Taco Bell Crunchwrap"}, {"description": answer})
    assert first["reason"] == "web_estimate_confirmation", first
    assert second.get("logged", {}).get("calories") in (None, calories), second
    assert not (second["status"] == "committed" and second["logged"]["calories"] != calories)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["dont log that one", "no don't", "cancel that", "never mind"])
async def test_a_negative_reply_to_a_shown_estimate_logs_nothing(monkeypatch, answer):
    srv, fake = _fresh(monkeypatch)
    _web_once(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R7", {"description": "Taco Bell Crunchwrap"}, {"description": answer})
    assert second["status"] != "committed", second
    assert fake.insert_count == 0


def test_fraction_words_inside_food_names_stay_names():
    from food_lookup import _clean_component
    for name in ("half & half", "half n half", "Half Moon cookie", "half baked ice cream",
                 "Half Smoke", "half sour pickle", "quarter dark chicken",
                 "half a dozen eggs", "half a chicken"):
        assert _clean_component(name)[1] != "servings", name


def test_a_half_and_one_half_are_halves():
    from food_lookup import _clean_component
    for text in ("a half McDonald's Quarter Pounder with Cheese",
                 "one half McDonald's Quarter Pounder with Cheese"):
        assert _clean_component(text) == (0.5, "servings", "McDonald's Quarter Pounder with Cheese"), text
    # A spoken half of a unit is still that unit.
    assert _clean_component("half a tablespoon of olive oil") == (0.5, "tablespoon", "olive oil")


def test_split_spellings_still_match_provider_names_exactly():
    from food_lookup import _exact_name_match
    assert _exact_name_match("peanut m&m's", {"food_name": "Peanut M&M's"})
    assert _exact_name_match("sour dough bread", {"food_name": "Sour Dough Bread"})
    assert _exact_name_match("home made chili", {"food_name": "Home Made Chili"})


@pytest.mark.asyncio
async def test_the_whole_one_overrides_the_first_half(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _CRUNCHWRAP)
    _no_web(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R5", {"description": "half a Taco Bell Crunchwrap"},
        {"description": "the whole Taco Bell Crunchwrap Supreme"})
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 530


def test_a_serving_of_butter_says_what_a_serving_is():
    from food_lookup import resolve_generic_whole_food
    assert "1 tablespoon" in resolve_generic_whole_food("1 serving butter")["assumption"]
    assert "assumption" not in resolve_generic_whole_food("2 tsp butter")


# Review round 2 ---------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["dont log the taco bell crunchwrap",
                                    "no not the taco bell crunchwrap"])
async def test_a_refusal_that_names_the_food_logs_nothing(monkeypatch, answer):
    srv, fake = _fresh(monkeypatch)
    _web_once(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R2-1", {"description": "Taco Bell Crunchwrap"}, {"description": answer})
    assert second["status"] != "committed", second
    assert fake.insert_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["I don't mind", "I don't care, log it", "dont care"])
async def test_dont_care_and_dont_mind_are_a_yes(monkeypatch, answer):
    srv, fake = _fresh(monkeypatch)
    _web_once(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R2-4", {"description": "Taco Bell Crunchwrap"}, {"description": answer})
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 120


# Review round 3: any no-word blocks the yes shortcuts -------------------------

_NO_REPLIES = ["don't log it", "dont log it", "no, don't log it", "please don't log it",
               "that's not fine", "no, I'm not sure", "never mind, just log it",
               "no, just log it", "nah thats fine"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", _NO_REPLIES)
async def test_a_reply_with_a_no_word_never_logs_a_shown_estimate(monkeypatch, answer):
    srv, fake = _fresh(monkeypatch)
    _web_once(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R3-1", {"description": "Taco Bell Crunchwrap"}, {"description": answer})
    assert second["status"] != "committed", second
    assert fake.insert_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", _NO_REPLIES + ["not the supreme"])
async def test_a_reply_with_a_no_word_never_takes_the_first_option(monkeypatch, answer):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _CRUNCHWRAP)
    _no_web(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R3-2", {"description": "Taco Bell Crunchwrap"}, {"description": answer})
    assert second["status"] != "committed", second
    assert fake.insert_count == 0


_CFA = [
    ("spicy", "Spicy Chicken Sandwich", "Chick-fil-A", "1 sandwich", 214, 450, 28, 45, 18, 2),
    ("deluxe", "Deluxe Chicken Sandwich", "Chick-fil-A", "1 sandwich", 240, 500, 30, 46, 22, 2),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["no, the deluxe", "not spicy, the deluxe one", "no the deluxe one",
                                    "not the deluxe, the spicy", "not the spicy one but the deluxe",
                                    "yes the deluxe, no pickles", "no problem, the deluxe"])
async def test_a_no_word_about_the_options_logs_nothing(monkeypatch, answer):
    """The server does not parse "no, but ..." sentences (five review rounds of
    phrase rules each let a refusal or a wrong option through). A no-word that
    touches the question declines; the user names the pick plainly."""
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _CFA)
    _no_web(monkeypatch, srv)
    first, second = await _turns(
        srv, "R3-3", {"description": "Chick-fil-A chicken sandwich"}, {"description": answer})
    assert first["status"] == "needs_clarification" and len(first["options"]) == 2, first
    assert second["status"] != "committed", second
    assert fake.insert_count == 0


@pytest.mark.asyncio
async def test_naming_the_option_plainly_picks_it(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _CFA)
    _no_web(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R3-3b", {"description": "Chick-fil-A chicken sandwich"}, {"description": "the deluxe"})
    assert second["status"] == "committed" and second["logged"]["calories"] == 500, second


def test_a_no_word_with_a_new_food_is_a_new_request():
    from test_live_coach import _import_server
    import importlib
    srv = importlib.import_module("server")
    pending = {"tokens": srv.food_lookup._canon_food_tokens("Taco Bell Crunchwrap"),
               "options": ["Taco Bell Crunchwrap Supreme"]}
    assert srv._negated_reply("burger with no onions", pending) == "new"
    assert srv._negated_reply("dont log it", pending) == "decline"
    assert srv._negated_reply("not the crunchwrap but a chalupa", pending) == "decline"
    assert srv._negated_reply("2 Taco Bell Crunchwraps", pending) is None
    assert srv._negated_reply("I don't care", pending) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, calories", [("yes, half", 60), ("ok half", 60), ("yes, two", 240)])
async def test_an_amount_word_after_yes_scales(monkeypatch, answer, calories):
    srv, fake = _fresh(monkeypatch)
    _web_once(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R3-4", {"description": "Taco Bell Crunchwrap"}, {"description": answer})
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == calories


def test_a_blocked_fraction_is_no_quantity_at_all():
    from food_lookup import _clean_component
    assert _clean_component("a half a chicken")[0] is None


@pytest.mark.asyncio
async def test_yes_with_a_count_after_it_scales(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _web_once(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R2-5", {"description": "Taco Bell Crunchwrap"}, {"description": "yes 2"})
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 240


@pytest.mark.asyncio
async def test_a_shown_meal_of_several_foods_is_not_scaled_as_one(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _web_once(monkeypatch, srv)
    first, second = await _turns(
        srv, "R2-2", {"description": "Taco Bell Crunchwrap and Baja Blast"},
        {"description": "2 Taco Bell Crunchwraps and Baja Blast"})
    assert first["reason"] == "web_estimate_confirmation", first
    # Several shown foods are never scaled together; the reply is resolved again.
    assert not (second["status"] == "committed" and second["logged"]["calories"] == 480), second
    if second["status"] == "committed":
        assert " + 2 Taco Bell Crunchwraps and Baja Blast + " not in second["logged"]["name"]


def test_fraction_exclusions_cannot_be_skipped_past_the_article():
    from food_lookup import _clean_component
    for name in ("half of a chicken", "1/2 a chicken", "a half a chicken",
                 "half of a dozen eggs", "1/2 of a dozen donuts"):
        assert _clean_component(name)[1] != "servings", name
    for text, identity in (("half a chicken breast", "chicken breast"),
                           ("half a chicken sandwich", "chicken sandwich"),
                           ("half of a chicken breast", "chicken breast"),
                           ("half the pizza", "pizza"), ("half my sandwich", "sandwich"),
                           ("half a rack of ribs", "rack of ribs")):
        assert _clean_component(text) == (0.5, "servings", identity), text
    assert _clean_component("one half cup of rice") == (0.5, "cup", "rice")


# Review round 4: "not"/"dont" negate to the next comma --------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["not the breakfast bacon one", "not the Breakfast Crunchwrap Bacon"])
async def test_a_negated_multi_word_option_is_never_logged(monkeypatch, answer):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _CRUNCHWRAP)
    _no_web(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R4-1", {"description": "Taco Bell Crunchwrap"}, {"description": answer})
    assert second["status"] != "committed", second


_REFUSALS = ["I don't want the spicy one", "dont want the deluxe", "no way", "not now",
             "not today", "don't add it", "don't bother", "please don't save that",
             "don't record it", "not hungry anymore", "never happened", "nope, wrong food",
             "no don't log it yet", "not the spicy one", "yes please, nothing else",
             "yes, no changes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", _REFUSALS)
async def test_no_word_replies_never_log_the_option(monkeypatch, answer):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _CFA)
    _no_web(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R4-2", {"description": "Chick-fil-A chicken sandwich"}, {"description": answer})
    assert second["status"] != "committed", second
    assert fake.insert_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", _REFUSALS)
async def test_no_word_replies_never_log_the_shown_estimate(monkeypatch, answer):
    srv, fake = _fresh(monkeypatch)
    calls = _no_web(monkeypatch, srv)
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)

    async def once(query, **_kwargs):
        calls.append(query)
        # Only the first message's food has an estimate; nothing else does.
        return (_web_estimate(query=query, basis_unit="serving", food_name=query)
                if query == "Taco Bell Crunchwrap" else None)

    monkeypatch.setattr(srv.food_lookup.web_nutrition_lookup, "lookup", once)
    _first, second = await _turns(
        srv, "R4-3", {"description": "Taco Bell Crunchwrap"}, {"description": answer})
    assert second["status"] != "committed", second
    assert fake.insert_count == 0


@pytest.mark.asyncio
async def test_a_new_food_after_a_no_word_is_its_own_request(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _CFA + _CRUNCHWRAP)
    _no_web(monkeypatch, srv)
    first, second = await _turns(
        srv, "R4-6", {"description": "Chick-fil-A chicken sandwich"},
        {"description": "no, a Taco Bell Crunchwrap Supreme"})
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 530


@pytest.mark.asyncio
async def test_a_scaled_estimate_is_named_after_the_food(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _web_once(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R4-4", {"description": "Taco Bell Crunchwrap"}, {"description": "yes, half"})
    assert second["status"] == "committed", second
    assert second["logged"]["name"] == "half a Taco Bell Crunchwrap"
    srv2, fake2 = _fresh(monkeypatch)
    _web_once(monkeypatch, srv2)
    _first, again = await _turns(
        srv2, "R4-4b", {"description": "half a Taco Bell Crunchwrap"}, {"description": "yes 2"})
    assert again["status"] == "committed", again
    assert again["logged"]["calories"] == 240
    assert again["logged"]["name"] == "2 Taco Bell Crunchwrap"


@pytest.mark.asyncio
async def test_ok_half_on_a_pick_question_logs_half_the_first_option(monkeypatch):
    srv, fake = _fresh(monkeypatch)
    _provider(monkeypatch, srv, _CRUNCHWRAP)
    _no_web(monkeypatch, srv)
    _first, second = await _turns(
        srv, "R4-5", {"description": "Taco Bell Crunchwrap"}, {"description": "ok half"})
    assert second["status"] == "committed", second
    assert second["logged"]["calories"] == 265


# Each log starts a fresh conversation (Matt, 2026-11-01) ------------------------

class _DayStore:
    """Chat history for the day plus the time of the latest food entry."""

    def __init__(self, history, logged_at):
        self.history = list(history)
        self.logged_at = logged_at

    async def fetch_chat_messages_since(self, *_):
        return list(self.history)

    async def latest_food_log_at(self, _since):
        return self.logged_at

    async def insert_user_chat_message(self, text, *, daily_cap):
        self.history.append({"role": "user", "content": text})

    async def insert_chat_message(self, role, text, *_):
        self.history.append({"role": role, "content": text})

    async def insert_coach_usage(self, *_):
        pass


async def _history_seen_by_coach(store, monkeypatch, enabled=None):
    from agent_canvas import CanvasService
    if enabled is not None:
        monkeypatch.setenv("COACH_FRESH_CONTEXT_AFTER_LOG", enabled)
    seen = {}

    async def agent(**kwargs):
        seen["history"] = kwargs["history"]
        return "ok", []

    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {}, agent=agent)
    token = bind_user(uuid4())
    try:
        await service.turn(str(uuid4()), str(uuid4()), "Taco Bell Crunchwrap supreme", adapter="text")
    finally:
        reset_user(token)
    return [message["content"] for message in seen["history"]]


def _crunchwrap_day():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 11, 1, 6, 19, tzinfo=timezone.utc)
    history = [
        {"role": "user", "content": "2 Taco Bell Crunchwrap sliders", "created_at": t0},
        {"role": "assistant", "content": "Did you mean ...?", "created_at": t0 + timedelta(seconds=5)},
        {"role": "user", "content": "Creamy Chipotle chicken Crunchwrap slider",
         "created_at": t0 + timedelta(seconds=30)},
        {"role": "assistant", "content": "Logged 2 Creamy Chipotle sliders.",
         "created_at": t0 + timedelta(seconds=40)},
    ]
    return history, t0 + timedelta(seconds=38)


@pytest.mark.asyncio
async def test_the_message_after_a_log_does_not_see_the_earlier_food_requests(monkeypatch):
    """Production 2026-11-01: after logging sliders, 'Taco Bell Crunchwrap
    supreme' became '2 Taco Bell Crunchwrap Supreme sliders' from the earlier
    messages. After a log the coach starts fresh, receipt included."""
    history, logged_at = _crunchwrap_day()
    seen = await _history_seen_by_coach(_DayStore(history, logged_at), monkeypatch)
    assert seen == []


@pytest.mark.asyncio
async def test_a_question_still_open_after_the_last_log_keeps_its_context(monkeypatch):
    from datetime import timedelta
    history, logged_at = _crunchwrap_day()
    later = logged_at + timedelta(minutes=5)
    history += [{"role": "user", "content": "One Choco Laka Smoothie", "created_at": later},
                {"role": "assistant", "content": "Did you mean Choc-A-Laka 12oz?",
                 "created_at": later + timedelta(seconds=4)}]
    seen = await _history_seen_by_coach(_DayStore(history, logged_at), monkeypatch)
    assert seen == ["One Choco Laka Smoothie", "Did you mean Choc-A-Laka 12oz?"]


@pytest.mark.asyncio
async def test_fresh_context_after_a_log_can_be_switched_off(monkeypatch):
    history, logged_at = _crunchwrap_day()
    seen = await _history_seen_by_coach(_DayStore(history, logged_at), monkeypatch, enabled="false")
    assert len(seen) == 4


_ADMIN_URL = __import__("os").environ.get("MMMACROS_TEST_PG_ADMIN_URL", "")


@pytest.mark.skipif(not _ADMIN_URL, reason="set MMMACROS_TEST_PG_ADMIN_URL to a throwaway local PostgreSQL")
@pytest.mark.asyncio
async def test_latest_food_log_time_is_only_ever_the_callers_own(monkeypatch):
    import asyncpg
    from datetime import datetime, timezone
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    monkeypatch.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import migrations
    from store import Store
    name = "mmm_fresh_" + uuid4().hex[:12]
    admin = await asyncpg.connect(_ADMIN_URL)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    base, _, query = _ADMIN_URL.partition("?")
    url = f"{base.rsplit('/', 1)[0]}/{name}" + (f"?{query}" if query else "")
    await migrations.apply_migrations(url)
    mine, other = uuid4(), uuid4()
    conn = await asyncpg.connect(url)
    for user in (mine, other):
        await conn.execute("INSERT INTO users(id, display_name) VALUES($1, 'Synthetic test user')", user)
    early = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    late = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
    await conn.execute("INSERT INTO nutrition_entries(id,user_id,name,day,created_at) "
                       "VALUES('a',$1,'eggs','2026-11-01',$2)", mine, early)
    await conn.execute("INSERT INTO nutrition_entries(id,user_id,name,day,created_at) "
                       "VALUES('b',$1,'eggs','2026-11-01',$2)", other, late)
    await conn.close()
    store = Store(url)

    async def connect(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
        return self.pool
    monkeypatch.setattr(Store, "connect", connect)
    since = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    try:
        token = bind_user(mine)
        try:
            assert await store.latest_food_log_at(since) == early
            assert await store.latest_food_log_at(late) is None
        finally:
            reset_user(token)
        token = bind_user(uuid4())
        try:
            assert await store.latest_food_log_at(since) is None
        finally:
            reset_user(token)
    finally:
        await store.aclose()
