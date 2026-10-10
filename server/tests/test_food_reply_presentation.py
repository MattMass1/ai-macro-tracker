"""Canvas reply boundary regressions for machine metadata leaking into speech."""
import copy
from uuid import uuid4

import httpx
import pytest

from agent_canvas import CanvasService, CANVAS_INSTRUCTIONS
from auth import bind_user, reset_user
from coach import run_agent
from test_agent_canvas import MemoryStore


async def canvas_reply(reply, message, adapter="voice", food_result=None):
    store = MemoryStore()
    calls = []

    async def log_meal(call_id, args):
        calls.append((call_id, args))
        return copy.deepcopy(food_result)

    async def agent(**kwargs):
        if food_result is not None:
            await kwargs["handlers"]["log_meal"]({"description": "fixture meal"})
        return reply, []

    service = CanvasService(store_factory=lambda: store,
                            food_factory=lambda: {"log_meal": log_meal},
                            coach_factory=lambda: {}, agent=agent)
    token = bind_user(uuid4())
    try:
        session, turn = str(uuid4()), str(uuid4())
        result = await service.turn(session, turn, message, adapter=adapter)
        replay = await service.turn(session, turn, message, adapter=adapter)
        assert result == replay
        assert len(store.history) == 2
        assert store.history[-1]["content"] == result["reply"]
        for surface in result["surfaces"]:
            for component in surface["components"]:
                if component["component"] == "AgentMessage":
                    assert component["text"] == result["reply"]
        return result, calls
    finally:
        reset_user(token)


@pytest.mark.parametrize("adapter", ["text", "voice"])
@pytest.mark.parametrize(("message", "reply", "nutrition"), [
    ("How many calories in two BearBells Creamy Crisp bars?",
     "Two Barebells Creamy Crisp bars have 400 calories. Source: FatSecret: 65497603; verification state: provider_exact_identity.",
     "400 calories"),
    ("How many calories in two Taco Bell Creamy Chipotle Crispy Chicken Crunchwrap Sliders?",
     "Two Taco Bell Creamy Chipotle Crispy Chicken Crunchwrap Sliders have 640 calories. Source: FatSecret: 12345678; verification state: provider_exact_identity.",
     "640 calories"),
    ("How many calories are in two BearBells Creamy Crisp bars?",
     "Two Barebells Creamy Crisp bars contain **400 calories**. Source: **FatSecret: 65497603**; verification state: **provider_exact_identity**.",
     "**400 calories**"),
    ("How many calories are in two Taco Bell Creamy Chipotle Crispy Chicken Crunchwrap Sliders?",
     "Two Taco Bell Creamy Chipotle Crispy Chicken Crunchwrap Sliders contain **640 calories**. Source: **FatSecret: 124374739**; verification state: **provider_exact_identity**.",
     "**640 calories**"),
])
async def test_live_reply_metadata_is_formatted_before_history_native_and_replay(adapter, message, reply, nutrition):
    result, calls = await canvas_reply(reply, message, adapter)
    assert nutrition in result["reply"]
    assert "FatSecret" in result["reply"]
    assert "provider_exact_identity" not in result["reply"]
    assert "verification state" not in result["reply"]
    assert all(record_id not in result["reply"] for record_id in ("65497603", "12345678", "124374739"))
    assert calls == []


@pytest.mark.parametrize("message", [
    "Who has to verify it?",
    "Stop saying verification_state",
    "Why are you telling me provider_exact_identity?",
    "Give me calories without verification state details",
    "Don’t show verification_state",
    "Why did you show verification_state?",
    "What is the reason you display verification_state?",
])
async def test_complaints_about_metadata_do_not_request_diagnostic_output(message):
    result, _ = await canvas_reply(
        "It has 400 calories. verification_state: provider_exact_identity.", message)
    assert result["reply"] == "It has 400 calories."


@pytest.mark.parametrize("message", [
    "Show me the raw diagnostic metadata",
    "Give me the provider record ID and verification state",
    "What is the verification_state for that result?",
])
async def test_explicit_current_diagnostic_request_preserves_provenance_reply(message):
    reply = "Source: FatSecret: 65497603; verification state: provider_exact_identity."
    result, _ = await canvas_reply(reply, message)
    assert result["reply"] == reply


