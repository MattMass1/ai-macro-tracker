"""Speech spelling regressions through real lookup and voice code, no network."""
from uuid import uuid4

import pytest

from test_food_provider_handoff import provider_fixture
from auth import bind_user, reset_user
import server as srv


@pytest.mark.parametrize("query", [
    "two BearBells Creamy Crisp",
    "two Bear Bells Creamy Crisp",
    "2 bars Bare Bells creamy crisp",
    "2 Barebells creamy crisp",
])
async def test_spoken_brand_alias_searches_canonical_identity_and_saves_two_once(monkeypatch, query):
    fake, calls = provider_fixture(monkeypatch, "Barebells", "Creamy Crisp Protein Bar")
    tenant, other = uuid4(), uuid4()
    token = bind_user(tenant)
    try:
        handlers = srv._voice_tool_handlers()
        found = await handlers["lookup_food"]("spoken-lookup", {"query": query})
        assert found.get("resolution_ref"), found
        assert found["attribution"]["verification_state"] == "provider_exact_identity"
        assert found["applied_quantity"] == 2
        assert fake.insert_count == 0
        args = {"components": [{"description": query, "quantity": 2,
                                 "resolution_ref": found["resolution_ref"]}],
                "meal_type": "Snack"}
        saved = await handlers["log_meal"]("spoken-save", args)
        replay = await handlers["log_meal"]("spoken-save", args)
        assert saved["status"] == "committed" and replay["status"] == "replayed"
        assert saved["logged"]["calories"] == 360
        assert saved["logged"]["protein"] == 40
        assert saved["logged"]["id"] == replay["logged"]["id"]
        assert fake.insert_count == 1
        searches = [c["search_expression"].casefold() for c in calls if c["method"] == "foods.search"]
        assert any("barebells" in search for search in searches)
        assert not any("bearbells" in search or "bear bells" in search or "bare bells" in search
                       for search in searches)
    finally:
        reset_user(token)
    token = bind_user(other)
    try:
        rejected = await handlers["log_meal"]("cross-tenant", args)
        assert rejected["status"] == "needs_clarification"
        assert not fake.meals[other] and fake.insert_count == 1
    finally:
        reset_user(token)


@pytest.mark.parametrize(("query", "provider_name"), [
    ("two BearBells Creamy Crisp", "Creamy Crisp Protein Bar"),
    ("two BearBells Creamy Crisp bars", "Creamy Crisp Protein Bar"),
    ("two Barebells Creamy Crisp bars", "Creamy Crisp Protein Bar"),
    ("two bars BearBells Creamy Crisp", "Creamy Crisp Protein Bar"),
    ("BearBells Creamy Crisp two bars", "Creamy Crisp Protein Bar"),
    ("two BearBells Creamy Crisp protein bars", "Creamy Crisp Bar"),
])
async def test_direct_spoken_logging_resolves_two_without_prior_lookup(monkeypatch, query, provider_name):
    fake, _ = provider_fixture(monkeypatch, "Barebells", provider_name)
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("direct-spoken", {
            "description": query, "meal_type": "Snack",
        })
    finally:
        reset_user(token)
    assert result["status"] == "committed", result
    assert result["logged"]["calories"] == 360
    assert result["logged"]["protein"] == 40
    assert fake.insert_count == 1


@pytest.mark.parametrize("query", [
    "two Barbells Creamy Crisp",
    "two BearBellsFit Creamy Crisp",
    "two BearBells Soft Creamy Crisp",
    "two BearBells Caramel Creamy Crisp",
    "two BearBells",
    "two BearBells Flavor 2 Protein Bar",
    "two BearBells bars",
    "two BearBells Soft Creamy Crisp bars",
    "two BearBells Creamy Crisp protein",
])
async def test_spelling_alias_does_not_drop_brand_flavor_line_or_numeric_identity(monkeypatch, query):
    fake, _ = provider_fixture(monkeypatch, "Barebells", "Creamy Crisp Protein Bar")
    token = bind_user(uuid4())
    try:
        handlers = srv._voice_tool_handlers()
        found = await handlers["lookup_food"]("negative-lookup", {"query": query})
        result = await handlers["log_meal"]("negative-save", {
            "description": query, "meal_type": "Snack",
        })
    finally:
        reset_user(token)
    assert found.get("status") == result.get("status") == "needs_clarification"
    assert fake.insert_count == 0 and not fake.claims


@pytest.mark.parametrize("query", ["two BearBells", "two BearBells bars"])
@pytest.mark.parametrize("provider_name", ["Protein Bar", "Bar"])
async def test_brand_only_query_never_promotes_generic_provider_bar(monkeypatch, query, provider_name):
    fake, _ = provider_fixture(monkeypatch, "Barebells", provider_name)
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("generic-bar", {
            "description": query, "meal_type": "Snack",
        })
    finally:
        reset_user(token)
    assert result["status"] == "needs_clarification", result
    assert fake.insert_count == 0


@pytest.mark.parametrize("description,options", [
    ("Barebells Creamy Crisp", ["Barebells Creamy Crisp"]),
    ("fixture food", ["fixture different food"]),
    ("fixture food", []),
])
def test_food_lookup_failure_describes_missing_nutrition_not_approval(description, options):
    question = srv._unresolved_food_question(description, options)
    assert "verif" not in question.casefold()
    assert "approv" not in question.casefold()
    assert "nutrition" in question.casefold()


async def test_unavailable_exact_spoken_item_never_writes_or_asks_for_external_approval(monkeypatch):
    fake, _ = provider_fixture(monkeypatch, "Barebells", "Creamy Crisp", empty=True)
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("missing-nutrition", {
            "description": "two BearBells Creamy Crisp", "meal_type": "Snack",
        })
    finally:
        reset_user(token)
    assert result["status"] == "needs_clarification"
    assert "verif" not in result["question"].casefold()
    assert "approv" not in result["question"].casefold()
    assert fake.insert_count == 0
