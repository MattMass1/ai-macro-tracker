"""Real provider construction through voice consumption, with synthetic I/O.

Nutrition numbers below are fixture values, not claims about the named foods.
"""
import asyncio
import copy
import os
from uuid import uuid4

import pytest

os.environ.setdefault("APP_SHARED_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://fixture.invalid/not-used")

import food_lookup
import server as srv
from auth import bind_user, reset_user
from test_food_lookup import _enable_fatsecret, _fatsecret_get_hit
from test_live_coach import FakeVoiceStore


def provider_fixture(monkeypatch, brand, name, *, detail_name=None,
                     detail_brand=None, detail_id="fixture-exact", empty=False,
                     alternatives=()):
    _enable_fatsecret(monkeypatch)
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)
    srv._resolved_food_cache.clear()
    calls = []

    async def request(data):
        calls.append(dict(data))
        if data["method"] == "foods.search":
            return {"foods": {"food": [
                {"food_id": "fixture-exact", "food_name": name, "brand_name": brand},
                *({"food_id": f"alternative-{i}", "food_name": option,
                   "brand_name": brand} for i, option in enumerate(alternatives)),
            ]}}
        body = _fatsecret_get_hit(
            detail_name or name, description="1 item", grams=100,
            calories=180, protein=20, carbs=10, fat=6, fiber=2,
        )
        body["food"].update(food_id=detail_id, brand_name=(
            brand if detail_brand is None else detail_brand))
        if empty:
            body["food"]["servings"] = {"serving": []}
        return body

    async def no_web(_query):
        return None

    monkeypatch.setattr(food_lookup, "_fatsecret_request", request)
    monkeypatch.setattr(food_lookup.cofid_lookup, "lookup", no_web)
    monkeypatch.setattr(food_lookup.web_nutrition_lookup, "lookup", no_web)
    return fake, calls


@pytest.mark.parametrize(("brand", "name"), [
    ("Taco Bell", "Creamy Chipotle Crispy Chicken Crunchwrap Slider"),
    ("Chick-fil-A", "Chicken Sandwich"),
    ("Barebells", "Cookies and Cream Protein Bar"),
    ("David", "Protein Bar Cinnamon Bun"),
])
async def test_real_provider_lookup_then_two_items_commit_once(monkeypatch, brand, name):
    fake, _calls = provider_fixture(monkeypatch, brand, name)
    tenant, other = uuid4(), uuid4()
    token = bind_user(tenant)
    try:
        handlers = srv._voice_tool_handlers()
        query = f"{brand} {name}"
        found = await handlers["lookup_food"]("find", {"query": query})
        assert found.get("resolution_ref"), found
        assert found["attribution"]["verification_state"] == "provider_exact_identity"
        assert found["attribution"]["provider"] == "FatSecret"
        args = {"components": [{"description": query, "quantity": 2,
                                 "resolution_ref": found["resolution_ref"]}],
                "meal_type": "Lunch"}
        saved = await handlers["log_meal"]("save", args)
        replay = await handlers["log_meal"]("save", args)
        assert saved["status"] == "committed" and replay["status"] == "replayed"
        assert saved["logged"]["calories"] == 360
        assert saved["logged"]["protein"] == 40
        assert saved["logged"]["macro_source"].startswith("FatSecret: fixture-exact")
        assert saved["logged"]["id"] == replay["logged"]["id"]
        assert fake.insert_count == 1 and len(fake.meals[tenant]) == 1
    finally:
        reset_user(token)
    token = bind_user(other)
    try:
        denied = await handlers["log_meal"]("cross-tenant", args)
        assert denied["status"] == "needs_clarification"
        assert not fake.meals[other] and fake.insert_count == 1
    finally:
        reset_user(token)


@pytest.mark.parametrize("quantity", [None, 2, 3])
async def test_explicit_slider_variant_and_spoken_two_do_not_repeat_pick_list(monkeypatch, quantity):
    fake, _ = provider_fixture(
        monkeypatch, "Taco Bell", "Creamy Chipotle Crispy Chicken Crunchwrap Slider",
        alternatives=["Jalapeno Honey Mustard Crispy Chicken Crunchwrap Slider"],
    )
    token = bind_user(uuid4())
    try:
        args = {
            "description": "two Taco Bell creamy chipotle crispy chicken Crunchwrap sliders",
            "meal_type": "Dinner",
        }
        if quantity is not None:
            args["quantity"] = quantity
        result = await srv._voice_tool_handlers()["log_meal"]("two-sliders", args)
    finally:
        reset_user(token)
    if quantity == 3:
        assert result["status"] == "needs_clarification"
        assert result["reason"] == "invalid_portion"
        assert fake.insert_count == 0
        return
    assert result["status"] == "committed", result
    assert result["logged"]["calories"] == 360
    assert fake.insert_count == 1


