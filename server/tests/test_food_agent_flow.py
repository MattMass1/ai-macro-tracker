"""WP3: food agent coordinator, confirmation binding, commit, readback, unknown outcome.

Offline only. A scripted interpreter returns typed ``AgentTurn`` objects, a fake
finder serves fixed panels, and ``MemoryDrafts`` is an in-memory double of the
migration-004 draft store with the same CAS, idempotency and failure points.

This lane proves the code does the right thing GIVEN a correct interpretation.
It does not prove the model interprets correctly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from food_agent import confirm
from food_agent import contracts as c
from food_agent import portion as p
from food_agent.coordinator import FoodAgent, FoodTurnResult
from food_agent.drafts import DraftCommitConflict, MemoryDrafts
from food_agent.ports import FindResult

# ---------------------------------------------------------------- fixtures


def _food(food_id: str, name: str, basis: str, **panel) -> c.ResolvedFood:
    return c.ResolvedFood.model_validate_json(json.dumps({
        "food_id": food_id, "provider": "fatsecret", "external_id": food_id, "display_name": name,
        "macro_source": f"FatSecret: {name}",
        "serving_basis": p.parse_serving_text(basis).model_dump(mode="json"),
        "panel": {"protein": None, "calories": None, "carbs": None, "fat": None, "fiber": None, **panel},
    }))


QP = _food("fs-qp", "McDonald's Quarter Pounder", "1 sandwich",
           protein=26, calories=420, carbs=40, fat=20, fiber=2)
QPC = _food("fs-qpc", "McDonald's Quarter Pounder with Cheese", "1 sandwich",
            protein=30, calories=520, carbs=42, fat=26, fiber=2)
SMOOTHIE = _food("fs-mango", "Smoothie King Mango Smoothie", "12 fl oz",
                 protein=4, calories=300, carbs=70, fat=1, fiber=3)
EGG = _food("fs-egg", "Egg, whole, large", "1 large egg", protein=6, calories=70, carbs=0, fat=5, fiber=0)
TOAST = _food("fs-toast", "Whole wheat toast", "1 slice", protein=4, calories=80, carbs=14, fat=1, fiber=2)
NO_CAL = _food("fs-mystery", "Mystery Bar", "1 bar", protein=10)
NO_FIBER = _food("fs-shake", "Core Power Elite Shake", "14 fl oz", protein=42, calories=230, carbs=8, fat=4)

CATALOG = {"quarter pounder": QP, "quarter pounder with cheese": QPC, "mango smoothie": SMOOTHIE,
           "eggs": EGG, "toast": TOAST, "mystery bar": NO_CAL, "core power elite": NO_FIBER}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 17, 30, tzinfo=timezone.utc)  # 12:30 New York

    def __call__(self) -> datetime:
        return self.now


class Script:
    """Scripted interpreter: returns the queued AgentTurn for each call."""

    def __init__(self):
        self.queue: list[c.AgentTurn] = []
        self.calls: list[tuple[str, dict]] = []

    def push(self, *actions: dict) -> None:
        self.queue.append(c.AgentTurn.model_validate_json(json.dumps(
            {"schema_version": c.SCHEMA_VERSION, "actions": list(actions)})))

    async def __call__(self, message, context):
        self.calls.append((message, dict(context)))
        if not self.queue:
            raise AssertionError(f"interpreter was not expected for {message!r}")
        return self.queue.pop(0)


class Finder:
    def __init__(self):
        self.calls: list[tuple[c.FoodIdentity, c.Portion]] = []

    async def find(self, identity, portion):
        self.calls.append((identity, portion))
        food = CATALOG.get(identity.name.casefold())
        if food is None:
            return FindResult(status="no_match", food=None, note=None,
                              question=f"Where was the {identity.name} from?")
        spoken = " ".join(x for x in (identity.brand, identity.name) if x)
        note = None if spoken.casefold() == food.display_name.casefold() else f"matched to {food.display_name}"
        return FindResult(status="matched", food=food, note=note, question=None)


def _ident(name, brand=None, kind="generic", variant=None):
    return {"name": name, "brand": brand, "variant": variant, "kind": kind, "preparation": None}


def _portion(kind="unspecified", amount=None, unit=None, text=""):
    return {"kind": kind, "amount": amount, "unit": unit, "text": text}


def add(*items):
    return {"type": "add_items", "items": [
        {"item_ref": None, "identity": ident, "portion": portion, "modifications": []}
        for ident, portion in items]}


def revise(item_ref, identity=None, portion=None, clear=()):
    return {"type": "revise_item", "item_ref": item_ref, "identity": identity,
            "clear_fields": list(clear), "portion": portion, "modifications": None}


HALF_QP = add((_ident("Quarter Pounder", "McDonald's", "restaurant"),
               _portion("serving_fraction", 0.5, text="half of a")))
SMOOTHIE_ADD = add((_ident("Mango Smoothie", "Smoothie King", "restaurant"), _portion()))


@pytest.fixture
def rig():
    clock = Clock()
    drafts = MemoryDrafts(clock=clock)
    script, finder = Script(), Finder()
    agent = FoodAgent(drafts, script, finder, None, clock)
    return agent, drafts, script, finder, clock


async def _say(agent, text) -> FoodTurnResult:
    return await agent.turn(text, session_ref="canvas-1", turn_ref=f"t-{abs(hash(text))}")


def _no_numbered_list(reply: str) -> None:
    import re
    assert not re.search(r"(^|\n)\s*\d+[.)]\s", reply), reply


# ---------------------------------------------------------------- confirm.py


@pytest.mark.parametrize("text", ["yes", "Yes!", "yep", "yeah", "ok", "OK.", "okay", "log it", "yes log it",
                                  "sure", "yes please", "Yep, log it", "go ahead", "looks good", "👍"])
def test_plain_yes(text):
    assert confirm.classify(text) == "yes"


@pytest.mark.parametrize("text", ["yes but 6 oz instead of 12", "yes 6 oz", "ok half", "yes with cheese",
                                  "yes and a coke", "log it as dinner", "yes no", "yeah 2", "", "maybe",
                                  "no eggs", "not yet", "yes but", "the eggs one"])
def test_not_a_plain_yes(text):
    assert confirm.classify(text) != "yes"


@pytest.mark.parametrize("text", ["no", "nope", "Cancel", "don't log it", "never mind", "nah"])
def test_plain_no(text):
    assert confirm.classify(text) == "no"


# ---------------------------------------------------------------- write path


async def test_first_food_message_writes_zero_meals_and_shows_a_preview(rig):
    agent, drafts, script, finder, _ = rig
    script.push(HALF_QP)
    result = await _say(agent, "Half of a McDonald's quarter pounder")
    assert drafts.entries == {} and drafts.commit_calls == []
    assert result.preview is not None and result.receipt is None
    assert result.preview.totals.calories == 210.0 and result.preview.totals.protein == 13.0
    assert result.outcome.effect == "not_attempted"
    reply = result.reply
    assert reply == ("McDonald's Quarter Pounder (half): 13 g protein, 210 kcal, 20 g carbs, 10 g fat, "
                     "1 g fiber. Log it?")
    assert reply.index("protein") < reply.index("kcal")  # protein leads
    assert reply.rstrip().endswith("?") and "log" in reply.casefold()
    assert "logged" not in reply.casefold()
    _no_numbered_list(reply)
    # Binding stored in pending_question, bound to the revision AFTER the write.
    (draft,) = drafts.drafts.values()
    pending = c.PendingConfirm.from_pending_question(draft["pending_question"])
    assert pending is not None and pending.revision == draft["revision"] == result.preview.revision


async def test_matched_to_is_said_when_the_source_name_differs(rig):
    agent, drafts, script, _, _ = rig
    script.push(add((_ident("Quarter Pounder", kind="restaurant"), _portion())))
    result = await _say(agent, "a quarter pounder")
    assert "Matched to McDonald's Quarter Pounder." in result.reply
    assert result.reply.endswith("Log it?") and result.preview.totals.calories == 420.0


async def test_not_food_is_handed_off_without_writes(rig):
    agent, drafts, script, _, _ = rig
    script.push({"type": "not_food"})
    result = await _say(agent, "how many rest days should I take")
    assert result.handed_off is True and result.preview is None
    assert drafts.drafts == {} and drafts.entries == {}


async def test_yes_but_6_oz_rescales_same_product_without_a_new_search_then_one_yes_commits(rig):
    agent, drafts, script, finder, _ = rig
    script.push(SMOOTHIE_ADD)
    first = await _say(agent, "a mango smoothie from smoothie king")
    assert first.preview.totals.calories == 300.0
    assert len(finder.calls) == 1

    script.push({"type": "confirm", "draft_ref": first.preview.draft_id, "shown_revision": first.preview.revision},)
    # The model also (wrongly) thinks this is a confirmation: no commit, no edit.
    second = await _say(agent, "yes but 6 oz instead of 12")
    assert drafts.commit_calls == [] and drafts.entries == {}
    assert "logged" not in second.reply.casefold()

    script.push(revise("i1", portion=_portion("mass", 6, "oz", "6 oz")))
    third = await _say(agent, "yes but 6 oz instead of 12")
    assert drafts.commit_calls == [] and drafts.entries == {}
    assert len(finder.calls) == 1  # portion-only revision: no new search
    assert third.preview.totals.calories == 150.0 and third.preview.revision > first.preview.revision
    (draft,) = drafts.drafts.values()
    item = c.DraftItemState.model_validate_json(json.dumps(draft["items"][0]["resolution"]["state"]))
    assert item.resolved.food_id == "fs-mango" and item.multiplier == 0.5
    _no_numbered_list(third.reply)

    calls_before = len(script.calls)
    done = await _say(agent, "yes")
    assert len(script.calls) == calls_before  # a plain yes needs no model call
    assert len(drafts.commit_calls) == 1 and len(drafts.entries) == 1
    entry = next(iter(drafts.entries.values()))
    assert entry["calories"] == 150.0 and entry["protein"] == 2.0
    meta = entry["component_metadata"]["items"][0]
    assert meta["food_id"] == "fs-mango" and meta["multiplier"] == 0.5
    assert meta["portion"] == {"kind": "mass", "amount": 6.0, "unit": "oz", "text": "6 oz"}
    assert done.receipt is not None and done.receipt.verified and done.receipt.nutrients.calories == 150.0
    assert done.outcome.effect == "applied_verified" and "logged" in done.reply.casefold()

    # A repeated yes never writes again.
    script.push({"type": "confirm", "draft_ref": first.preview.draft_id, "shown_revision": 1})
    again = await _say(agent, "yes")
    assert len(drafts.entries) == 1 and again.receipt is None


async def test_half_quarter_pounder_with_cheese_keeps_the_half_applied_once(rig):
    agent, drafts, script, finder, _ = rig
    script.push(HALF_QP)
    await _say(agent, "Half of a McDonald's quarter pounder")
    script.push(revise("i1", identity={"name": "Quarter Pounder with Cheese", "brand": None,
                                       "variant": None, "kind": None, "preparation": None}))
    second = await _say(agent, "quarter pounder with cheese")
    assert finder.calls[-1][0].name == "Quarter Pounder with Cheese"
    assert finder.calls[-1][1].kind == "serving_fraction" and finder.calls[-1][1].amount == 0.5
    assert second.preview.totals.calories == 260.0
    assert drafts.entries == {}
    done = await _say(agent, "yes")
    assert len(drafts.entries) == 1
    entry = next(iter(drafts.entries.values()))
    assert entry["calories"] == 260.0 and entry["protein"] == 15.0  # not 520, not 130
    assert "Quarter Pounder with Cheese" in entry["name"]
    meta = entry["component_metadata"]["items"][0]
    assert meta["portion"]["amount"] == 0.5 and meta["multiplier"] == 0.5 and meta["food_id"] == "fs-qpc"
    assert done.receipt.nutrients.calories == 260.0 and done.receipt.nutrients.protein == 15.0


async def test_model_confirm_without_a_plain_user_yes_never_commits(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    shown = await _say(agent, "half a quarter pounder")
    for text in ("sure thing buddy whatever", "yes but with cheese", "log it as dinner"):
        script.push({"type": "confirm", "draft_ref": shown.preview.draft_id,
                     "shown_revision": shown.preview.revision})
        result = await _say(agent, text)
        assert drafts.commit_calls == [] and drafts.entries == {}, text
        assert result.receipt is None


async def test_stale_yes_reshows_the_preview_and_does_not_commit(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    shown = await _say(agent, "half a quarter pounder")
    # Another device changes the draft after the preview was shown: the stored
    # binding still names the old revision.
    (draft_id,) = drafts.drafts
    drafts.drafts[draft_id]["revision"] += 1
    stale = await _say(agent, "yes")
    assert drafts.commit_calls == [] and drafts.entries == {}
    assert stale.preview is not None and stale.preview.revision > shown.preview.revision
    assert "logged" not in stale.reply.casefold()
    # The re-shown preview is now the bound one: a fresh yes commits it once.
    done = await _say(agent, "yes")
    assert len(drafts.entries) == 1 and done.receipt is not None


async def test_tampered_content_hash_does_not_commit(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    (draft_id,) = drafts.drafts
    pending = c.PendingConfirm.from_pending_question(drafts.drafts[draft_id]["pending_question"])
    drafts.drafts[draft_id]["pending_question"] = pending.model_copy(
        update={"content_hash": "0" * 64}).to_pending_question()
    result = await _say(agent, "yes")
    assert drafts.commit_calls == [] and drafts.entries == {} and result.preview is not None


async def test_two_open_drafts_and_yes_asks_which(rig):
    agent, drafts, script, _, clock = rig
    script.push(add((_ident("Eggs"), _portion("count", 2, text="2"))))
    await _say(agent, "2 eggs")
    clock.now += timedelta(minutes=45)  # a separate meal, not an addition
    script.push(add((_ident("Toast"), _portion())))
    await _say(agent, "toast")
    assert len(drafts.drafts) == 2
    # Showing the toast cleared the eggs binding (B1). Another device then shows the eggs again.
    eggs_id = next(i for i, row in drafts.drafts.items()
                   if row["items"][0]["resolution"]["state"]["identity"]["name"] == "Eggs")
    assert drafts.drafts[eggs_id]["pending_question"] is None
    _rebind(drafts, eggs_id)
    result = await _say(agent, "yes")
    assert drafts.commit_calls == [] and drafts.entries == {}
    assert result.reply.rstrip().endswith("?")
    assert "Egg" in result.reply and "toast" in result.reply.casefold()
    _no_numbered_list(result.reply)


async def test_unknown_calories_never_commits_and_asks_one_question(rig):
    agent, drafts, script, _, _ = rig
    script.push(add((_ident("Mystery Bar"), _portion())))
    result = await _say(agent, "a mystery bar")
    assert result.preview is None and result.reply.count("?") == 1
    assert "0 kcal" not in result.reply
    script.push({"type": "ask", "question": "Which brand was it?"})
    yes = await _say(agent, "yes")
    assert drafts.commit_calls == [] and drafts.entries == {} and yes.receipt is None


async def test_unknown_fiber_blocked_when_unknowns_cannot_be_stored(rig):
    agent, drafts, script, _, _ = rig
    drafts.unknown_writable = False
    script.push(add((_ident("Core Power Elite"), _portion())))
    result = await _say(agent, "a core power elite")
    assert "fiber" in result.reply and "0 g fiber" not in result.reply
    script.push({"type": "ask", "question": "Which brand was it?"})
    await _say(agent, "yes")
    assert drafts.commit_calls == [] and drafts.entries == {}


async def test_unknown_fiber_is_stored_and_shown_as_unknown_never_zero(rig):
    agent, drafts, script, _, _ = rig
    drafts.unknown_writable = True
    script.push(add((_ident("Core Power Elite"), _portion())))
    shown = await _say(agent, "a core power elite")
    assert shown.preview.totals.fiber is None and "fiber unknown" in shown.reply
    done = await _say(agent, "yes")
    entry = next(iter(drafts.entries.values()))
    assert entry["fiber"] is None and entry["component_metadata"]["unknown_nutrients"] == ["fiber"]
    assert done.receipt.nutrients.fiber is None and done.receipt.nutrients.protein == 42.0
    assert "0 g fiber" not in done.reply


async def test_plain_no_cancels_the_pending_preview(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    result = await _say(agent, "no")
    (draft,) = drafts.drafts.values()
    assert draft["state"] == "canceled" and drafts.entries == {}
    assert "logged" not in result.reply.casefold() or "not logged" in result.reply.casefold()


# ---------------------------------------------------------------- unknown outcome


async def test_lost_commit_response_is_unverified_then_reconciled_by_the_same_key(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    drafts.fail_after_commit = 1
    lost = await _say(agent, "yes")
    assert lost.outcome.status == "unknown" and lost.outcome.effect == "unknown"
    assert "could not verify" in lost.reply.casefold() and "logged" not in lost.reply.casefold()
    assert lost.receipt is None
    assert len(drafts.entries) == 1  # it did save; the response was lost
    # Next turn reconciles FIRST, by the same operation key, before anything else.
    keys_before = list(drafts.commit_calls)
    again = await _say(agent, "yes")
    assert len(drafts.entries) == 1
    assert all(call[0] == keys_before[0][0] for call in drafts.commit_calls)  # never a new key
    assert again.receipt is not None and again.receipt.verified and again.outcome.effect == "applied_verified"
    # Reconciled once: a later turn does not report it again.
    script.push({"type": "not_food"})
    later = await _say(agent, "thanks")
    assert later.handed_off and len(drafts.entries) == 1


async def test_commit_that_never_reached_the_db_is_retried_with_the_same_key_and_hash(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    drafts.fail_before_commit = 1
    lost = await _say(agent, "yes")
    assert lost.outcome.status == "unknown" and drafts.entries == {}
    script.push({"type": "not_food"})
    await _say(agent, "hello?")
    assert len(drafts.entries) == 1
    (first, second) = drafts.commit_calls
    assert first[:3] == second[:3]  # same key (draft, revision) and same request hash


async def test_failed_readback_is_unverified_then_reconciled(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    drafts.readback_fails = 1
    lost = await _say(agent, "yes")
    assert lost.outcome.effect == "unknown" and "logged" not in lost.reply.casefold()
    fixed = await _say(agent, "yes")
    assert len(drafts.entries) == 1 and fixed.receipt is not None and fixed.receipt.verified


# ---------------------------------------------------------------- partial repair


async def test_fixing_one_item_never_creates_a_second_meal(rig):
    agent, drafts, script, finder, _ = rig
    script.push(add((_ident("Eggs"), _portion("count", 2, text="2")), (_ident("Toast"), _portion())))
    await _say(agent, "2 eggs and toast")
    script.push(revise("i1", portion=_portion("count", 3, text="3")))
    fixed = await _say(agent, "actually 3 eggs")
    assert len(finder.calls) == 2 and fixed.preview.totals.calories == 290.0
    await _say(agent, "yes")
    assert len(drafts.entries) == 1
    (entry_id, entry), = drafts.entries.items()
    # After the commit: fixing one item amends the SAME entry, never a new meal.
    script.push(revise("i2", portion=_portion("count", 2, text="2 slices")))
    amend = await _say(agent, "oh and it was 2 slices of toast")
    assert amend.preview is not None and drafts.entries[entry_id]["calories"] == 290.0
    done = await _say(agent, "yes")
    assert list(drafts.entries) == [entry_id]
    assert drafts.entries[entry_id]["calories"] == 370.0 and drafts.entries[entry_id]["entry_revision"] == 2
    assert done.receipt.entry_revision == 2 and "updated" in done.reply.casefold()


async def test_amend_of_a_meal_changed_elsewhere_conflicts_once_and_is_not_retried(rig):
    agent, drafts, script, _, _ = rig
    script.push(add((_ident("Eggs"), _portion("count", 2, text="2"))))
    await _say(agent, "2 eggs")
    await _say(agent, "yes")
    (entry_id, entry), = drafts.entries.items()
    script.push(revise("i1", portion=_portion("count", 3, text="3")))
    await _say(agent, "make it 3")
    entry["entry_revision"] += 1  # edited on another device meanwhile
    result = await _say(agent, "yes")
    assert result.outcome.status == "conflict" and result.receipt is None
    assert "logged" not in result.reply.casefold() and drafts.entries[entry_id]["calories"] == 140.0
    calls = len(drafts.commit_calls)
    script.push({"type": "not_food"})
    later = await _say(agent, "ok whatever, how's my protein")
    assert later.handed_off and len(drafts.commit_calls) == calls  # the conflict is not retried


async def test_item_refs_are_unique_across_open_and_logged_meals(rig):
    agent, drafts, script, _, clock = rig
    script.push(add((_ident("Eggs"), _portion("count", 2, text="2"))))
    await _say(agent, "2 eggs")
    await _say(agent, "yes")
    script.push(add((_ident("Toast"), _portion())))
    await _say(agent, "toast")
    open_rows = [row for row in drafts.drafts.values() if row["state"] != "committed"]
    assert [i["resolution"]["state"]["item_ref"] for i in open_rows[0]["items"]] == ["i2"]


# ---------------------------------------------------------------- review round (B1, S1, S2, N1)

BIG_MAC_COMBO = _food("fs-bmc", "McDonald's Big Mac Combo", "1 combo",
                      protein=28, calories=1080, carbs=140, fat=42, fiber=8)


def _ask_combo(finder):
    plain = Finder.find

    async def find(identity, portion):
        finder.calls.append((identity, portion))
        if identity.name.casefold() == "big mac":
            return FindResult(status="needs_detail", food=None, note=None, question="Did you mean Big Mac Combo?")
        if identity.name.casefold() == "big mac combo":
            return FindResult(status="matched", food=BIG_MAC_COMBO, note=None, question=None)
        finder.calls.pop()
        return await plain(finder, identity, portion)
    finder.find = find


def _rebind(drafts, draft_id):
    """Another device showed this draft's preview: give it a valid binding again."""
    row = drafts.drafts[draft_id]
    items = [c.DraftItemState.model_validate_json(json.dumps(i["resolution"]["state"])) for i in row["items"]]
    row["pending_question"] = c.PendingConfirm(draft_id=draft_id, revision=row["revision"],
                                               content_hash=c.content_hash(items)).to_pending_question()


