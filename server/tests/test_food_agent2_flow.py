"""food_agent2 whole-dialogue replays: real coordinator, in-memory drafts, fake model and providers.

Each failure class from docs/agent/CATCHUP-2026-11-05.md section 6 has a dialogue here.
This proves the code does the right thing GIVEN a correct interpretation. It does
not prove the model interprets correctly: that is the live bench.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from food_agent.drafts import MemoryDrafts
from food_agent2 import contracts as c
from food_agent2.retrieval import Retrieval
from food_agent2.turn import FoodAgent2

from test_food_agent2_retrieval import (FakeSources, curated_egg, fatsecret, usda_row, web_estimate)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 17, 30, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Script:
    def __init__(self):
        self.queue: list = []
        self.calls: list = []

    def push(self, *actions):
        self.queue.append(c.Turn.model_validate_json(json.dumps(
            {"schema_version": c.SCHEMA_VERSION, "actions": list(actions)})))

    async def __call__(self, message, context):
        self.calls.append((message, dict(context)))
        if not self.queue:
            raise AssertionError(f"model was not expected for {message!r}")
        return self.queue.pop(0)


def ident(name, brand=None, kind="generic", preparation=None, variant=None):
    return {"name": name, "brand": brand, "variant": variant, "kind": kind, "preparation": preparation}


def portion(kind="unspecified", amount=None, unit=None, text=""):
    return {"kind": kind, "amount": amount, "unit": unit, "text": text}


def add(*items):
    return {"type": "add_items", "items": [
        {"item_ref": None, "identity": i, "portion": p, "modifications": [], "supplied": s}
        for i, p, s in (x if len(x) == 3 else (*x, None) for x in items)]}


def revise(ref, identity=None, new_portion=None, supplied=None):
    return {"type": "revise_item", "item_ref": ref, "identity": identity, "clear_fields": [],
            "portion": new_portion, "modifications": None, "supplied": supplied}


def supplied(**values):
    return {key: values.get(key) for key in ("calories", "protein", "carbs", "fat", "fiber")}


SETTLE = {"type": "settle", "use_web": False}
USE_WEB = {"type": "settle", "use_web": True}
NOT_FOOD = {"type": "not_food"}
CANCEL = {"type": "cancel"}
THREE = portion("count", 3, text="3")
ONE = portion("count", 1, text="1")


class Rig:
    def __init__(self, sources=None, unknown_writable=False, recall=None):
        self.clock, self.script = Clock(), Script()
        self.drafts = MemoryDrafts(clock=self.clock, unknown_writable=unknown_writable)
        self.sources = sources or FakeSources()
        self.agent = FoodAgent2(self.drafts, self.script, Retrieval(self.sources), recall, self.clock)

    async def say(self, text, session="canvas:1"):
        return await self.agent.turn(text, session_ref=session, turn_ref="t")

    @property
    def entries(self):
        return list(self.drafts.entries.values())


def club_sources():
    hits = {"chick-fil-a grilled chicken club sandwich": [
        {"food_id": "11", "food_name": "Grilled Chicken Club w/ American", "brand_name": "Chick-fil-A"}]}
    return FakeSources(hits=hits, foods={"11": fatsecret("Grilled Chicken Club w/ American", "Chick-fil-A", "11",
                                                        serving="1 sandwich", kcal=520.0)})


CLUB = (ident("Grilled Chicken Club Sandwich", "Chick-fil-A", "restaurant"), ONE)


# ---------------------------------------------------------------- contract


def test_model_cannot_ask_or_send_a_confirmed_flag():
    for bad in ({"type": "ask", "question": "Light or regular?"},
                {"type": "cancel", "confirmed": True}):
        with pytest.raises(ValidationError):
            c.Turn.model_validate_json(json.dumps({"schema_version": c.SCHEMA_VERSION, "actions": [bad]}))


def test_not_food_stands_alone():
    with pytest.raises(ValidationError):
        c.Turn.model_validate_json(json.dumps({"schema_version": c.SCHEMA_VERSION,
                                               "actions": [NOT_FOOD, CANCEL]}))


def test_schema_is_openai_strict():
    schema = c.turn_json_schema()

    def walk(node):
        if isinstance(node, dict):
            if "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(schema)
    assert "anyOf" not in schema


# ---------------------------------------------------------------- failure class 1: dead-end loop


async def test_named_restaurant_item_previews_then_logs_on_yes():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    preview = await rig.say("Chic fil a Grilled chicken club sandwich")
    assert preview.preview is not None and "?" == preview.reply[-1]
    assert "matched to" in preview.reply.casefold()
    assert "where was it from" not in preview.reply.casefold()
    done = await rig.say("yes")
    assert done.receipt.verified and len(rig.entries) == 1
    assert done.reply.startswith("Logged ")


async def test_no_message_ever_repeats_a_question():
    rig = Rig()
    rig.script.push(add((ident("Mystery Stew", kind="other"), portion())))
    first = await rig.say("mystery stew")
    assert first.question is None and "not counted" in first.reply.casefold()
    rig.script.push(NOT_FOOD)
    second = await rig.say("what??")
    assert first.reply != second.reply or second.question is None


async def test_use_the_internet_forces_the_web_lane():
    sources = FakeSources(hits={}, web={"chick-fil-a grilled chicken club": web_estimate("club", 500.0)})
    rig = Rig(sources)
    rig.script.push(add((ident("Grilled Chicken Club Sandwich", "Chick-fil-A", "restaurant"), ONE)))
    first = await rig.say("club sandwich")
    assert first.preview is not None  # the ladder already reached the web
    assert "est." in first.reply


# ---------------------------------------------------------------- failure class 2: near match


async def test_near_match_is_disclosed_and_logged():
    hits = {"barebells marshmallow peanut road": [
        {"food_id": "5", "food_name": "Marshmallow Rocky Road Protein Bar", "brand_name": "Barebells"}]}
    sources = FakeSources(hits=hits, foods={"5": fatsecret("Marshmallow Rocky Road Protein Bar", "Barebells", "5",
                                                           serving="1 bar")})
    rig = Rig(sources)
    rig.script.push(add((ident("Marshmallow Peanut Road", "Barebells", "packaged"), portion())))
    result = await rig.say("Barebells marshmallow peanut road")
    assert "matched to Barebells Marshmallow Rocky Road Protein Bar" in result.reply.replace("Matched", "matched")
    assert result.preview.lines[0].nutrients.calories == 300


# ---------------------------------------------------------------- failure class 3: plain foods


async def test_three_sunny_side_up_eggs_log():
    rig = Rig(FakeSources(exact={"eggs": curated_egg()}))
    rig.script.push(add((ident("eggs", preparation="sunny side up"), THREE)))
    result = await rig.say("3 sunny side up eggs")
    assert result.preview is not None
    assert result.preview.lines[0].nutrients.calories == pytest.approx(207, abs=1)
    await rig.say("yes")
    assert rig.entries[0]["calories"] == pytest.approx(207, abs=1)


# ---------------------------------------------------------------- failure class 5: user numbers


async def test_user_calories_win_over_the_lookup_and_are_labeled():
    rows = [usda_row("Pizza, cheese, thin crust", fdc=9, kcal=270.0, measures=[("1 slice", 107)])]
    rig = Rig(FakeSources(rows={"pizza": rows}))
    rig.script.push(add((ident("New York style pizza"), portion("count", 1.5, text="1.5 slices"))))
    await rig.say("1.5 slices of New York style piece")
    rig.script.push(revise("i1", supplied=supplied(calories=325)))
    second = await rig.say("I'd say 325")
    assert second.preview.lines[0].nutrients.calories == 325
    assert "your" in second.reply.casefold()
    rig.script.push(revise("i1", new_portion=portion("count", 1, text="1 slice")))
    third = await rig.say("Do 1 slice")
    assert third.preview.lines[0].nutrients.calories == 325
    await rig.say("Yes")
    assert rig.entries[0]["calories"] == 325


async def test_a_number_the_user_never_typed_is_ignored():
    rig = Rig(FakeSources(exact={"eggs": curated_egg()}))
    rig.script.push(add((ident("eggs"), THREE, supplied(calories=999))))
    result = await rig.say("3 eggs")
    assert result.preview.lines[0].nutrients.calories != 999


async def test_pasted_label_logs_both_lines_without_a_question():
    panel = "Cal\tFat\tCarbs\tProtein\nGrilled Chicken Club\t520\t22g\t44g\t37g\nHoney Roasted BBQ (1 packet)\t60\t5g\t3g\t0g"
    rig = Rig(unknown_writable=True)
    rig.script.push(add(
        (ident("Grilled Chicken Club", kind="restaurant"), ONE, supplied(calories=520, fat=22, carbs=44, protein=37)),
        (ident("Honey Roasted BBQ", kind="restaurant"), portion("count", 1, text="1 packet"),
         supplied(calories=60, fat=5, carbs=3, protein=0))))
    result = await rig.say(panel)
    assert result.question is None
    assert [line.nutrients.calories for line in result.preview.lines] == [520, 60]
    await rig.say("yes")
    assert rig.entries[0]["calories"] == 580
    assert rig.entries[0]["fiber"] is None  # unknown stays unknown, never 0


async def test_partial_label_fills_gaps_from_the_source_when_nulls_cannot_be_saved():
    rows = [usda_row("Pizza, cheese, thin crust", fdc=9, kcal=270.0, measures=[("1 slice", 107)])]
    rig = Rig(FakeSources(rows={"pizza": rows}), unknown_writable=False)
    rig.script.push(add((ident("pizza"), ONE, supplied(calories=540))))
    result = await rig.say("1 slice of pizza, 540 calories")
    line = result.preview.lines[0].nutrients
    assert line.calories == 540 and line.protein == pytest.approx(20)  # the row, scaled to the user's calories
    assert "scaled" in result.reply


# ---------------------------------------------------------------- failure class 6: re-asking


async def test_raw_or_cooked_is_asked_once_for_a_weighed_food_then_never_again():
    tuna = {"name": "yellowfin (ahi) tuna", "source": "Generic: yellowfin (ahi) tuna, per 100 g, raw, fresh",
            "serving_size": "Generic: tuna - 100 g", "macros_per_100g": {"calories": 109, "protein": 24, "carbs": 0,
                                                                         "fat": 0.5, "fiber": 0},
            "macros_per_serving": {"calories": 109, "protein": 24, "carbs": 0, "fat": 0.5, "fiber": 0},
            "attribution": {"cache_allowed": True}}
    rig = Rig(FakeSources(exact={"ahi tuna": tuna}))
    rig.script.push(add((ident("ahi tuna"), portion("mass", 3.5, "oz", "3.5 oz"))))
    first = await rig.say("ahi tuna 3.5 oz")
    assert first.question and "raw or cooked" in first.question
    assert first.quick_replies[:2] == ("Raw", "Cooked")
    rig.script.push(NOT_FOOD)
    second = await rig.say("lol whatever")
    assert second.question is None
    assert "raw or cooked" not in second.reply
    assert "not counted" in second.reply.casefold()


async def test_answering_raw_resolves_the_item():
    tuna = {"name": "yellowfin (ahi) tuna", "source": "Generic: yellowfin (ahi) tuna, per 100 g, raw, fresh",
            "serving_size": "Generic: tuna - 100 g",
            "macros_per_100g": {"calories": 109, "protein": 24, "carbs": 0, "fat": 0.5, "fiber": 0},
            "macros_per_serving": {"calories": 109, "protein": 24, "carbs": 0, "fat": 0.5, "fiber": 0},
            "attribution": {"cache_allowed": True}}
    rig = Rig(FakeSources(exact={"ahi tuna": tuna, "raw ahi tuna": tuna}))
    rig.script.push(add((ident("ahi tuna"), portion("mass", 3.5, "oz", "3.5 oz"))))
    await rig.say("ahi tuna 3.5 oz")
    rig.script.push(revise("i1", identity={"name": None, "brand": None, "variant": None, "kind": None,
                                           "preparation": "raw"}))
    result = await rig.say("Raw")
    assert result.preview.lines[0].nutrients.calories == pytest.approx(108.1, abs=0.5)


async def test_portion_change_keeps_the_food_with_no_new_lookup():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    before = len(rig.sources.calls)
    rig.script.push(revise("i1", new_portion=portion("serving_fraction", 0.5, text="half")))
    result = await rig.say("half of it")
    assert result.preview.lines[0].nutrients.calories == 260
    assert [call for call in rig.sources.calls[before:] if call[0] != "food"] == []


async def test_identical_food_repeated_before_confirming_is_not_a_second_item():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.script.push(add(CLUB))
    result = await rig.say("club")
    assert len(result.preview.lines) == 1


# ---------------------------------------------------------------- failure class 8: mid-draft handoff


async def test_short_reply_mid_draft_never_leaves_for_the_old_path():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.script.push(NOT_FOOD)
    result = await rig.say("Do 1 slice")
    assert result.handed_off is False


async def test_genuinely_unrelated_message_hands_off_when_nothing_is_open():
    rig = Rig()
    rig.script.push(NOT_FOOD)
    assert (await rig.say("how should I train tomorrow?")).handed_off is True


# ---------------------------------------------------------------- partial logging, rule 7 and 8


async def test_unknown_food_never_blocks_the_ready_food():
    rig = Rig(FakeSources(exact={"eggs": curated_egg()}))
    rig.script.push(add((ident("eggs"), THREE), (ident("mystery stew", kind="other"), portion())))
    result = await rig.say("3 eggs and mystery stew")
    assert result.preview is not None and len(result.preview.lines) == 1
    assert "Not counted: mystery stew" in result.reply
    assert "tell me what it was" in result.reply.casefold()
    done = await rig.say("yes")
    assert "not counted: mystery stew" in done.reply.casefold()
    assert len(rig.entries) == 1 and "mystery" not in rig.entries[0]["name"].casefold()


async def test_slow_web_never_blocks_food_that_is_ready():
    rig = Rig(FakeSources(exact={"eggs": curated_egg()}, web={"mystery stew": web_estimate("stew")}, web_delay=5.0))
    rig.agent.web_soft_seconds = 0.01
    rig.script.push(add((ident("eggs"), THREE), (ident("mystery stew", kind="other"), portion())))
    result = await rig.say("3 eggs and mystery stew")
    assert result.preview is not None and len(result.preview.lines) == 1
    assert "mystery stew" in result.reply


async def test_single_slow_food_waits_for_the_web():
    rig = Rig(FakeSources(web={"mystery stew": web_estimate("stew")}, web_delay=0.02))
    rig.agent.web_soft_seconds = 0.001
    rig.script.push(add((ident("mystery stew", kind="other"), portion())))
    assert (await rig.say("mystery stew")).preview is not None


async def test_settle_assumes_one_serving_and_says_so():
    rows = [usda_row("Rice, sticky", fdc=3, fiber=0.5)]
    rig = Rig(FakeSources(rows={"sticky rice": rows}))
    rig.script.push(add((ident("sticky rice"), portion())))
    first = await rig.say("sticky rice")
    assert first.question  # no amount and a per-100 g source
    rig.script.push(SETTLE)
    second = await rig.say("just estimate it")
    assert second.preview is not None and "assumed" in second.reply.casefold()


async def test_remove_only_drops_what_the_user_removed():
    rig = Rig(FakeSources(exact={"eggs": curated_egg()}))
    rig.script.push(add((ident("eggs"), THREE), (ident("mystery stew", kind="other"), portion())))
    await rig.say("3 eggs and mystery stew")
    rig.script.push({"type": "remove_item", "item_ref": "i2"})
    result = await rig.say("forget the stew")
    assert "stew" not in result.reply.casefold()
    assert len(result.preview.lines) == 1


# ---------------------------------------------------------------- confirmation authority


async def test_model_saying_confirm_never_commits():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.script.push({"type": "confirm"})
    result = await rig.say("that looks about right to me")
    assert result.receipt is None and not rig.entries
    assert result.preview is not None


async def test_yes_after_an_edit_confirms_the_new_preview_only():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.script.push(revise("i1", new_portion=portion("serving_fraction", 0.5, text="half")))
    await rig.say("Yes but half")
    assert not rig.entries
    await rig.say("yes")
    assert rig.entries[0]["calories"] == 260


async def test_a_preview_shown_in_another_session_is_reshown_not_committed():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club", session="canvas:phone")
    result = await rig.say("yes", session="canvas:ipad")
    assert not rig.entries and result.preview is not None


async def test_a_message_after_the_preview_retires_it():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.script.push(NOT_FOOD)
    await rig.say("how much protein is left today, and should I eat more before lifting?")
    rig.script.push({"type": "confirm"})
    result = await rig.say("yes")
    assert not rig.entries
    assert result.preview is not None


async def test_cancel_with_no():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    assert (await rig.say("no")).reply == "OK, not logged."
    assert not rig.entries


async def test_provider_panel_is_not_persisted_and_is_refetched_by_id_at_commit():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    stored = next(iter(rig.drafts.drafts.values()))["items"][0]["resolution"]["state"]["resolved"]["panel"]
    assert stored == {}
    await rig.say("yes")
    assert rig.entries[0]["calories"] == 520
    assert ("food", "11") in rig.sources.calls[-1:]


async def test_provider_changing_its_numbers_shows_a_new_preview_instead_of_committing():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.sources.foods["11"] = fatsecret("Grilled Chicken Club w/ American", "Chick-fil-A", "11",
                                        serving="1 sandwich", kcal=600.0)
    result = await rig.say("yes")
    assert not rig.entries and result.preview.lines[0].nutrients.calories == 600


# ---------------------------------------------------------------- amendments, unknown outcomes


async def test_amend_keeps_every_food_the_entry_already_counts():
    sources = club_sources()
    sources.exact_rows["eggs"] = curated_egg()
    rig = Rig(sources)
    rig.script.push(add(CLUB, (ident("eggs"), THREE)))
    await rig.say("club and 3 eggs")
    await rig.say("yes")
    rig.script.push(revise("i2", new_portion=portion("count", 2, text="2")))
    preview = await rig.say("actually 2 eggs")
    assert len(preview.preview.lines) == 2 and preview.reply.endswith("Save the change?")
    done = await rig.say("yes")
    assert done.reply.startswith("Updated ")
    assert len(rig.entries) == 1 and rig.entries[0]["calories"] == pytest.approx(520 + 138, abs=1)


async def test_lost_commit_response_reconciles_without_a_second_row():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.drafts.fail_after_commit = 1
    result = await rig.say("yes")
    assert result.outcome.effect == "unknown" and "could not verify" in result.reply
    again = await rig.say("yes")
    assert again.receipt.verified and len(rig.entries) == 1
    assert len(rig.drafts.commit_calls) == 1


async def test_a_reconciled_receipt_never_calls_saved_food_not_counted():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.drafts.fail_after_commit = 1
    await rig.say("yes")
    recovered = await rig.say("yes")
    assert "not counted" not in recovered.reply.casefold()


async def test_a_user_number_with_filled_gaps_survives_the_commit_turn():
    hits = {"chick-fil-a grilled chicken club sandwich": [
        {"food_id": "11", "food_name": "Grilled Chicken Club w/ American", "brand_name": "Chick-fil-A"}]}
    rig = Rig(FakeSources(hits=hits, foods={"11": fatsecret("Grilled Chicken Club w/ American", "Chick-fil-A", "11",
                                                            serving="1 sandwich", kcal=520.0)}))
    rig.script.push(add((*CLUB, supplied(calories=650))))
    preview = await rig.say("club, 650 calories")
    assert preview.preview.lines[0].nutrients.calories == 650
    done = await rig.say("yes")
    assert done.receipt.verified and rig.entries[0]["calories"] == 650
    assert rig.entries[0]["protein"] == 25  # 20 g from the source, scaled by 650 / 520


async def test_settle_with_nothing_open_says_so():
    rig = Rig()
    rig.script.push(SETTLE)
    assert (await rig.say("just log it")).reply == "There's nothing waiting to log right now."


async def test_failed_readback_is_unverified_and_never_resent():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.drafts.readback_fails = 1
    result = await rig.say("yes")
    assert result.receipt is None and result.outcome.effect == "unknown"
    follow = await rig.say("yes")
    assert follow.receipt.verified and len(rig.drafts.commit_calls) == 1


async def test_receipt_numbers_come_from_the_saved_row():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    done = await rig.say("yes")
    kcal = float(re.search(r"([\d.]+) kcal", done.reply)[1])
    assert kcal == rig.entries[0]["calories"]


async def test_store_outage_claims_nothing():
    rig = Rig()

    async def down(_since):
        raise ConnectionError("synthetic")
    rig.drafts.recent = down
    result = await rig.say("3 eggs")
    assert result.outcome.effect == "unknown" and not rig.entries


async def test_a_model_outage_shows_the_meal_again_and_yes_still_logs_it():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")

    async def broken(message, context):
        raise RuntimeError("synthetic")
    rig.agent.interpret = broken
    first = await rig.say("something odd")
    second = await rig.say("something odd again")
    assert first.handed_off is False and second.preview is not None
    assert "couldn't read" not in second.reply
    assert (await rig.say("yes")).receipt.verified and len(rig.entries) == 1


async def test_new_meal_after_a_break_starts_a_new_draft():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.clock.now += timedelta(minutes=25)
    rig.script.push(add((ident("mystery stew", kind="other"), portion())))
    await rig.say("mystery stew")
    assert len(rig.drafts.drafts) == 2


async def test_model_sees_only_short_fixed_context():
    rig = Rig(club_sources())
    rig.script.push(add(CLUB))
    await rig.say("club")
    rig.script.push(NOT_FOOD)
    await rig.say("hm")
    context = rig.script.calls[-1][1]
    assert set(context) == {"now", "last_question", "open_drafts", "last_logged"}
    assert context["open_drafts"][0]["items"][0]["item_ref"] == "i1"


# ---------------------------------------------------------------- live bench 2026-11-07


async def test_food_nothing_has_ends_with_a_question():
    rig = Rig()
    rig.script.push(add((ident("ahi tuna bowl"), ONE)))
    result = await rig.say("I had an ahi tuna bowl")
    assert result.reply.startswith("Not counted: ahi tuna bowl.") and result.reply.endswith("?")
    assert not rig.entries


async def test_amount_question_never_says_one_serving_is_one_serving():
    sources = FakeSources(hits={"smoothie king choc-a-laka": [
        {"food_id": "5", "food_name": "Choc-A-Laka", "brand_name": "Smoothie King"}]},
        foods={"5": fatsecret("Choc-A-Laka", "Smoothie King", "5")})
    rig = Rig(sources)
    rig.script.push(add((ident("Choc-A-Laka", "Smoothie King", "restaurant"),
                         portion("volume", 6, "fl_oz", "6 oz"))))
    result = await rig.say("6 oz Choc-A-Laka from Smoothie King")
    assert result.reply.startswith("How much ") and "One serving is 1 serving" not in result.reply


async def test_changing_a_saved_food_to_one_nothing_has_names_it_and_keeps_the_meal():
    sources = club_sources()
    sources.exact_rows["eggs"] = curated_egg()
    rig = Rig(sources)
    rig.script.push(add(CLUB, (ident("eggs"), THREE)))
    await rig.say("club and 3 eggs")
    await rig.say("yes")
    saved = dict(rig.entries[0])
    rig.script.push(revise("i2", identity=ident("ostrich eggs")))
    result = await rig.say("they were ostrich eggs")
    assert result.reply.casefold() == "i couldn't find ostrich eggs, so i left the meal as it was."
    assert rig.entries == [saved]
