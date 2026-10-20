"""Meal flow v2: partial logging, generic-core estimates, ask-once and lanes.

No database and no network: the real MealDrafts engine runs on an in-memory
draft store with the real server validator, and every food resolves from a
fixed fake resolver. Replays the reported dialogue:
"Ahi Tuna 3.5 oz, Sticky Rice with Gyoza sauce, Gochujang sauces, Fried
onions 1 serving" -> "Raw" -> "Pick any brand" -> "JUST LOG THE TUNA".
"""
from __future__ import annotations

import functools
import json
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

import httpx
import pytest

import domain
from auth import bind_user, reset_user
from meal_drafts import MEAL_DRAFT_INSTRUCTIONS, MEAL_DRAFT_TOOL, MealDrafts

DINNER = datetime(2026, 10, 19, 19, 40, tzinfo=domain.LOCAL_TZ)

TUNA_RAW = {"name": "yellowfin tuna, raw", "macro_source": "USDA FDC 175159: Fish, tuna, fresh, yellowfin, raw",
            "calories": 108.0, "protein": 24.0, "carbs": 0.0, "fat": 0.5, "fiber": 0.0, "basis": "raw"}
TUNA_COOKED = {"name": "yellowfin tuna, cooked", "macro_source": "USDA FDC 172006: Fish, tuna, yellowfin, cooked",
               "calories": 129.0, "protein": 29.0, "carbs": 0.0, "fat": 0.6, "fiber": 0.0, "basis": "cooked"}
RICE = {"name": "glutinous rice, cooked", "macro_source": "Generic: glutinous rice, per 100 g, cooked; source: USDA FDC 169714",
        "calories": 169.0, "protein": 3.5, "carbs": 36.7, "fat": 0.3, "fiber": 1.7, "basis": "cooked"}
BANANA = {"name": "banana", "macro_source": "Generic: banana, per 100 g, raw; source: USDA FDC 173944",
          "calories": 105.0, "protein": 1.3, "carbs": 27.0, "fat": 0.4, "fiber": 3.1, "basis": "raw"}
GOCHUJANG_WEB = {"name": "gochujang sauces", "macro_source": "WEB ESTIMATE (unverified): example.test; per 1 serving",
                 "calories": 30.0, "protein": 0.8, "carbs": 6.5, "fat": 0.3, "fiber": 0.4,
                 "estimate_provenance": "server_web_estimate",
                 "attribution": {"source_host": "example.test", "verification_state": "unverified_web_estimate",
                                 "cache_allowed": False}}

FOODS = {"raw ahi tuna": TUNA_RAW, "cooked ahi tuna": TUNA_COOKED, "sticky rice": RICE, "banana": BANANA,
         "raw 93/7 ground beef": {**TUNA_RAW, "macro_source": "USDA FDC 174036: beef, raw"},
         "raw chicken breast": {**TUNA_RAW, "macro_source": "USDA FDC 171077: chicken, raw"}}

MEAL = {"add": [
    {"description": "Ahi Tuna", "quantity": 3.5, "unit": "oz"},
    {"description": "Sticky Rice with Gyoza sauce"},
    {"description": "Gochujang sauces"},
    {"description": "Fried onions", "quantity": 1, "unit": "serving"}]}

RAW = {"edit": [{"item_ref": "item1", "preparation": "raw"}]}


class FakeDraftStore:
    """In-memory stand-in for the draft store: CAS saves and a commit keyed by
    (draft, revision) that replays instead of writing twice."""

    def __init__(self):
        self.drafts, self.entries, self.commits, self.operations = {}, {}, [], {}

    async def fetch_meal_draft_context(self, **_):
        drafts = [deepcopy(d) for d in self.drafts.values()]
        return {"open": [d for d in drafts if d["state"] in {"collecting", "needs_detail", "ready"}],
                "committed": [d for d in drafts if d["state"] == "committed"]}

    def _clean(self, draft, revision):
        d = deepcopy({k: v for k, v in draft.items() if not k.startswith("_")})
        for i, item in enumerate(d["items"]):
            item["position"] = i
            item.pop("_live", None)
        d["revision"] = revision
        d.setdefault("committed_entry_id", None)
        d.setdefault("committed_entry_revision", None)
        json.dumps(d, default=str)  # everything stored must serialize
        return d

    async def create_meal_draft(self, draft):
        d = self._clean(draft, 1)
        self.drafts[d["id"]] = d
        return deepcopy(d)

    async def fetch_meal_draft(self, draft_id):
        return deepcopy(self.drafts.get(str(draft_id)))

    async def save_meal_draft(self, draft, expected):
        current = self.drafts[str(draft["id"])]
        if current["revision"] != expected or current["state"] not in {"collecting", "needs_detail", "ready"}:
            return None
        d = self._clean(draft, expected + 1)
        for item in d["items"]:
            assert item["status"] in {"included", "removed"}
            assert item.get("needs") in {None, "identity", "portion", "preparation", "contents", "estimate_consent"}
        self.drafts[d["id"]] = d
        return deepcopy(d)

    async def cancel_meal_draft(self, draft_id, expected):
        self.drafts[str(draft_id)]["state"] = "canceled"

    async def commit_meal_draft(self, draft_id, rev, request_hash, values, metadata):
        from store import DraftConflict
        key = (draft_id, rev)
        if key in self.operations:
            return {**deepcopy(self.operations[key]), "_operation_replayed": True}
        draft = self.drafts[draft_id]
        if draft["state"] != "ready" or draft["revision"] != rev:
            raise DraftConflict("changed")
        self.commits.append((draft_id, rev, values, metadata))
        fields = {"name": values["name"], "meal": values["meal"], "macro_source": values["macro_source"],
                  **{k: values.get(k) for k in domain.MACRO_KEYS}}
        if draft.get("amends_entry_id"):
            entry = self.entries[draft["amends_entry_id"]]
            entry.update(fields, entry_revision=entry["entry_revision"] + 1)
            outcome = "amended"
        else:
            entry = {"id": str(uuid4()), "entry_revision": 1, **fields}
            self.entries[entry["id"]] = entry
            outcome = "committed"
        draft.update(state="committed", committed_entry_id=entry["id"],
                     committed_entry_revision=entry["entry_revision"], meal_type=values["meal"])
        self.operations[key] = {"outcome": outcome, "entry": dict(entry), "day_total": None}
        return deepcopy(self.operations[key])

    async def save_usual(self, *, label, items, totals, macro_source, meal):
        self.usuals = getattr(self, "usuals", []) + [(label, deepcopy(items), dict(totals))]
        return {"template_revision": 1}

    async def fetch_entry_revision(self, entry_id):
        return deepcopy(self.entries.get(str(entry_id)))

    async def fetch_food_default(self, _identity):
        return None

    async def set_food_default(self, _identity, _preparation):
        return None