async def test_p2_yes_to_a_model_side_question_never_logs_the_earlier_preview(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    script.push({"type": "ask", "question": "Did you have any sauce with it?"})
    await _say(agent, "what about the sauce")
    script.push({"type": "confirm", "draft_ref": next(iter(drafts.drafts)), "shown_revision": 1})
    answer = await _say(agent, "yes")
    assert drafts.entries == {} and drafts.commit_calls == [] and answer.receipt is None
    assert len(script.calls) == 3  # the yes reached the model; it was not taken as a commit
    # The model's confirm only re-shows the preview; a yes to THAT logs it.
    assert answer.preview is not None and answer.reply.endswith("Log it?")
    done = await _say(agent, "yes")
    assert len(drafts.entries) == 1 and done.receipt is not None


async def test_r1_no_to_a_model_side_question_does_not_cancel_the_meal(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    script.push({"type": "ask", "question": "Did you have any sauce with it?"})
    await _say(agent, "what about the sauce")
    script.push({"type": "ask", "question": "OK. Anything else with it?"})
    await _say(agent, "no")
    assert [row["state"] for row in drafts.drafts.values()] != ["canceled"]
    assert drafts.entries == {}


async def test_p3_yes_to_another_drafts_question_never_logs_the_first_draft(rig):
    agent, drafts, script, finder, clock = rig
    _ask_combo(finder)
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    clock.now += timedelta(minutes=25)
    script.push(add((_ident("Big Mac", "McDonald's", "restaurant"), _portion())))
    asked = await _say(agent, "a big mac")
    assert asked.reply == "Did you mean Big Mac Combo?"
    big_mac_ref = asked.draft_id
    item_ref = drafts.drafts[big_mac_ref]["items"][0]["resolution"]["state"]["item_ref"]
    script.push(revise(item_ref, identity={"name": "Big Mac Combo", "brand": None, "variant": None,
                                           "kind": None, "preparation": None}))
    answered = await _say(agent, "yes")
    assert drafts.entries == {} and answered.preview is not None
    assert answered.preview.draft_id == big_mac_ref
    await _say(agent, "yes")
    assert [e["name"] for e in drafts.entries.values()] == ["McDonald's Big Mac Combo"]


async def test_s1_yes_answering_a_did_you_mean_question_reaches_the_model_and_resolves(rig):
    agent, drafts, script, finder, _ = rig
    _ask_combo(finder)
    script.push(add((_ident("Big Mac", "McDonald's", "restaurant"), _portion())))
    asked = await _say(agent, "a big mac")
    assert asked.question == "Did you mean Big Mac Combo?"
    script.push(revise("i1", identity={"name": "Big Mac Combo", "brand": None, "variant": None,
                                       "kind": None, "preparation": None}))
    answered = await _say(agent, "yes")
    assert len(script.calls) == 2 and script.calls[1][0] == "yes"
    assert "Did you mean Big Mac Combo?" in json.dumps(script.calls[1][1])
    assert answered.preview is not None and "I still need" not in answered.reply
    assert drafts.entries == {}


async def test_s2_removing_the_only_item_of_a_logged_meal_is_honest(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    await _say(agent, "yes")
    (entry_id, entry), = drafts.entries.items()
    saved = dict(entry)
    script.push({"type": "remove_item", "item_ref": "i1"})
    result = await _say(agent, "actually remove the burger")
    assert "Removed" not in result.reply and "didn't change" in result.reply
    assert result.outcome.effect == "not_applied"
    assert drafts.entries == {entry_id: saved}


async def test_n1_no_cancel_after_an_unverified_save_that_never_landed_logs_nothing(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    drafts.fail_before_commit = 1
    lost = await _say(agent, "yes")
    assert lost.outcome.effect == "unknown"
    no = await _say(agent, "no, cancel")
    assert drafts.entries == {} and "logged" not in no.reply.casefold().replace("not logged", "")
    (draft_id, row), = drafts.drafts.items()
    assert row["state"] == "canceled"
    # The original request arriving late cannot write either.
    first = drafts.commit_calls[0]
    with pytest.raises(DraftCommitConflict):
        await drafts.commit(*first)
    assert drafts.entries == {}


async def test_model_outage_hands_off_when_nothing_is_open_but_not_when_a_draft_is(rig):
    agent, drafts, script, _, _ = rig

    async def down(message, context):
        raise RuntimeError("model unavailable")
    agent.interpret = down
    workout = await _say(agent, "what's my workout today")
    assert workout.handed_off is True and workout.reply == "" and drafts.drafts == {}
    agent.interpret = script
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    agent.interpret = down
    with_draft = await _say(agent, "with extra pickles please")
    assert with_draft.handed_off is False and "couldn't read" in with_draft.reply
    assert drafts.entries == {}


class FlakyClearDrafts(MemoryDrafts):
    """save() fails when clearing a binding (review R-1 probe)."""

    fail_clear: int = 0

    async def save(self, draft, expected_revision):
        if self.fail_clear and draft.pending is None and draft.state == "collecting":
            self.fail_clear -= 1
            raise ConnectionError("simulated: DB write failed while clearing a binding")
        return await super().save(draft, expected_revision)


async def test_r1_failed_binding_clear_then_yes_never_logs_the_earlier_preview():
    clock = Clock()
    drafts, script = FlakyClearDrafts(clock=clock), Script()
    agent = FoodAgent(drafts, script, Finder(), None, clock)
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    drafts.fail_clear = 1
    script.push({"type": "ask", "question": "Did you have any sauce with it?"})
    asked = await _say(agent, "what about the sauce")
    assert asked.reply == "Did you have any sauce with it?"
    (row,) = drafts.drafts.values()
    assert (row["pending_question"] or "").startswith("{")  # the clear really failed
    script.push({"type": "ask", "question": "Which sauce?"})
    yes = await _say(agent, "yes")
    assert drafts.entries == {} and drafts.commit_calls == [] and yes.receipt is None


async def test_r1_exception_mid_turn_still_leaves_no_confirmable_stale_preview(rig, monkeypatch):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")

    async def boom(*_a, **_k):
        raise RuntimeError("unexpected bug while editing")
    monkeypatch.setattr(agent, "_edit", boom)
    script.push(add((_ident("Toast"), _portion())))
    with pytest.raises(RuntimeError):
        await _say(agent, "and a coke")
    (row,) = drafts.drafts.values()
    assert row["pending_question"] is None  # cleared in a finally
    monkeypatch.undo()
    script.push({"type": "ask", "question": "Anything else?"})
    yes = await _say(agent, "yes")
    assert drafts.entries == {} and yes.receipt is None


async def test_r1_marker_write_failure_fails_closed(rig):
    agent, drafts, script, _, _ = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    drafts.fail_marks = 1
    result = await _say(agent, "what about the sauce")  # interpreter is never reached
    assert result.outcome.status == "error" and "nothing changed" in result.reply.casefold()
    assert len(script.calls) == 1 and drafts.entries == {}


async def test_r2_bare_count_of_eggs_against_a_two_egg_serving_is_one_serving_without_a_question(rig, monkeypatch):
    agent, drafts, script, _, _ = rig
    two_eggs = _food("fs-2eggs", "Eggs, whole, large", "2 large eggs", protein=12, calories=140, carbs=1,
                     fat=10, fiber=0)
    monkeypatch.setitem(CATALOG, "eggs", two_eggs)
    script.push(add((_ident("Eggs"), _portion("count", 2, text="2"))))
    result = await _say(agent, "2 eggs")
    assert result.question is None and result.preview is not None
    assert result.preview.totals.calories == 140.0
    (row,) = drafts.drafts.values()
    assert row["items"][0]["resolution"]["state"]["multiplier"] == 1.0


class NoHistory:
    def __init__(self, status="not_found"):
        self.status = status

    async def recall(self, query, when_hint):
        from food_agent.ports import RecallResult
        return RecallResult(status=self.status, items=(), source_entry_id=None, note=None,
                            question=f"I couldn't find {query} in your log. Where was it from?")


@pytest.mark.parametrize("status, outcome", [("not_found", "no_match"), ("needs_detail", "needs_detail")])
async def test_recall_that_finds_nothing_asks_honestly_and_writes_nothing(rig, status, outcome):
    agent, drafts, script, _, _ = rig
    agent.recaller = NoHistory(status)
    script.push({"type": "recall_meal", "query": "ahi tuna bowl", "when_hint": "last time"})
    result = await _say(agent, "ahi tuna bowl again, the one I had last time")
    assert result.reply == "I couldn't find ahi tuna bowl in your log. Where was it from?"
    assert result.outcome.status == outcome and result.outcome.effect == "not_attempted"
    assert result.preview is None and drafts.drafts == {} and drafts.entries == {}
    # With a meal already waiting, a failed recall keeps its items; only its preview
    # binding is cleared, because the last thing shown is now the question (B1).
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    (draft_id, row), = drafts.drafts.items()
    items_before = json.dumps(row["items"][0]["resolution"], sort_keys=True)
    script.push({"type": "recall_meal", "query": "ahi tuna bowl", "when_hint": None})
    again = await _say(agent, "and the tuna bowl from last time")
    assert again.reply == "I couldn't find ahi tuna bowl in your log. Where was it from?"
    assert json.dumps(drafts.drafts[draft_id]["items"][0]["resolution"], sort_keys=True) == items_before
    assert drafts.drafts[draft_id]["pending_question"] is None and drafts.entries == {}
    script.push({"type": "ask", "question": "Where was the tuna bowl from?"})
    after = await _say(agent, "yes")  # a yes to the recall question never logs the burger
    assert drafts.entries == {} and after.receipt is None


# ---------------------------------------------------------------- model adapter


def _openai_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}],
                                     "model": "fake", "usage": {}},
                          request=httpx.Request("POST", "https://example.invalid"))