async def test_numeric_provider_name_is_identity_not_an_extra_item_count(monkeypatch):
    fake, _ = provider_fixture(monkeypatch, "Fixture Candy", "3 Musketeers Bar")
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("numeric-name", {
            "description": "3 Musketeers Bar", "meal_type": "Snack",
        })
    finally:
        reset_user(token)
    assert result["status"] == "committed", result
    assert result["logged"]["calories"] == 180
    assert fake.insert_count == 1


@pytest.mark.parametrize(("query", "name", "expected"), [
    ("Barebells Creamy Crisp Protein Bar", "Creamy Crisp", "committed"),
    ("Barebells Creamy Crisp", "Creamy Crisp Protein Bar", "committed"),
    ("Barebells Creamy Crisp", "Soft Creamy Crisp Protein Bar", "needs_clarification"),
    ("Barebells Creamy Crisp", "Creamy Crisp Caramel Protein Bar", "needs_clarification"),
    ("Barebells Flavor 2 Protein Bar", "Flavor 3 Protein Bar", "needs_clarification"),
])
async def test_barebells_category_alias_preserves_flavor_line_and_numbers(
    monkeypatch, query, name, expected,
):
    fake, _ = provider_fixture(monkeypatch, "Barebells", name)
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("category-alias", {
            "description": query, "meal_type": "Snack",
        })
    finally:
        reset_user(token)
    assert result["status"] == expected, result
    assert fake.insert_count == (1 if expected == "committed" else 0)


async def test_zero_provider_item_count_never_falls_back_to_one_serving(monkeypatch):
    fake, _ = provider_fixture(monkeypatch, "Taco Bell", "Crunchwrap Supreme")
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("zero-count", {
            "description": "zero Taco Bell Crunchwrap Supremes", "meal_type": "Dinner",
        })
    finally:
        reset_user(token)
    assert result["status"] == "needs_clarification", result
    assert fake.insert_count == 0


async def test_real_provider_composite_attributions_survive_voice_handoff(monkeypatch):
    fake, _ = provider_fixture(monkeypatch, "Taco Bell", "Crunchwrap Supreme")
    query = "2 servings Taco Bell Crunchwrap Supreme and 1 serving Taco Bell Crunchwrap Supreme"
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        found = await handlers["lookup_food"]("composite-lookup", {"query": query})
        assert found.get("resolution_ref"), found
        result = await handlers["log_meal"]("composite-save", {
            "description": query, "resolution_ref": found["resolution_ref"],
            "meal_type": "Dinner",
        })
    finally:
        reset_user(token)
    assert result["status"] == "committed", result
    assert result["logged"]["calories"] == 540
    assert len(result["components"][0]["component_metadata"]) == 2
    assert fake.insert_count == 1


@pytest.mark.parametrize("change", [
    {"detail_name": "Jalapeno Honey Mustard Crispy Chicken Crunchwrap Slider"},
    {"detail_name": "Creamy Chipotle Crispy Chicken Crunchwrap Slider Supreme"},
    {"detail_brand": "Other Restaurant"},
    {"detail_id": "different-id"},
])
async def test_search_detail_identity_changes_never_gain_voice_write(monkeypatch, change):
    fake, _ = provider_fixture(
        monkeypatch, "Taco Bell", "Creamy Chipotle Crispy Chicken Crunchwrap Slider",
        **change,
    )
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("mismatch", {
            "description": "Taco Bell Creamy Chipotle Crispy Chicken Crunchwrap Slider",
            "meal_type": "Dinner",
        })
    finally:
        reset_user(token)
    assert result["status"] == "needs_clarification"
    assert fake.insert_count == 0 and fake.claims == {}


async def test_missing_nutrition_for_explicit_variant_does_not_repeat_identity_question(monkeypatch):
    fake, _ = provider_fixture(
        monkeypatch, "Taco Bell", "Creamy Chipotle Crispy Chicken Crunchwrap Slider",
        empty=True, alternatives=["Jalapeno Honey Mustard Crispy Chicken Crunchwrap Slider"],
    )
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("unavailable", {
            "description": "two Taco Bell Creamy Chipotle Crispy Chicken Crunchwrap Sliders",
            "meal_type": "Dinner",
        })
    finally:
        reset_user(token)
    assert result["status"] == "needs_clarification"
    assert "did you mean" not in result["question"].lower()
    assert "nutrition" in result["question"].lower()
    assert fake.insert_count == 0