@pytest.fixture
def engine():
    import server as srv
    store = FakeDraftStore()
    calls: list[str] = []
    foods = dict(FOODS)

    async def resolve(component):
        description = str(component["description"]).casefold()
        calls.append(description)
        found = foods.get(description)
        found = found() if callable(found) else found  # a source that can stop answering
        return dict(found) if found else None

    async def never(_description):  # the draft path must never ask the brand question
        raise AssertionError("unresolved/brand question requested")

    eng = MealDrafts(store=store, resolve=resolve, unresolved_question=never,
                     validate_values=srv._validated_meal_values, clock=lambda: DINNER)
    scope = bind_user(uuid4())
    try:
        yield {"engine": eng, "store": store, "calls": calls, "foods": foods}
    finally:
        reset_user(scope)


def _texts(*results):
    return " ".join(str(r.get("question") or "") + " " + str(r.get("confirmation") or "") for r in results)


def _total(*panels, key="calories"):
    return round(sum(p[key] for p in panels), 2)


# --------------------------------------------------------------------------- #
# The reported dialogue
# --------------------------------------------------------------------------- #

async def test_real_dialogue_asks_only_raw_or_cooked_then_logs_partial_meal_once(engine):
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle(MEAL, session_ref="canvas:s")
    assert first["status"] == "needs_detail"
    assert first["question"] == "Was that 3.5 oz of Ahi Tuna weighed raw or cooked?"
    assert store.commits == []
    second = await eng.handle({"draft_ref": first["draft_ref"], **RAW})
    assert second["status"] == "committed", second
    assert len(store.commits) == 1
    text = second["confirmation"]
    # Verified first, then the estimate with its source and what it left out.
    assert "3.5 oz Ahi Tuna, weighed raw" in text
    assert "Sticky Rice (est., generic; Gyoza sauce not counted)" in text
    assert text.index("Ahi Tuna") < text.index("Sticky Rice") < text.index("Total:") < text.index("Not counted:")
    assert "Not counted: Gochujang sauces" in text and "1 serving Fried onions" in text
    assert "tell me what it was to add it" in text
    # Only counted items are in the entry and its totals.
    logged = second["logged"]
    assert "Gochujang" not in logged["name"] and "Fried onions" not in logged["name"]
    assert "Gyoza" not in logged["name"]
    assert logged["calories"] == _total(TUNA_RAW, RICE)
    metadata = store.commits[0][3]["components"]
    rice = next(c for c in metadata if "Sticky Rice" in c["description"])
    assert rice["estimate_provenance"] == "generic_core_fallback"
    assert "brand" not in _texts(first, second).casefold()
    # Resolved items are not looked up again after the answer.
    assert engine["calls"].count("sticky rice") == 1


async def test_dialogue_variant_web_estimate_is_auto_included_and_disclosed(engine):
    engine["foods"]["gochujang sauces"] = GOCHUJANG_WEB
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle(MEAL)
    assert first["question"] == "Was that 3.5 oz of Ahi Tuna weighed raw or cooked?"  # no "Use it?" consent
    second = await eng.handle({"draft_ref": first["draft_ref"], **RAW})
    assert second["status"] == "committed", second
    text = second["confirmation"]
    assert "Gochujang sauces (est., example.test)" in text
    assert "Not counted: 1 serving Fried onions" in text
    assert second["logged"]["calories"] == _total(TUNA_RAW, RICE, GOCHUJANG_WEB)
    assert len(store.commits) == 1


