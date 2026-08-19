"""Service-level tests for the write/undo paths against a fake Postgres store.

`day_payload()` reconciles its response with the write that just happened:
`ensure` prepends the just-written row when the day query missed it, and
`exclude` drops the just-deleted row when the query still shows it. That keeps
write responses deterministic even if a concurrent write lands between the
INSERT and the SELECT. These tests pin that reconciliation, plus the
normalization and provenance recorded on the way into the store.
"""

import os

import httpx
import pytest

os.environ.setdefault("APP_SHARED_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

import server as srv  # noqa: E402

TODAY = srv.domain.effective_date().isoformat()


def meal_row(row_id, name, meal, cal, protein, carbs, fat, created, fiber=0.0):
    return {
        "id": row_id,
        "name": name,
        "meal": meal,
        "calories": float(cal),
        "protein": float(protein),
        "carbs": float(carbs),
        "fat": float(fat),
        "fiber": float(fiber),
        "date": TODAY,
        "created_time": created,
    }


TARGETS_ROW = {
    "id": "t1",
    "name": "Initial",
    "calories": 2400.0,
    "protein": 215.0,
    "carbs": 200.0,
    "fat": 70.0,
    "fiber": 30.0,
    "effective_date": "2026-01-01",
}


class FakeStore:
    """Store double whose queries can lag the write by one row.

    With ``lagging=True``, ``insert_meal``/``delete`` do not change what the
    query methods return — standing in for a reader that raced the write — so
    the tests can pin that ``day_payload`` reconciles the response anyway.
    """

    def __init__(self, meals, lagging=True):
        self.visible = list(meals)
        self.inserted: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.lagging = lagging

    async def fetch_meals(self, start, end=None):
        return [dict(row) for row in self.visible]

    async def fetch_meal_rollups(self, day):
        return []

    async def fetch_day_rollups(self, start=None, end=None):
        return []

    async def fetch_targets(self, day):
        return dict(TARGETS_ROW)

    async def insert_meal(
        self, *, name, meal, calories, protein, carbs, fat, fiber, day, macro_source
    ):
        row = {
            **meal_row(
                "new-row", name, meal, calories, protein, carbs, fat,
                "2026-07-25T20:00:00+00:00", fiber,
            ),
            "date": day.isoformat(),
            "macro_source": macro_source,
        }
        self.inserted.append(row)
        if not self.lagging:
            self.visible.insert(0, {k: v for k, v in row.items() if k != "macro_source"})
        return row

    async def delete(self, table, row_id):
        self.deleted.append((table, row_id))
        if not self.lagging:
            self.visible = [row for row in self.visible if row["id"] != row_id]


@pytest.fixture
def lagging(monkeypatch):
    fake = FakeStore(
        [meal_row("meal-1", "Eggs", "Breakfast", 280, 28, 2, 18, "2026-07-25T12:00:00+00:00")]
    )
    monkeypatch.setattr(srv, "_client", fake)
    return fake


async def test_log_meal_response_includes_the_row_it_just_wrote(lagging):
    payload = await srv.write_meal(
        name="Core Power shake",
        calories=150,
        protein=30,
        carbs=3,
        fat=2.5,
        macro_source="Fairlife Core Power label",
        meal="snack",
        day_value=None,
    )

    assert [meal["id"] for meal in payload["meals"]] == ["new-row", "meal-1"]
    assert payload["totals"] == {
        "calories": 430.0,
        "protein": 58.0,
        "carbs": 5.0,
        "fat": 20.5,
        "fiber": 0.0,
    }
    assert payload["remaining"]["protein"] == 157.0
    assert payload["logged"]["macro_source"] == "Fairlife Core Power label"
    assert "warning" not in payload


async def test_log_meal_does_not_double_count_once_the_query_catches_up(monkeypatch):
    fake = FakeStore(
        [meal_row("meal-1", "Eggs", "Breakfast", 280, 28, 2, 18, "2026-07-25T12:00:00+00:00")],
        lagging=False,
    )
    monkeypatch.setattr(srv, "_client", fake)
    payload = await srv.write_meal(
        name="Core Power shake",
        calories=150,
        protein=30,
        carbs=3,
        fat=2.5,
        macro_source="Fairlife Core Power label",
        meal="Snack",
        day_value=None,
    )
    assert len(payload["meals"]) == 2
    assert payload["totals"]["protein"] == 58.0


async def test_atwater_warning_rides_along_with_the_write(lagging):
    payload = await srv.write_meal(
        name="Suspicious entry",
        calories=900,
        protein=30,
        carbs=3,
        fat=2.5,
        macro_source="Brand label",
        meal="Snack",
        day_value=None,
    )
    assert "Atwater check" in payload["warning"]
    assert payload["logged"]["id"] == "new-row"  # still written
    assert len(lagging.inserted) == 1


async def test_undo_excludes_the_row_it_just_deleted(lagging):
    payload = await srv.undo_last_meal()
    assert lagging.deleted == [("nutrition_entries", "meal-1")]
    assert payload["removed"]["name"] == "Eggs"
    assert payload["meals"] == []
    assert payload["totals"] == {
        "calories": 0.0,
        "protein": 0.0,
        "carbs": 0.0,
        "fat": 0.0,
        "fiber": 0.0,
    }
    assert payload["remaining"]["protein"] == 215.0


async def test_undo_with_nothing_logged_is_an_actionable_error(monkeypatch):
    monkeypatch.setattr(srv, "_client", FakeStore([]))
    with pytest.raises(Exception) as excinfo:
        await srv.undo_last_meal()
    assert "nothing to undo" in str(excinfo.value)


async def test_write_normalizes_fields_and_records_the_source(lagging):
    await srv.write_meal(
        name="  Chicken ",
        calories=300,
        protein=55,
        carbs=0,
        fat=7,
        macro_source="FDA FoodData Central: chicken breast, roasted",
        meal="dinner",
        day_value="2026-07-20",
    )
    row = lagging.inserted[-1]
    assert row["name"] == "Chicken"
    assert row["meal"] == "Dinner"
    assert row["date"] == "2026-07-20"
    assert row["macro_source"] == "FDA FoodData Central: chicken breast, roasted"
    assert row["calories"] == 300.0 and row["protein"] == 55.0 and row["fat"] == 7.0


async def test_vision_log_uses_openai_token_and_vision_model(monkeypatch):
    captured = {}

    async def fake_post(token, payload):
        captured["token"] = token
        captured["payload"] = payload
        return httpx.Response(
            200,
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
            json={
                "choices": [{
                    "message": {
                        "content": '{"name":"Chicken bowl","calories":500,"protein":40,"carbs":45,"fat":18}'
                    }
                }]
            },
        )

    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "openai-test-token")
    monkeypatch.setattr(srv, "_post_openai_chat", fake_post)

    result = await srv.analyze_food_image("data:image/jpeg;base64,AAAA", "lunch")

    assert captured["token"] == "openai-test-token"
    assert captured["payload"]["model"] == "gpt-4o-mini"
    assert captured["payload"]["messages"][1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,AAAA"},
    }
    assert result["name"] == "Chicken bowl"
    assert result["meal"] == "Lunch"