@pytest.mark.parametrize("reply", [
    "Closest I found: about 400 calories (web estimate, example.test). Should I log it?",
    "The save outcome is not verified yet. Check Today before trying again.",
    "A prior save has an unknown outcome. Refresh Today to check it before logging this again.",
    "For those sliders, did you mean Creamy Chipotle or Jalapeno Honey Mustard?",
    "100 g has 86 calories; 115 g has 98.9 calories. This is an unverified web estimate.",
    "FatSecret: 400 calories and 40 g protein.",
    "Source: FatSecret: 400 calories and 40 g protein.",
])
async def test_nutrition_uncertainty_and_confirmation_words_are_not_censored(reply):
    result, _ = await canvas_reply(reply, "Tell me about that food")
    assert result["reply"] == reply


async def test_labeled_machine_hashes_and_refs_are_removed_without_empty_success_claim():
    reply = ("verification state: provider_exact_identity; "
             f"evidence_hash: {'a' * 64}; resolution_ref: {'b' * 32}.")
    result, calls = await canvas_reply(reply, "How many calories?")
    assert result["reply"]
    assert "provider_exact_identity" not in result["reply"]
    assert "a" * 64 not in result["reply"] and "b" * 32 not in result["reply"]
    assert "saved" not in result["reply"].lower() and "logged" not in result["reply"].lower()
    assert calls == []


async def test_food_result_and_receipt_keep_authoritative_source_and_confirmation():
    food_result = {"status": "committed", "operation_id": "fixture-op",
                   "logged": {"id": "fixture-row", "name": "fixture meal",
                              "macro_source": "FatSecret: 65497603"},
                   "confirmation": "Logged fixture meal: 400 calories.",
                   "attribution": {"verification_state": "provider_exact_identity"}}
    before = copy.deepcopy(food_result)
    result, calls = await canvas_reply(
        "Logged fixture meal: 400 calories. verification state: provider_exact_identity.",
        "Log the meal", food_result=food_result)
    assert result["reply"] == "Logged fixture meal: 400 calories."
    assert food_result == before
    receipt = next(component for surface in result["surfaces"]
                   for component in surface["components"] if component["component"] == "MealReceipt")
    assert receipt["text"] == food_result["confirmation"]
    assert len(calls) == 1


async def test_real_coach_request_receives_canvas_instruction_override(monkeypatch):
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "fixture-only")
    payloads = []
    store = MemoryStore()

    async def post(_token, payload):
        payloads.append(copy.deepcopy(payload))
        return httpx.Response(200, request=httpx.Request("POST", "https://fixture.invalid"), json={"choices": [{"message": {
            "content": "Two bars have 400 calories. verification state: provider_exact_identity."}}]})

    async def agent(**kwargs):
        return await run_agent(post=post, **kwargs)

    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {}, agent=agent)
    token = bind_user(uuid4())
    try:
        result = await service.turn(str(uuid4()), str(uuid4()), "How many calories in two bars?", adapter="voice")
    finally:
        reset_user(token)
    assert payloads[0]["messages"][0] == {"role": "system", "content": CANVAS_INSTRUCTIONS}
    assert result["reply"] == "Two bars have 400 calories."


@pytest.mark.parametrize("adapter", ["text", "voice"])
@pytest.mark.parametrize(("state", "warning"), [
    ("unverified_web_estimate", "web estimate"),
    ("unknown", "unknown"),
    ("provider_search_match", "uncertain"),
])
async def test_machine_state_that_is_the_only_caution_retains_human_warning(adapter, state, warning):
    result, _ = await canvas_reply(
        f"It has 400 calories. verification state: {state}.",
        "How many calories?", adapter)
    assert "400 calories" in result["reply"]
    assert warning in result["reply"].lower()
    assert "verification state" not in result["reply"]


@pytest.mark.parametrize("adapter", ["text", "voice"])
async def test_bold_machine_caution_keeps_warning_and_nutrition_formatting(adapter):
    result, _ = await canvas_reply(
        "It has **400 calories**. **verification state**: **unverified_web_estimate**.",
        "How many calories?", adapter)
    assert "**400 calories**" in result["reply"]
    assert "web estimate" in result["reply"].lower()
    assert "unverified_web_estimate" not in result["reply"]