async def test_option_a_single_web_estimate_logs_without_consent(engine):
    engine["foods"]["fixture stew"] = {**GOCHUJANG_WEB, "name": "fixture stew"}
    result = await engine["engine"].handle({"add": [{"description": "fixture stew", "quantity": 1}]})
    assert result["status"] == "committed", result
    assert "(est., example.test)" in result["confirmation"]


async def test_old_draft_waiting_on_consent_still_accepts_consent_estimate(engine):
    engine["foods"]["fixture stew"] = {**GOCHUJANG_WEB, "name": "fixture stew"}
    eng, store = engine["engine"], engine["store"]
    created = await eng.handle({"intent": "plan", "add": [{"description": "fixture stew", "quantity": 1}]})
    draft = store.drafts[created["draft_ref"]]
    # Simulate a draft saved by the previous release: waiting on consent.
    draft.update(intent="consume", state="needs_detail", pending_question="Use it?")
    draft["items"][0].update(needs="estimate_consent", question="Use it?")
    logged = await eng.handle({"draft_ref": created["draft_ref"], "consent_estimate": ["item1"]})
    assert logged["status"] == "committed", logged
    assert len(store.commits) == 1


async def test_just_log_the_tuna_removes_the_rest_and_commits_once(engine):
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle(MEAL)
    # "Raw. JUST LOG THE TUNA": exactly what the instructions map it to.
    assert '"just log X", "only the X"' in MEAL_DRAFT_INSTRUCTIONS and "only:[" in MEAL_DRAFT_INSTRUCTIONS
    done = await eng.handle({"draft_ref": first["draft_ref"], **RAW, "only": ["item1"]})
    assert done["status"] == "committed", done
    assert len(store.commits) == 1
    assert "Ahi Tuna" in done["logged"]["name"] and "Rice" not in done["logged"]["name"]
    assert done["logged"]["calories"] == TUNA_RAW["calories"]
    assert "Not counted" not in done["confirmation"]  # removed on purpose, not "not counted"


async def test_log_now_with_unanswered_raw_or_cooked_never_reasks(engine):
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle({"add": [MEAL["add"][0], MEAL["add"][1]]})
    assert "raw or cooked" in first["question"]
    done = await eng.handle({"draft_ref": first["draft_ref"], "log_now": True})
    assert done["status"] == "committed", done
    assert "raw or cooked?" not in _texts(done)
    assert "Not counted: 3.5 oz Ahi Tuna" in done["confirmation"]
    assert done["logged"]["calories"] == RICE["calories"]
    # No basis was guessed for the tuna.
    assert not any("ahi tuna" in call for call in engine["calls"])
    assert len(store.commits) == 1


async def test_skip_lane_marks_item_not_counted(engine):
    eng = engine["engine"]
    first = await eng.handle({"add": [MEAL["add"][0], {"description": "banana", "quantity": 1}]})
    done = await eng.handle({"draft_ref": first["draft_ref"], "skip": ["item1"]})
    assert done["status"] == "committed", done
    assert done["logged"]["calories"] == BANANA["calories"]
    assert "Not counted: 3.5 oz Ahi Tuna" in done["confirmation"]


async def test_ask_once_second_turn_without_answer_does_not_repeat(engine):
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle({"add": [MEAL["add"][0]]})
    assert first["question"] == "Was that 3.5 oz of Ahi Tuna weighed raw or cooked?"
    # Next turn, the user says something else ("pick any brand"); no answer for the tuna.
    second = await eng.handle({"draft_ref": first["draft_ref"]})
    assert "raw or cooked?" not in _texts(second)
    # Both raw and cooked panels exist, so no basis is assumed: nothing is invented.
    assert store.commits == []
    assert "nothing" in second["confirmation"].casefold() and "Ahi Tuna" in second["confirmation"]
    third = await eng.handle({"draft_ref": first["draft_ref"]})
    assert "raw or cooked?" not in _texts(third)


async def test_ask_once_assumes_the_only_sourced_basis_and_says_so(engine):
    del engine["foods"]["cooked ahi tuna"]  # only a raw panel exists
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle({"add": [MEAL["add"][0]]})
    second = await eng.handle({"draft_ref": first["draft_ref"]})
    assert second["status"] == "committed", second
    assert "assumed raw" in second["confirmation"]
    assert second["logged"]["calories"] == TUNA_RAW["calories"]
    assert len(store.commits) == 1


async def test_not_counted_never_invents_macros(engine):
    eng, store = engine["engine"], engine["store"]
    partial = await eng.handle({"add": [{"description": "banana", "quantity": 1},
                                        {"description": "mystery stew", "quantity": 1, "unit": "bowl"}]})
    assert partial["status"] == "committed", partial
    assert partial["logged"]["calories"] == BANANA["calories"]
    assert "mystery stew" not in partial["logged"]["name"]
    assert "Not counted: 1 bowl mystery stew" in partial["confirmation"]
    assert len(store.commits[0][3]["components"]) == 1
    nothing = await eng.handle({"add": [{"description": "mystery stew"}]})
    assert nothing["status"] == "needs_detail"
    assert len(store.commits) == 1  # nothing written for a meal with no counted food
    assert "mystery stew" in nothing["question"] and "brand" not in nothing["question"].casefold()


