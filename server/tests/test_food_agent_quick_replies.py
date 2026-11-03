"""First real use (2026-11-03): item changes must not block a meal, and every
question the agent asks comes with one-tap answers. Synthetic data only."""
import json
from uuid import uuid4

import pytest

from auth import bind_user, reset_user
from food_agent import confirm
from food_agent import contracts as c
from food_agent import portion as p
from food_agent.coordinator import FoodAgent, portion_choices
from food_agent.drafts import MemoryDrafts
from food_agent.modifications import adjustment_note, is_minor, mod_text
from food_agent.ports import FindResult
from test_food_agent_canvas import HALF_QP as CANVAS_HALF_QP, Rig
from test_food_agent_flow import Clock, HALF_QP, Script, _ident, _portion, add, revise


def _food(food_id, name, basis, **panel):
    return c.ResolvedFood.model_validate_json(json.dumps({
        "food_id": food_id, "provider": "synthetic", "external_id": food_id, "display_name": name,
        "macro_source": f"Synthetic: {name}",
        "serving_basis": p.parse_serving_text(basis).model_dump(mode="json"),
        "panel": {"protein": None, "calories": None, "carbs": None, "fat": None, "fiber": None, **panel},
    }))


CLUB = _food("cfa-club", "Chick-fil-A Grilled Chicken Club Sandwich", "1 sandwich",
             protein=37, calories=520, carbs=45, fat=22, fiber=3)
BURGER = _food("qp", "McDonald's Quarter Pounder", "1 sandwich", protein=26, calories=420, carbs=40, fat=20, fiber=2)
CHICKEN = _food("chicken", "Chicken breast, grilled", "100 g", protein=31, calories=165, carbs=0, fat=4, fiber=0)
FOODS = {"grilled chicken club sandwich": CLUB, "quarter pounder": BURGER, "chicken breast": CHICKEN}


class Finder:
    async def find(self, identity, portion):
        food = FOODS.get(identity.name.casefold())
        if food is None:
            return FindResult(status="no_match", food=None, note=None, question=f"Where was the {identity.name} from?")
        return FindResult(status="matched", food=food, note=None, question=None)


def rig():
    clock, script = Clock(), Script()
    drafts = MemoryDrafts(clock=clock)
    return FoodAgent(drafts, script, Finder(), None, clock), drafts, script


def club_without(*toppings):
    action = add((_ident("Grilled Chicken Club Sandwich", "Chick-fil-A", "restaurant"), _portion()))
    action["items"][0]["modifications"] = [{"op": "remove", "text": t} for t in toppings]
    return action


async def test_first_use_sandwich_logs_in_one_message_and_one_tap():
    agent, drafts, script = rig()
    script.push(club_without("tomato", "lettuce"))
    shown = await agent.turn("I had a chic fil a grilled club chicken sandwich but no tomato no lettuce",
                             session_ref="s")
    assert shown.preview is not None and not drafts.entries
    assert "(no tomato, no lettuce)" in shown.reply
    assert "Counted as the standard recipe." in shown.reply and "520 kcal" in shown.reply
    assert "source-backed" not in shown.reply and "variant" not in shown.reply
    assert shown.reply.endswith("Log it?") and shown.quick_replies == ("Log it", "Cancel")

    logged = await agent.turn("Log it", session_ref="s")
    assert logged.receipt is not None and logged.receipt.verified
    (entry,) = drafts.entries.values()
    assert entry["calories"] == 520 and "no tomato" in entry["name"]
    saved = entry["component_metadata"]["items"][0]["modifications"]
    assert [m["text"] for m in saved] == ["tomato", "lettuce"]
    assert logged.quick_replies == ()


