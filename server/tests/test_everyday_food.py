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


def test_cooked_alias_does_not_match_raw_rice():
    from food_lookup import resolve_generic_whole_food
    cooked = resolve_generic_whole_food("1 cup cooked white rice")
    raw = resolve_generic_whole_food("100 g white rice")
    assert cooked is not None
    assert "cooked" in cooked["basis"]
    assert cooked["macros_per_serving"]["calories"] == pytest.approx(206.98)
    assert raw["macros_per_serving"]["calories"] == 355