async def test_retry_of_the_same_commit_replays_without_a_second_write(engine):
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle(MEAL)
    done = await eng.handle({"draft_ref": first["draft_ref"], **RAW})
    retry = await eng.handle({"draft_ref": first["draft_ref"], **RAW})
    recover = await eng.handle({"draft_ref": first["draft_ref"]})
    assert done["status"] == "committed"
    assert retry["status"] == "replayed" and retry["entry_id"] == done["entry_id"]
    assert recover["status"] == "replayed"
    assert len(store.commits) == 1


async def test_one_question_per_reply(engine):
    eng = engine["engine"]
    first = await eng.handle({"add": [{"description": "93/7 ground beef", "quantity": 3, "unit": "oz"},
                                      {"description": "chicken breast", "quantity": 4, "unit": "oz"},
                                      {"description": "mystery stew"}]})
    assert first["question"] == "Was that 3 oz of 93/7 ground beef weighed raw or cooked?"
    assert first["question"].count("?") == 1
    second = await eng.handle({"draft_ref": first["draft_ref"], **RAW})
    # The chicken was never asked, so it gets its own single question now.
    assert second["question"] == "Was that 4 oz of chicken breast weighed raw or cooked?"
    third = await eng.handle({"draft_ref": first["draft_ref"],
                              "edit": [{"item_ref": "item2", "preparation": "raw"}]})
    assert third["status"] == "committed", third


def test_schema_and_instructions_offer_the_lanes():
    props = MEAL_DRAFT_TOOL["input_schema"]["properties"]
    assert props["log_now"]["type"] == "boolean" and props["skip"]["type"] == "array"
    for phrase in ("log_now", "skip", "just log", "any brand", "never send nutrition numbers"):
        assert phrase in MEAL_DRAFT_INSTRUCTIONS
    assert "brand" not in MEAL_DRAFT_TOOL["description"].casefold().replace("brands", "")


# --------------------------------------------------------------------------- #
# The server writes the final reply: no extra provider round
# --------------------------------------------------------------------------- #