async def test_model_adapter_sends_strict_schema_and_parses_agent_turn(monkeypatch):
    from food_agent.model_adapter import ModelInterpreter
    sent = []

    async def post(token, payload):
        sent.append((token, payload))
        return _openai_response(json.dumps({"schema_version": "food-agent/1", "actions": [{"type": "not_food"}]}))

    interpret = ModelInterpreter(post=post, token="test-token", model="fake-model")
    turn = await interpret("hi", {"open_draft": None})
    assert turn.actions[0].type == "not_food"
    token, payload = sent[0]
    fmt = payload["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == c.agent_turn_json_schema()
    assert payload["model"] == "fake-model" and "tools" not in payload


async def test_model_confirmed_true_is_rejected_and_cannot_commit(rig):
    from food_agent.model_adapter import InterpreterError, ModelInterpreter
    bad = json.dumps({"schema_version": "food-agent/1", "actions": [
        {"type": "confirm", "draft_ref": "x", "shown_revision": 1, "confirmed": True}]})
    with pytest.raises(ValidationError):
        c.AgentTurn.model_validate_json(bad)

    async def post(token, payload):
        return _openai_response(bad)

    interpret = ModelInterpreter(post=post, token="t", model="m")
    with pytest.raises(InterpreterError):
        await interpret("log it", {})

    agent, drafts, script, finder, clock = rig
    script.push(HALF_QP)
    await _say(agent, "half a quarter pounder")
    agent.interpret = interpret
    result = await _say(agent, "go on then, confirmed")
    assert drafts.entries == {} and result.outcome.effect != "applied_verified"