@pytest.mark.parametrize(("provider", "state", "external_id", "source"), [
    ("FatSecret", None, "fixture", "FatSecret: fixture"),
    ("FatSecret", "unknown", "fixture", "FatSecret: fixture"),
    ("OpenFoodFacts", "community_text", "fixture", "OpenFoodFacts: fixture"),
    ("OpenFoodFacts", "provider_exact_identity", "fixture", "OpenFoodFacts: fixture"),
    ("FatSecret", "provider_exact_identity", None, "FatSecret: fixture"),
    ("FatSecret", "provider_exact_identity", "other", "FatSecret: fixture"),
    ("FatSecret", "absent_attribution", "fixture", "FatSecret: fixture"),
])
async def test_unknown_or_mismatched_provider_provenance_stays_blocked(
    monkeypatch, provider, state, external_id, source,
):
    fake, _ = provider_fixture(monkeypatch, "Fixture", "Food")
    found = {"name": "Fixture Food", "source": source,
             "macros_per_serving": {"calories": 180, "protein": 20,
                                    "carbs": 10, "fat": 6, "fiber": 2},
             "attribution": {"provider": provider, "external_id": external_id,
                             "verification_state": state}}
    if state == "absent_attribution":
        found.pop("attribution")
    monkeypatch.setattr(srv, "resolve_food", lambda *_a, **_k: asyncio.sleep(
        0, result=copy.deepcopy(found)))
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        lookup = await handlers["lookup_food"]("unknown-lookup", {"query": "Fixture Food"})
        result = await handlers["log_meal"]("unknown-log", {
            "description": "Fixture Food", "meal_type": "Lunch"})
    finally:
        reset_user(token)
    assert lookup.get("status") == result.get("status") == "needs_clarification"
    assert fake.insert_count == 0


@pytest.mark.parametrize("adapter", ["text", "voice"])
async def test_answered_variant_clears_old_canvas_question_after_verified_save(monkeypatch, adapter):
    from agent_canvas import CanvasService
    from test_agent_canvas import MemoryStore

    fake, _ = provider_fixture(
        monkeypatch, "Taco Bell", "Creamy Chipotle Crispy Chicken Crunchwrap Slider",
        alternatives=["Jalapeno Honey Mustard Crispy Chicken Crunchwrap Slider"],
    )

    async def agent(**kwargs):
        result = await kwargs["handlers"]["log_meal"]({
            "description": kwargs["message"], "quantity": 2, "meal_type": "Dinner",
        })
        return result["confirmation"], []

    service = CanvasService(store_factory=MemoryStore,
                            food_factory=srv._voice_tool_handlers,
                            coach_factory=lambda: {}, agent=agent)
    token = bind_user(uuid4())
    try:
        session_id = str(uuid4())
        await service.turn(session_id, str(uuid4()), "Taco Bell Crunchwrap Sliders", adapter=adapter)
        session = service.session(session_id)
        assert session.canvas.surfaces["task"]["components"][0]["component"] == "FoodClarification"
        assert fake.insert_count == 0
        await service.turn(session_id, str(uuid4()),
                           "Taco Bell Creamy Chipotle Crispy Chicken Crunchwrap Sliders", adapter=adapter)
        components = [item["component"] for surface in session.canvas.surfaces.values()
                      for item in surface["components"]]
        assert fake.insert_count == 1
        assert "MealReceipt" in components
        assert "FoodClarification" not in components
    finally:
        reset_user(token)


async def test_food_success_preserves_unrelated_workout_task(monkeypatch):
    from agent_canvas import CanvasService
    from test_agent_canvas import MemoryStore

    fake, _ = provider_fixture(monkeypatch, "Taco Bell", "Crunchwrap Supreme")

    async def agent(**kwargs):
        result = await kwargs["handlers"]["log_meal"]({
            "description": "Taco Bell Crunchwrap Supreme", "meal_type": "Dinner",
        })
        return result["confirmation"], []

    service = CanvasService(store_factory=MemoryStore,
                            food_factory=srv._voice_tool_handlers,
                            coach_factory=lambda: {}, agent=agent)
    token = bind_user(uuid4())
    try:
        session_id = str(uuid4())
        session = service.session(session_id, create=True)
        session.canvas.present("task", "task", [{"id": "workout", "component": "WorkoutOverview"}])
        before = copy.deepcopy(session.canvas.surfaces["task"])
        await service.turn(session_id, str(uuid4()), "log it", adapter="voice")
        assert fake.insert_count == 1
        assert session.canvas.surfaces["task"] == before
    finally:
        reset_user(token)