def _tool_call_response(name, arguments):
    return httpx.Response(200, request=httpx.Request("POST", "https://example.test"), json={
        "choices": [{"message": {"content": None, "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]})


def _text_response(text):
    return httpx.Response(200, request=httpx.Request("POST", "https://example.test"),
                          json={"choices": [{"message": {"content": text}}]})


async def test_run_agent_end_turn_returns_server_confirmation_after_one_call(monkeypatch):
    from coach import run_agent
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "offline")
    posts = []

    async def post(_token, payload):
        posts.append(payload)
        if len(posts) == 1:
            return _tool_call_response("update_meal", {"add": [{"description": "banana"}]})
        return _text_response("Model paraphrase that should never be used.")

    async def update_meal(_args):
        return {"status": "committed", "confirmation": "Logged as Snack: 1 banana.", "end_turn": True}

    reply, audit = await run_agent(history=[], message="a banana", onboarding=False,
                                   handlers={"update_meal": update_meal}, post=post,
                                   tool_catalog=[MEAL_DRAFT_TOOL])
    assert len(posts) == 1
    assert reply == "Logged as Snack: 1 banana."
    assert audit[0]["tool"] == "update_meal"


async def test_run_agent_without_end_turn_still_asks_the_model(monkeypatch):
    from coach import run_agent
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "offline")
    posts = []

    async def post(_token, payload):
        posts.append(payload)
        if len(posts) == 1:
            return _tool_call_response("get_today", {})
        return _text_response("You have 1200 kcal left.")

    async def get_today(_args):
        return {"calories": 800}

    reply, _ = await run_agent(history=[], message="how am I doing", onboarding=False,
                               handlers={"get_today": get_today}, post=post)
    assert len(posts) == 2 and reply == "You have 1200 kcal left."


@pytest.mark.parametrize("status, expected", [
    ("committed", "Logged as Dinner: 3.5 oz Ahi Tuna, weighed raw. Want me to remember these exact items?"),
    ("needs_detail", "Was that 3.5 oz of Ahi Tuna weighed raw or cooked?"),
])
async def test_canvas_update_meal_ends_the_turn_with_the_server_text(monkeypatch, status, expected):
    from agent_canvas import CanvasService
    from coach import run_agent
    from test_agent_canvas import MemoryStore
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "offline")
    posts = []

    class Drafts:
        async def context(self):
            return {"open_meals": [], "logged_meals": []}

        async def handle(self, args, *, session_ref=None, **_turn):
            if status == "committed":
                return {"status": "committed", "draft_ref": "d1", "logged": {"id": "row-1", "name": "tuna"},
                        "confirmation": "Logged as Dinner: 3.5 oz Ahi Tuna, weighed raw.",
                        "offer": "Want me to remember these exact items?"}
            return {"status": "needs_detail", "draft_ref": "d1", "items": [],
                    "question": expected, "confirmation": expected}

    async def post(_token, payload):
        posts.append(payload)
        if len(posts) == 1:
            return _tool_call_response("update_meal", {"draft_ref": "d1", **RAW})
        return _text_response("Model paraphrase that should never be used.")

    async def drafts_factory():
        return Drafts()

    service = CanvasService(store_factory=lambda: MemoryStore(),
                            food_factory=lambda: {"log_meal": None, "lookup_food": None},
                            coach_factory=lambda: {}, agent=functools.partial(run_agent, post=post),
                            drafts_factory=drafts_factory)
    scope = bind_user(uuid4())
    try:
        output = await service.turn(str(uuid4()), str(uuid4()), "Raw", adapter="text")
    finally:
        reset_user(scope)
    assert len(posts) == 1
    assert output["reply"] == expected


# --------------------------------------------------------------------------- #
# Review round 1 (B1, B2, N1-N7)
# --------------------------------------------------------------------------- #

FATSECRET = {"provider": "FatSecret", "external_id": "999", "verification_state": "provider_exact_identity",
             "cache_allowed": False}


async def test_b1_unlicensed_core_panel_refreshes_by_core_identity_and_commits(engine):
    engine["foods"]["sticky rice"] = {**RICE, "macro_source": "FatSecret: 999", "attribution": FATSECRET}
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle(MEAL)
    stored_rice = store.drafts[first["draft_ref"]]["items"][1]["resolution"]
    assert stored_rice["refresh"] is True and "calories" not in stored_rice  # never cached
    done = await eng.handle({"draft_ref": first["draft_ref"], **RAW})
    assert done["status"] == "committed", done
    assert done["logged"]["calories"] == _total(TUNA_RAW, RICE)
    saved = await eng.handle({"save_usual": {"draft_ref": first["draft_ref"], "label": "poke"}})
    assert saved["status"] == "saved_usual", saved


async def test_b1_failed_refresh_is_not_counted_and_the_rest_commits(engine):
    engine["foods"]["banana"] = {**BANANA, "macro_source": "FatSecret: 999", "attribution": FATSECRET}
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle({"add": [MEAL["add"][0], {"description": "banana", "quantity": 1}]})
    del engine["foods"]["banana"]  # the provider no longer returns it
    done = await eng.handle({"draft_ref": first["draft_ref"], **RAW})
    assert done["status"] == "committed", done
    assert done["logged"]["calories"] == TUNA_RAW["calories"]
    assert "Not counted: 1 banana (nutrition source unavailable)" in done["confirmation"]
    assert len(store.commits) == 1


def _canvas(engine_, post):
    from agent_canvas import CanvasService
    from coach import run_agent
    from test_agent_canvas import MemoryStore

    async def drafts_factory():
        return engine_

    return CanvasService(store_factory=lambda: MemoryStore(),
                         food_factory=lambda: {"log_meal": None, "lookup_food": None},
                         coach_factory=lambda: {}, agent=functools.partial(run_agent, post=post),
                         drafts_factory=drafts_factory)


def _tool_calls_response(*calls):
    return httpx.Response(200, request=httpx.Request("POST", "https://example.test"), json={
        "choices": [{"message": {"content": None, "tool_calls": [{
            "id": f"call-{i}", "type": "function",
            "function": {"name": "update_meal", "arguments": json.dumps(args)}}
            for i, args in enumerate(calls)]}}]})


async def _two_turns(engine, monkeypatch, second_round):
    """Turn 1 through the canvas adds the meal; turn 2 runs `second_round(ref)`
    as ONE provider round with several update_meal calls."""
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "offline")
    rounds = []

    async def post(_token, payload):
        rounds.append(payload)
        if len(rounds) == 1:
            return _tool_calls_response(MEAL)
        ref = next(iter(engine["store"].drafts))
        return _tool_calls_response(*second_round(ref))

    service = _canvas(engine["engine"], post)
    session = str(uuid4())
    first = await service.turn(session, str(uuid4()), "Ahi Tuna 3.5 oz, ...", adapter="text")
    second = await service.turn(session, str(uuid4()), "Raw. JUST LOG THE TUNA", adapter="text")
    assert len(rounds) == 2  # one provider call per turn
    return first, second


async def test_b2_remove_then_answer_in_one_round_logs_the_tuna_raw(engine, monkeypatch):
    first, second = await _two_turns(engine, monkeypatch, lambda ref: [
        {"draft_ref": ref, "remove": ["item2", "item3", "item4"]},
        {"draft_ref": ref, **RAW}])
    assert first["reply"] == "Was that 3.5 oz of Ahi Tuna weighed raw or cooked?"
    assert len(engine["store"].commits) == 1
    assert "3.5 oz Ahi Tuna, weighed raw" in second["reply"] and "Nothing was logged" not in second["reply"]
    assert engine["store"].commits[0][2]["calories"] == TUNA_RAW["calories"]


async def test_b2_add_then_answer_in_one_round_logs_everything(engine, monkeypatch):
    _, second = await _two_turns(engine, monkeypatch, lambda ref: [
        {"draft_ref": ref, "add": [{"description": "banana", "quantity": 1}]},
        {"draft_ref": ref, **RAW}])
    assert len(engine["store"].commits) == 1
    assert engine["store"].commits[0][2]["calories"] == _total(TUNA_RAW, RICE, BANANA)
    assert "3.5 oz Ahi Tuna, weighed raw" in second["reply"]
    assert second["reply"].count("Logged") == 1


async def test_b2_unanswered_question_settles_at_the_end_of_the_next_turn(engine, monkeypatch):
    # The user ignores the question ("pick any brand"); the model sends a bare
    # draft_ref. The question is not repeated: the turn ends with a receipt.
    del engine["foods"]["cooked ahi tuna"]
    _, second = await _two_turns(engine, monkeypatch, lambda ref: [{"draft_ref": ref}])
    assert "raw or cooked?" not in second["reply"]
    assert "(assumed raw)" in second["reply"] and len(engine["store"].commits) == 1


def _entries(engine):
    return list(engine["store"].entries.values())


@pytest.mark.parametrize("later_call, calories, names_in, names_out", [
    # The user's real "Raw. JUST LOG THE TUNA": answer first, then the lane.
    ({"only": ["item1"]}, TUNA_RAW["calories"], ["Ahi Tuna"], ["Rice"]),
    ({"remove": ["item2", "item3", "item4"]}, TUNA_RAW["calories"], ["Ahi Tuna"], ["Rice"]),
    ({"add": [{"description": "banana", "quantity": 1}]}, _total(TUNA_RAW, RICE, BANANA),
     ["Ahi Tuna", "Sticky Rice", "banana"], []),
])
async def test_blocker_completing_call_first_then_more_edits_amend_the_same_entry(
        engine, monkeypatch, later_call, calories, names_in, names_out):
    _, second = await _two_turns(engine, monkeypatch, lambda ref: [
        {"draft_ref": ref, **RAW}, {"draft_ref": ref, **later_call}])
    entries = _entries(engine)
    assert len(entries) == 1  # one entry: the second call amended it, never a second write
    entry = entries[0]
    assert entry["calories"] == calories and entry["entry_revision"] == 2
    assert all(n in entry["name"] for n in names_in) and not any(n in entry["name"] for n in names_out)
    # The reply is the final receipt only.
    assert second["reply"].startswith("Updated as Dinner:") and "Logged" not in second["reply"]
    assert "already logged" not in second["reply"]
    assert all(n in second["reply"] for n in names_in)


async def _turns(engine, monkeypatch, *rounds):
    """One canvas turn per round. Round 1 adds MEAL; each later round is
    `fn(refs)` -> the update_meal calls of ONE provider round, where refs is
    the list of draft ids in creation order."""
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "offline")
    posts = []

    async def post(_token, _payload):
        posts.append(1)
        refs = list(engine["store"].drafts)
        return _tool_calls_response(*(rounds[len(posts) - 2](refs) if len(posts) > 1 else [MEAL]))

    service, session = _canvas(engine["engine"], post), str(uuid4())
    outputs = []
    for _ in range(len(rounds) + 1):
        outputs.append(await service.turn(session, str(uuid4()), "...", adapter="text"))
    assert len(posts) == len(rounds) + 1  # one provider call per turn
    return outputs


