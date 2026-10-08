"""Real local resolution of the live-canary wording; writes remain synthetic."""
from uuid import uuid4

import pytest

from auth import bind_user, reset_user
from test_live_coach import FakeVoiceStore, _import_server


@pytest.mark.asyncio
async def test_live_wording_chicken_and_cooked_white_rice_commits_one_composite(monkeypatch):
    srv = _import_server(monkeypatch)
    store = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", store)

    async def forbidden(*args, **kwargs):
        raise AssertionError("known generic aliases must resolve locally")

    monkeypatch.setattr(srv.food_lookup, "search_fatsecret", forbidden)
    monkeypatch.setattr(srv.food_lookup, "search_openfoodfacts", forbidden)
    scope = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("canary-words", {
            "components": [{"description": "grilled chicken breast", "portion": "6 ounces"},
                           {"description": "cooked white rice", "portion": "1 cup"}],
            "meal_type": "Dinner"})
        repeated = await srv._voice_tool_handlers()["log_meal"]("canary-words", {
            "components": [{"description": "grilled chicken breast", "portion": "6 ounces"},
                           {"description": "cooked white rice", "portion": "1 cup"}],
            "meal_type": "Dinner"})
        rows = await store.fetch_meals(srv.domain.effective_date())
    finally:
        reset_user(scope)
    assert result["status"] == "committed"
    assert repeated["status"] == "replayed"
    assert store.insert_count == len(rows) == 1
    assert rows[0]["calories"] == pytest.approx(458.72)
    assert rows[0]["protein"] == pytest.approx(58.54)
    assert len(result["components"]) == 2
    assert "estimate" in result["confirmation"].lower()
    assert result["day_total"]["calories"] == rows[0]["calories"]


@pytest.mark.parametrize(("query", "calories"), [
    ("bacon", 43.28),
    ("2 slices of bacon", 86.56),
    ("3 strips of bacon", 129.84),
    ("three rashers of bacon", 129.84),
    ("two pieces of bacon", 86.56),
    ("streaky bacon", 43.28),
    ("30 g bacon", 162.3),
])
def test_bacon_resolves_locally_per_cooked_slice(query, calories):
    """Bacon used to fall through every provider (OpenFoodFacts outage, no
    FatSecret, web estimate not configured) and the canvas asked the user to
    verify it while the legacy chat silently estimated. A sourced local row
    resolves it per cooked 8 g slice without any network."""
    from food_lookup import resolve_generic_whole_food
    found = resolve_generic_whole_food(query)
    assert found is not None
    assert found["macros_per_serving"]["calories"] == pytest.approx(calories, abs=0.01)
    assert "FDC 167712" in found["source"]
    assert "estimate" not in found["source"].casefold()


@pytest.mark.asyncio
async def test_canvas_and_voice_log_bacon_without_asking_to_verify(monkeypatch):
    srv = _import_server(monkeypatch)
    store = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", store)

    async def forbidden(*args, **kwargs):
        raise AssertionError("bacon must resolve locally, never via a provider")

    monkeypatch.setattr(srv.food_lookup, "search_fatsecret", forbidden)
    monkeypatch.setattr(srv.food_lookup, "search_openfoodfacts", forbidden)
    monkeypatch.setattr(srv.food_lookup.web_nutrition_lookup, "lookup", forbidden)
    scope = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("bacon-1", {
            "description": "3 strips of bacon", "meal_type": "Breakfast"})
        compound = await srv._voice_tool_handlers()["log_meal"]("bacon-2", {
            "description": "2 eggs and 2 slices of bacon", "meal_type": "Breakfast"})
        rows = await store.fetch_meals(srv.domain.effective_date())
    finally:
        reset_user(scope)
    assert result["status"] == "committed", result
    bacon_row = next(row for row in rows if row["name"] == "3 strips of bacon")
    assert bacon_row["calories"] == pytest.approx(129.84)
    assert bacon_row["protein"] == pytest.approx(8.89, abs=0.02)  # per-slice rounding
    assert compound["status"] == "committed", compound
    assert len(compound["components"]) == 2
    assert store.insert_count == 2
    assert "verify" not in (result.get("confirmation") or "").casefold()


def test_cooked_alias_does_not_match_raw_rice():
    from food_lookup import resolve_generic_whole_food
    cooked = resolve_generic_whole_food("1 cup cooked white rice")
    raw = resolve_generic_whole_food("100 g white rice")
    assert cooked is not None
    assert "cooked" in cooked["basis"]
    assert cooked["macros_per_serving"]["calories"] == pytest.approx(206.98)
    assert raw["macros_per_serving"]["calories"] == 355
