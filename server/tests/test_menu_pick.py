"""The menu picker: the model infers which real menu record the user meant.

Offline. A fake picker stands in for the model in the retrieval tests; the MenuPicker
tests use a fake Responses endpoint. Provider names are FatSecret-style test data.
"""

from __future__ import annotations

import json

import httpx
import pytest

from food_agent.contracts import FoodIdentity, Portion
from food_agent2 import menu_pick
from food_agent2.retrieval import Retrieval

from test_food_agent2_flow import Rig, add, ident, portion
from test_food_agent2_retrieval import FakeSources, fatsecret

BRAND = "Domino's Pizza"
# No "small" or "personal" anywhere: only knowing the menu links "personal" to the 10 inch.
MENU = {"10": 'Hand Tossed Cheese Pizza (10")', "14": 'Hand Tossed Cheese Pizza (14")',
        "p10": 'Hand Tossed Pepperoni Pizza (10")', "tw": "Parmesan Bread Twists"}
NONE = Portion(kind="unspecified", text="")


def sources(phrases=("domino's personal pizza", "domino's pizza")):
    hits = [{"food_id": food_id, "food_name": name, "brand_name": BRAND} for food_id, name in MENU.items()]
    foods = {food_id: fatsecret(name, BRAND, food_id, serving="1 pizza", kcal=900.0) for food_id, name in MENU.items()}
    return FakeSources(hits={phrase: hits for phrase in phrases}, foods=foods)


class FakePicker:
    def __init__(self, *answers, fail=False):
        self.answers, self.fail = list(answers), fail
        self.calls: list[tuple[dict, list[dict]]] = []

    async def __call__(self, said, candidates):
        self.calls.append((dict(said), list(candidates)))
        if self.fail:
            raise RuntimeError("model down")
        return self.answers.pop(0) if self.answers else menu_pick.NO_PICK


def personal():
    return FoodIdentity(name="personal pizza", brand="Domino's", kind="restaurant")


async def find(identity, picker, src=None, amount=NONE):
    retrieval = Retrieval(src or sources(), picker=picker)
    return await retrieval.find(identity, amount, retrieval.scope())


async def test_the_model_infers_the_personal_pizza_is_the_10_inch_cheese():
    picker = FakePicker(menu_pick.Pick("10"))
    found = await find(personal(), picker)
    assert found.status == "verified" and found.food.display_name.endswith('Cheese Pizza (10")')
    said, offered = picker.calls[0]
    assert said["brand"] == "Domino's" and said["item"] == "personal pizza"
    assert {c["id"] for c in offered} == set(MENU), "every same-brand record is offered"


async def test_without_a_picker_the_same_words_find_nothing():
    assert (await find(personal(), None)).status == "needs_web"


async def test_an_id_that_was_not_offered_is_never_used():
    found = await find(personal(), FakePicker(menu_pick.Pick("999")))
    assert found.status == "needs_web"


async def test_the_model_can_search_in_the_chains_own_words_once_more():
    src = sources(phrases=("domino's hand tossed cheese pizza small",))
    picker = FakePicker(menu_pick.Pick(None, ("Domino's Hand Tossed Cheese Pizza Small",)), menu_pick.Pick("10"))
    found = await find(personal(), picker, src)
    assert found.status == "verified" and len(picker.calls) == 2
    assert ("hits", "domino's hand tossed cheese pizza small") in src.calls


async def test_a_clear_word_match_needs_no_model():
    picker = FakePicker(menu_pick.Pick("14"))
    found = await find(FoodIdentity(name="hand tossed cheese pizza 10 inch", brand="Domino's", kind="restaurant"),
                       picker, sources(phrases=("domino's hand tossed cheese pizza 10 inch",)))
    assert found.food.display_name.endswith('Cheese Pizza (10")') and picker.calls == []


async def test_a_stated_size_is_never_offered_in_another_size():
    picker = FakePicker()
    await find(FoodIdentity(name="14 inch cheese pizza", brand="Domino's", kind="restaurant"), picker,
               sources(phrases=("domino's 14 inch cheese pizza", "domino's cheese pizza", "domino's pizza")))
    if picker.calls:
        assert {c["id"] for c in picker.calls[0][1]} & {"10", "p10"} == set()


async def test_a_failed_model_falls_back_to_the_word_ranking():
    found = await find(FoodIdentity(name="cheese pizza", brand="Domino's", kind="restaurant"),
                       FakePicker(fail=True), sources(phrases=("domino's cheese pizza",)))
    assert found.status == "verified"


async def test_the_picker_can_be_switched_off(monkeypatch):
    monkeypatch.setenv(menu_pick.FLAG, "false")
    picker = FakePicker(menu_pick.Pick("10"))
    assert (await find(personal(), picker)).status == "needs_web" and picker.calls == []


async def test_the_whole_dialogue_logs_the_inferred_pizza():
    rig = Rig(sources())
    rig.agent.retrieval.picker = FakePicker(menu_pick.Pick("10"))
    rig.script.push(add((ident("personal pizza", "Domino's", "restaurant"), portion())))
    result = await rig.say("Dominos personal pizza")
    assert result.preview is not None and "Not counted" not in result.reply
    assert 'Hand Tossed Cheese Pizza (10")' in result.reply
    assert result.preview.lines[0].nutrients.calories == 900


# ---------------------------------------------------------------- the model call itself


def reply(text, status="completed"):
    body = {"status": status, "output": [{"type": "message", "role": "assistant", "status": "completed",
                                          "content": [{"type": "output_text", "text": text}]}]}
    return httpx.Response(200, json=body, request=httpx.Request("POST", "https://example.invalid"))


CANDIDATES = [{"id": "10", "name": 'Hand Tossed Cheese Pizza (10")', "brand": BRAND}]


@pytest.mark.parametrize("text,expected", [
    (json.dumps({"pick": "10", "search": []}), menu_pick.Pick("10")),
    (json.dumps({"pick": "77", "search": []}), menu_pick.Pick(None)),
    (json.dumps({"pick": None, "search": ["Domino's Small Cheese Pizza"]}),
     menu_pick.Pick(None, ("Domino's Small Cheese Pizza",))),
    ("not json", menu_pick.NO_PICK),
])
async def test_menu_picker_reads_only_offered_ids(text, expected):
    sent = []

    async def post(token, payload):
        sent.append(payload)
        return reply(text)

    picker = menu_pick.MenuPicker(post=post, token="t", model="m")
    assert await picker({"brand": "Domino's", "item": "personal pizza"}, CANDIDATES) == expected
    payload = sent[0]
    assert payload["store"] is False and payload["text"]["format"]["strict"] is True
    assert "personal pizza" in payload["input"][1]["content"] and 'Cheese Pizza (10\\")' in payload["input"][1]["content"]


async def test_menu_picker_without_a_key_or_on_an_error_picks_nothing():
    async def boom(token, payload):
        raise httpx.ConnectError("down")

    assert await menu_pick.MenuPicker(post=boom, token="")({"item": "x"}, CANDIDATES) == menu_pick.NO_PICK
    assert await menu_pick.MenuPicker(post=boom, token="t")({"item": "x"}, CANDIDATES) == menu_pick.NO_PICK