def _one_entry(engine, calories, revision):
    entries = _entries(engine)
    assert len(entries) == 1, entries  # never a second write
    assert entries[0]["calories"] == calories and entries[0]["entry_revision"] == revision, entries[0]
    return entries[0]


def _open_drafts(engine):
    return [d for d in engine["store"].drafts.values() if d["state"] in {"collecting", "needs_detail", "ready"}]


async def test_class_raw_only_then_noop_log_now_keeps_the_amended_receipt(engine, monkeypatch):
    *_, out = await _turns(engine, monkeypatch, lambda r: [
        {"draft_ref": r[0], **RAW}, {"draft_ref": r[0], "only": ["item1"]}, {"draft_ref": r[0], "log_now": True}])
    entry = _one_entry(engine, TUNA_RAW["calories"], 2)
    assert "Rice" not in entry["name"]
    assert "3.5 oz Ahi Tuna, weighed raw" in out["reply"] and "Rice" not in out["reply"]
    assert "Total: 108 kcal" in out["reply"]


async def test_class_raw_then_removes_one_by_one(engine, monkeypatch):
    *_, out = await _turns(engine, monkeypatch, lambda r: [
        {"draft_ref": r[0], **RAW}, {"draft_ref": r[0], "remove": ["item2"]},
        {"draft_ref": r[0], "remove": ["item3"]}, {"draft_ref": r[0], "remove": ["item4"]}])
    _one_entry(engine, TUNA_RAW["calories"], 2)
    assert "Ahi Tuna" in out["reply"] and "Rice" not in out["reply"] and "Total: 108 kcal" in out["reply"]


