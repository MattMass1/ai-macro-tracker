"""Service-level tests for the write/undo paths against a fake Postgres store.

`day_payload()` reconciles its response with the write that just happened:
`ensure` prepends the just-written row when the day query missed it, and
`exclude` drops the just-deleted row when the query still shows it. That keeps
write responses deterministic even if a concurrent write lands between the
INSERT and the SELECT. These tests pin that reconciliation, plus the
normalization and provenance recorded on the way into the store.
"""

import json
import os
from uuid import uuid4

import httpx
import pytest

os.environ.setdefault("APP_SHARED_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

import server as srv  # noqa: E402
from starlette.requests import Request  # noqa: E402

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

    async def resolve_device(self, raw_token):
        return uuid4() if raw_token == "device-token" else None


def barcode_request(payload, token="device-token"):
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/food/barcode",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }, receive)


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


async def test_barcode_route_returns_product_and_404(monkeypatch, lagging):
    async def hit(code):
        assert code == "12345678905"  # leading GTIN padding is harmless
        return {
            "name": "Chocolate protein bar",
            "macros_per_100g": {
                "calories": 364.0, "protein": 36.4, "carbs": 32.7,
                "fat": 14.5, "fiber": 5.5,
            },
            "source": "OpenFoodFacts barcode: 12345678905",
            "serving_size": "1 bar (55 g)",
            "macros_per_serving": {"calories": 200.0, "protein": 20.0,
                                   "carbs": 18.0, "fat": 8.0, "fiber": 3.0},
        }

    monkeypatch.setattr(srv.food_lookup, "resolve_by_barcode", hit)
    response = await srv.api_food_barcode(barcode_request({"code": "00012345678905"}))
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["calories"] == 364.0
    assert payload["serving_size"] == "1 bar (55 g)"
    assert payload["macros_per_serving"]["calories"] == 200.0

    async def miss(_code):
        return None

    monkeypatch.setattr(srv.food_lookup, "resolve_by_barcode", miss)
    response = await srv.api_food_barcode(barcode_request({"code": "737628064502"}))
    assert response.status_code == 404
    assert json.loads(response.body) == {"error": "Barcode not found in database"}


async def test_barcode_route_rejects_invalid_code(monkeypatch, lagging):
    async def unexpected(_code):
        pytest.fail("invalid barcodes must not reach OpenFoodFacts")

    monkeypatch.setattr(srv.food_lookup, "resolve_by_barcode", unexpected)
    response = await srv.api_food_barcode(barcode_request({"code": "ABC-123"}))
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "code must contain 8 to 14 digits"}


def test_library_search_uses_structured_fields_and_knee_preference():
    rows = [
        {"name": "Barbell Squat", "muscle_group": ["Quads", "Glutes"], "workout_type": "Legs", "equipment": "barbell", "difficulty": "intermediate", "swaps": []},
        {"name": "Romanian Deadlift", "muscle_group": ["Hams", "Glutes"], "workout_type": "Legs", "equipment": "barbell", "difficulty": "intermediate", "swaps": []},
        {"name": "Leg Curls", "muscle_group": ["Hams"], "workout_type": "Legs", "equipment": "machine", "difficulty": "beginner", "swaps": []},
        {"name": "Lat Pulldown", "muscle_group": ["Back"], "workout_type": "Pull", "equipment": "cable", "difficulty": "beginner", "swaps": []},
        {"name": "Reverse Fly", "muscle_group": ["Shoulders"], "workout_type": "Pull", "equipment": "dumbbell", "difficulty": "beginner", "swaps": []},
    ]

    result = srv.search_workout_library(rows, "knee friendly leg exercises")
    hams = srv.search_workout_library(rows, "hams")
    lats = srv.search_workout_library(rows, "lats")
    rear_delts = srv.search_workout_library(rows, "rear delts")

    assert {row["name"] for row in result["exercises"]} == {"Romanian Deadlift", "Leg Curls"}
    assert all(row["workout_type"] == "Legs" for row in result["exercises"])
    assert "knee-stress exercises excluded" in result["note"]
    assert {row["name"] for row in hams["exercises"]} == {"Romanian Deadlift", "Leg Curls"}
    assert [row["name"] for row in lats["exercises"]] == ["Lat Pulldown"]
    assert [row["name"] for row in rear_delts["exercises"]] == ["Reverse Fly"]