async def test_a_change_that_matters_is_disclosed_never_hidden():
    agent, drafts, script = rig()
    action = add((_ident("Quarter Pounder", "McDonald's", "restaurant"), _portion()))
    action["items"][0]["modifications"] = [{"op": "extra", "text": "beef patty"}]
    script.push(action)
    shown = await agent.turn("quarter pounder with an extra patty", session_ref="s")
    assert shown.preview is not None
    assert "(extra beef patty)" in shown.reply
    assert "I can't adjust for extra beef patty yet" in shown.reply


async def test_cancel_button_cancels_the_preview():
    agent, drafts, script = rig()
    script.push(club_without("pickles"))
    await agent.turn("club no pickles", session_ref="s")
    result = await agent.turn("Cancel", session_ref="s")
    assert result.reply == "OK, not logged." and not drafts.entries


async def test_how_much_question_offers_amount_buttons_and_the_tap_finishes_it():
    agent, drafts, script = rig()
    script.push(add((_ident("Quarter Pounder", "McDonald's", "restaurant"),
                     _portion("mass", 6, "oz", text="6 oz"))))
    asked = await agent.turn("6 oz quarter pounder", session_ref="s")
    assert asked.preview is None and asked.question
    assert asked.quick_replies == ("Half", "1 serving", "2 servings", "Cancel")
    script.push(revise("i1", portion=_portion("serving_fraction", 0.5, text="half")))
    shown = await agent.turn("Half", session_ref="s")
    assert shown.preview is not None and shown.preview.totals.calories == 210
    assert shown.quick_replies == ("Log it", "Cancel")


def test_amount_buttons_follow_the_source_serving():
    assert portion_choices(p.parse_serving_text("100 g")) == ("4 oz", "6 oz", "8 oz")
    assert portion_choices(p.parse_serving_text("12 fl oz")) == ("8 fl oz", "12 fl oz", "16 fl oz")
    assert portion_choices(p.parse_serving_text("1 sandwich")) == ("Half", "1 serving", "2 servings")


def test_only_log_and_cancel_buttons_carry_yes_or_no_authority():
    assert confirm.classify("Log it") == "yes" and confirm.classify("Save it") == "yes"
    assert confirm.classify("Cancel") == "no"
    for label in ("Half", "1 serving", "2 servings", "4 oz", "8 fl oz", "Raw", "Cooked"):
        assert confirm.classify(label) == "other", label


def test_garnish_removals_are_minor_and_wording_is_plain():
    remove = lambda text: c.Modification(op="remove", text=text)
    assert is_minor(remove("tomato")) and is_minor(remove("lettuce and tomato"))
    assert is_minor(remove("no pickles"))
    assert not is_minor(remove("cheese")) and not is_minor(c.Modification(op="extra", text="beef patty"))
    assert mod_text(remove("no tomato")) == "no tomato"
    assert mod_text(c.Modification(op="extra", text="extra cheese")) == "extra cheese"


def test_adjustment_note_lists_only_what_could_move_the_numbers():
    item = lambda *mods: c.DraftItemState(item_ref="i1", identity=c.FoodIdentity(name="x", kind="generic"),
                                          portion=c.Portion(kind="unspecified", text=""), modifications=list(mods))
    assert adjustment_note([item()]) is None
    assert adjustment_note([item(c.Modification(op="remove", text="lettuce"))]) == "counted as the standard recipe"
    note = adjustment_note([item(c.Modification(op="remove", text="lettuce"),
                                 c.Modification(op="remove", text="cheese"))])
    assert note == "counted as the standard recipe. I can't adjust for no cheese yet"


@pytest.fixture
def user():
    scope = bind_user(uuid4())
    yield
    reset_user(scope)


async def test_canvas_turn_carries_quick_replies_only_when_there_is_something_to_tap(user):
    rig_ = Rig()
    rig_.script.push(CANVAS_HALF_QP)
    shown = await rig_.say("Half of a McDonald's quarter pounder")
    assert shown["quickReplies"] == ["Log it", "Cancel"]
    logged = await rig_.say("Log it")
    assert "quickReplies" not in logged and logged["reply"].startswith("Logged")