async def test_class_raw_removes_then_add_banana_is_not_dropped(engine, monkeypatch):
    *_, out = await _turns(engine, monkeypatch, lambda r: [
        {"draft_ref": r[0], **RAW}, {"draft_ref": r[0], "remove": ["item2"]},
        {"draft_ref": r[0], "remove": ["item3", "item4"]},
        {"draft_ref": r[0], "add": [{"description": "banana", "quantity": 1}]}])
    entry = _one_entry(engine, _total(TUNA_RAW, BANANA), 3)
    assert "banana" in entry["name"] and "Rice" not in entry["name"]
    assert "banana" in out["reply"] and "Rice" not in out["reply"]


async def test_class_amend_that_stops_on_a_question_is_the_head(engine, monkeypatch):
    chicken = {"description": "chicken breast", "quantity": 5, "unit": "oz"}
    _, asked, done = await _turns(
        engine, monkeypatch,
        lambda r: [{"draft_ref": r[0], **RAW}, {"draft_ref": r[0], "add": [chicken]},
                   {"draft_ref": r[0], "remove": ["item2"]}],
        lambda r: [{"draft_ref": r[-1], "edit": [{"item_ref": "item5", "preparation": "raw"}]}])
    assert asked["reply"] == "Was that 5 oz of chicken breast weighed raw or cooked?"
    entry = _one_entry(engine, _total(TUNA_RAW, FOODS["raw chicken breast"]), 2)
    assert "chicken breast" in entry["name"] and "Rice" not in entry["name"]
    assert "chicken breast" in done["reply"] and "Rice" not in done["reply"]
    assert _open_drafts(engine) == []  # no stale open amend left behind


async def test_class_two_amends_of_a_meal_logged_in_an_earlier_turn(engine, monkeypatch):
    *_, out = await _turns(
        engine, monkeypatch,
        lambda r: [{"draft_ref": r[0], **RAW}],
        lambda r: [{"amend_ref": r[0], "edit": [{"item_ref": "item1", "preparation": "cooked"}]},
                   {"amend_ref": r[0], "add": [{"description": "banana", "quantity": 1}]}])
    entry = _one_entry(engine, _total(TUNA_COOKED, RICE, BANANA), 3)
    assert "weighed cooked" in entry["name"] and "banana" in entry["name"]
    assert out["reply"].startswith("Updated as Dinner:") and out["reply"].count("Updated") == 1
    assert "weighed cooked" in out["reply"] and "banana" in out["reply"] and "weighed raw" not in out["reply"]


async def test_class_failure_after_a_receipt_in_one_round_reaches_the_reply(engine, monkeypatch):
    answers = iter([True, True])  # turn 1 lookup, then the commit's refresh; then gone
    engine["foods"]["banana"] = lambda: (
        {**BANANA, "macro_source": "FatSecret: 999", "attribution": FATSECRET} if next(answers, False) else None)
    meal = {"add": [MEAL["add"][0], {"description": "banana", "quantity": 1}]}
    monkeypatch.setitem(globals(), "MEAL", meal)
    _, out = await _turns(engine, monkeypatch, lambda r: [
        {"draft_ref": r[0], **RAW}, {"draft_ref": r[0], "add": [{"description": "sticky rice"}]}])
    _one_entry(engine, _total(TUNA_RAW, BANANA), 1)
    assert "Logged as Dinner: 3.5 oz Ahi Tuna, weighed raw + 1 banana" in out["reply"]
    assert "I couldn't re-check 1 banana right now, so nothing changed." in out["reply"]


async def test_stale_draft_of_an_amended_meal_returns_conflict_not_old_receipt(engine):
    eng = engine["engine"]
    first = await eng.handle(MEAL)
    await eng.handle({"draft_ref": first["draft_ref"], **RAW})
    await eng.handle({"amend_ref": first["draft_ref"], "only": ["item1"]})
    for args in ({"draft_ref": first["draft_ref"]}, {"draft_ref": first["draft_ref"], "log_now": True}):
        stale = await eng.handle(args)
        assert stale["status"] == "conflict", stale
        assert "Rice" not in stale["confirmation"]


async def test_s1_amend_with_unavailable_uncacheable_panel_changes_nothing(engine):
    engine["foods"]["banana"] = {**BANANA, "macro_source": "FatSecret: 999", "attribution": FATSECRET}
    eng, store = engine["engine"], engine["store"]
    logged = await eng.handle({"add": [{"description": "banana", "quantity": 1}]})
    assert logged["status"] == "committed", logged
    del engine["foods"]["banana"]  # the provider no longer answers for it
    amended = await eng.handle({"amend_ref": logged["draft_ref"],
                                "add": [{"description": "sticky rice", "quantity": 1, "unit": "cup"}]})
    assert amended["status"] == "failed", amended
    assert amended["confirmation"] == "I couldn't re-check 1 banana right now, so nothing changed."
    entry = _entries(engine)[0]
    assert entry["entry_revision"] == 1 and entry["calories"] == BANANA["calories"]
    assert len(store.commits) == 1
    assert not [d for d in store.drafts.values() if d["state"] in {"collecting", "needs_detail", "ready"}]