def test_library_search_combines_equipment_and_workout_type():
    rows = [
        {"name": "Bench Press", "muscle_group": ["Chest"], "workout_type": "Push", "equipment": "barbell", "difficulty": "intermediate", "swaps": []},
        {"name": "Machine Press", "muscle_group": ["Chest"], "workout_type": "Push", "equipment": "machine", "difficulty": "beginner", "swaps": []},
        {"name": "Cable Fly", "muscle_group": ["Chest"], "workout_type": "Push", "equipment": "cable", "difficulty": "beginner", "swaps": []},
        {"name": "Hack Squat", "muscle_group": ["Quads"], "workout_type": "Legs", "equipment": "machine", "difficulty": "intermediate", "swaps": []},
        {"name": "Mountain Climbers", "muscle_group": ["Cardio"], "workout_type": "Cardio", "equipment": "bodyweight", "difficulty": "beginner", "swaps": []},
        {"name": "Treadmill", "muscle_group": ["Cardio"], "workout_type": "Cardio", "equipment": "machine", "difficulty": "beginner", "swaps": []},
    ]

    commercial = srv.search_workout_library(rows, "commercial gym push")
    cardio = srv.search_workout_library(rows, "bodyweight cardio")

    assert {row["name"] for row in commercial["exercises"]} == {"Bench Press", "Machine Press"}
    assert [row["name"] for row in cardio["exercises"]] == ["Mountain Climbers"]
    assert all("matched" in row for row in commercial["exercises"] + cardio["exercises"])


def test_library_search_finds_dumbbell_bench_press_in_large_library():
    rows = [
        {"name": f"Dumbbell Exercise {index}", "muscle_group": ["Push"],
         "workout_type": "Push", "equipment": "dumbbell",
         "difficulty": "intermediate", "swaps": []}
        for index in range(900)
    ]
    rows.append({"name": "Dumbbell Bench Press", "muscle_group": ["Push"],
                 "workout_type": "Push", "equipment": "dumbbell",
                 "difficulty": "intermediate", "swaps": []})

    result = srv.search_workout_library(rows, "dumbbell bench press")

    assert result["exercises"][0]["name"] == "Dumbbell Bench Press"


async def test_api_library_returns_full_catalog(monkeypatch):
    rows = [{"id": str(index), "name": f"Exercise {index}"} for index in range(901)]

    class LibraryStore:
        async def fetch_workout_library(self):
            return rows

    monkeypatch.setattr(srv, "_client", LibraryStore())

    result = await srv.api_library.__wrapped__(None)

    assert len(result["exercises"]) > 500


def test_library_search_unknown_query_returns_full_library():
    rows = [
        {"name": f"Exercise {index}", "muscle_group": ["Chest"], "workout_type": "Push", "equipment": "machine", "difficulty": "beginner", "swaps": []}
        for index in range(52)
    ]
    rows.extend([
        {"name": "Barbell Squat", "muscle_group": ["Quads"], "workout_type": "Legs", "equipment": "barbell", "difficulty": "beginner", "swaps": []},
        {"name": "Walking Lunges", "muscle_group": ["Quads"], "workout_type": "Legs", "equipment": "bodyweight", "difficulty": "beginner", "swaps": []},
        {"name": "Stairmaster", "muscle_group": ["Cardio"], "workout_type": "Cardio", "equipment": "machine", "difficulty": "beginner", "swaps": []},
        {"name": "Leg Press", "muscle_group": ["Quads"], "workout_type": "Legs", "equipment": "machine", "difficulty": "beginner", "swaps": []},
        {"name": "Box Jump", "muscle_group": ["Quads"], "workout_type": "Cardio", "equipment": "bodyweight", "difficulty": "beginner", "swaps": []},
        {"name": "Treadmill Incline Run", "muscle_group": ["Cardio"], "workout_type": "Cardio", "equipment": "machine", "difficulty": "beginner", "swaps": []},
        {"name": "Bench Press", "muscle_group": ["Chest"], "workout_type": "Push", "equipment": "barbell", "difficulty": "beginner", "swaps": []},
        {"name": "Biceps Curls", "muscle_group": ["Biceps"], "workout_type": "Pull", "equipment": "dumbbell", "difficulty": "beginner", "swaps": []},
        {"name": "Triceps Curls", "muscle_group": ["Triceps"], "workout_type": "Push", "equipment": "dumbbell", "difficulty": "beginner", "swaps": []},
    ])

    result = srv.search_workout_library(rows, "xyzzy")
    knee_friendly = srv.search_workout_library(rows, "knee friendly")

    assert len(result["exercises"]) == 61
    assert result["note"] == "no filter matched; full library returned"
    assert len(knee_friendly["exercises"]) == 55
    assert not {
        "Barbell Squat", "Walking Lunges", "Stairmaster", "Leg Press",
        "Box Jump", "Treadmill Incline Run",
    } & {
        row["name"] for row in knee_friendly["exercises"]
    }
    assert {"Bench Press", "Biceps Curls", "Triceps Curls"} <= {
        row["name"] for row in knee_friendly["exercises"]
    }
    assert "knee-stress exercises excluded" in knee_friendly["note"]


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