async def test_n1_identical_end_turn_replies_in_one_round_are_shown_once(monkeypatch):
    from coach import run_agent
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "offline")

    async def post(_token, _payload):
        return _tool_calls_response({"draft_ref": "d1"}, {"draft_ref": "d1"}, {"draft_ref": "d2"})

    async def update_meal(args):
        if args["draft_ref"] == "d2":
            return {"status": "committed", "draft_ref": "d2", "end_turn": True, "reply": "Logged as Snack: 1 banana."}
        return {"status": "replayed", "draft_ref": "d1", "end_turn": True, "reply": "Logged as Dinner: tuna."}

    reply, _ = await run_agent(history=[], message="x", onboarding=False,
                               handlers={"update_meal": update_meal}, post=post, tool_catalog=[MEAL_DRAFT_TOOL])
    assert reply == "Logged as Dinner: tuna. Logged as Snack: 1 banana."


async def test_n2_re_adding_a_not_counted_food_clears_its_not_counted_record(engine):
    eng = engine["engine"]
    first = await eng.handle({"add": [MEAL["add"][0], {"description": "Fried onions"}]})
    engine["foods"]["fried onions"] = {**BANANA, "name": "fried onions"}  # the user said more; now found
    done = await eng.handle({"draft_ref": first["draft_ref"], **RAW, "add": [{"description": "fried  onions"}]})
    assert done["status"] == "committed", done
    assert "Not counted" not in done["confirmation"]


async def test_n2_amend_add_back_clears_not_counted(engine):
    eng = engine["engine"]
    logged = await eng.handle({"add": [{"description": "banana", "quantity": 1}, {"description": "Fried onions"}]})
    assert "Not counted: Fried onions" in logged["confirmation"]
    engine["foods"]["fried onions"] = {**BANANA, "name": "fried onions"}
    amended = await eng.handle({"amend_ref": logged["draft_ref"], "add": [{"description": "Fried onions"}]})
    assert amended["status"] == "amended", amended
    assert "Not counted" not in amended["confirmation"]


async def test_n3_log_now_on_an_old_consent_draft_accepts_the_estimate(engine):
    engine["foods"]["fixture stew"] = {**GOCHUJANG_WEB, "name": "fixture stew"}
    eng, store = engine["engine"], engine["store"]
    created = await eng.handle({"intent": "plan", "add": [{"description": "fixture stew", "quantity": 1}]})
    draft = store.drafts[created["draft_ref"]]
    draft.update(intent="consume", state="needs_detail", pending_question="Use it?")
    draft["items"][0].update(needs="estimate_consent", question="Use it?")
    logged = await eng.handle({"draft_ref": created["draft_ref"], "log_now": True})  # "that's fine"
    assert logged["status"] == "committed", logged
    assert "(est., example.test)" in logged["confirmation"]


@pytest.mark.parametrize("text, expected", [
    ("sticky rice with gyoza sauce", ("sticky rice", "gyoza sauce")),
    ("steak with peppercorn sauce and mashed potatoes", None),
    ("chicken with rice", None),
    ("rice with soy sauce and sesame dressing", ("rice", "soy sauce and sesame dressing")),
    ("chicken with sweet and sour sauce", ("chicken", "sweet and sour sauce")),
    ("sweet and sour sauce", None),
    ("salt and pepper seasoning", None),
    ("fries with salt & pepper seasoning", ("fries", "salt & pepper seasoning")),
    ("chicken with rice and teriyaki sauce", ("chicken with rice", "teriyaki sauce")),
    ("eggs with toast and hot sauce", ("eggs with toast", "hot sauce")),
    ("greens with oil and vinegar dressing", ("greens", "oil and vinegar dressing")),
])
def test_n4_generic_core_strips_only_a_trailing_condiment(text, expected):
    from meal_drafts import generic_core
    assert generic_core(text) == expected


async def test_n5_log_now_on_a_new_meal_still_asks_raw_or_cooked(engine):
    eng, store = engine["engine"], engine["store"]
    result = await eng.handle({"add": [MEAL["add"][0], MEAL["add"][1]], "log_now": True})
    assert result["status"] == "needs_detail"
    assert result["question"] == "Was that 3.5 oz of Ahi Tuna weighed raw or cooked?"
    assert store.commits == []


async def test_n6_remove_marks_user_removed_and_receipt_omits_it(engine):
    eng, store = engine["engine"], engine["store"]
    first = await eng.handle(MEAL)
    done = await eng.handle({"draft_ref": first["draft_ref"], **RAW, "remove": ["item3", "item4"]})
    assert done["status"] == "committed", done
    assert "Not counted" not in done["confirmation"]
    items = store.drafts[first["draft_ref"]]["items"]
    assert items[2]["field_origins"].get("user_removed") and items[3]["field_origins"].get("user_removed")


async def test_n7_assumed_basis_is_disclosed_on_an_estimated_panel(engine):
    engine["foods"]["raw ahi tuna"] = {**GOCHUJANG_WEB, "name": "ahi tuna", "basis": "per 100 g raw"}
    del engine["foods"]["cooked ahi tuna"]
    eng = engine["engine"]
    first = await eng.handle({"add": [MEAL["add"][0]]})
    done = await eng.handle({"draft_ref": first["draft_ref"]})
    assert done["status"] == "committed", done
    assert "(assumed raw)" in done["confirmation"] and "(est., example.test)" in done["confirmation"]
