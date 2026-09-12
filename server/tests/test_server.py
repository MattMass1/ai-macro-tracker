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
from datetime import date
from decimal import Decimal
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
        self, *, name, meal, calories, protein, carbs, fat, fiber, day, macro_source,
        component_metadata=None
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


def log_request(payload, token="device-token", idempotency_key=None):
    request = barcode_request(payload, token)
    headers = list(request.scope["headers"])
    if idempotency_key is not None:
        headers.append((b"idempotency-key", idempotency_key.encode()))
    request.scope["path"] = "/api/log"
    request.scope["headers"] = headers
    return request


def get_request(path, token=None):
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type":"http", "method":"GET", "path":path,
                    "headers":headers, "query_string":b""})


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


async def test_write_meal_rejects_calories_without_macros(lagging):
    with pytest.raises(srv.MacroError, match="data-integrity failure"):
        await srv.write_meal(
            name="chicken",
            calories=410,
            protein=0,
            carbs=0,
            fat=0,
            macro_source="Food parser",
            meal="Dinner",
            day_value=None,
        )

    assert lagging.inserted == []


async def test_write_meal_allows_zero_macro_food(lagging):
    payload = await srv.write_meal(
        name="vodka soda",
        calories=100,
        protein=0,
        carbs=0,
        fat=0,
        macro_source="Drink label",
        meal="Snack",
        day_value=None,
    )

    assert payload["logged"]["name"] == "vodka soda"
    assert lagging.inserted[-1]["calories"] == 100.0


async def test_write_meal_still_allows_calories_with_macros(lagging):
    payload = await srv.write_meal(
        name="chicken",
        calories=410,
        protein=45,
        carbs=2,
        fat=18,
        macro_source="Food parser",
        meal="Dinner",
        day_value=None,
    )

    assert payload["logged"]["protein"] == 45.0
    assert len(lagging.inserted) == 1


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
            "attribution": {"provider":"OpenFoodFacts", "license":"ODbL",
                            "attribution_text":"Data from OpenFoodFacts"},
        }

    monkeypatch.setattr(srv.food_lookup, "resolve_by_barcode", hit)
    response = await srv.api_food_barcode(barcode_request({"code": "00012345678905"}))
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["calories"] == 364.0
    assert payload["serving_size"] == "1 bar (55 g)"
    assert payload["macros_per_serving"]["calories"] == 200.0
    assert payload["attribution"]["license"] == "ODbL"

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


async def test_readiness_is_authenticated_reports_status_and_leaks_no_secret(monkeypatch):
    class ReadyStore:
        async def resolve_device(self, token): return uuid4() if token == "device-token" else None
        async def migration_readiness(self):
            return {"database":"connected", "migrations":{"compatible":False,
                "pending":["001_food_catalog.sql"], "drift":[], "unknown":[]}}
    monkeypatch.setattr(srv, "_client", ReadyStore())
    unauthenticated = await srv.readiness(get_request("/api/readiness"))
    assert unauthenticated.status_code == 401
    response = await srv.readiness(get_request("/api/readiness", "device-token"))
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["status"] == "not_ready"
    assert payload["database"] == "connected"
    serialized = response.body.decode()
    assert "postgresql://" not in serialized and "device-token" not in serialized


async def test_api_log_optional_idempotency_key_and_conflict_status(monkeypatch):
    class IdempotentStore:
        async def resolve_device(self, token): return uuid4()
        async def insert_meal_idempotent(self, key, request_hash, **values):
            if key == "conflict":
                raise srv.IdempotencyConflict("Idempotency key was already used for a different request")
            assert key == "retry-key" and len(request_hash) == 64
            assert values["name"] == "Banana"
            return {"logged":{"id":"one", "name":"Banana"}, "date":values["day"].isoformat()}
    monkeypatch.setattr(srv, "_client", IdempotentStore())
    body = {"name":"Banana","calories":105,"protein":1,"carbs":27,"fat":0,
            "fiber":3,"macro_source":"Known food: Banana","meal":"Snack"}
    response = await srv.api_log(log_request(body, idempotency_key="retry-key"))
    assert response.status_code == 200
    assert json.loads(response.body)["logged"]["id"] == "one"
    conflict = await srv.api_log(log_request(body, idempotency_key="conflict"))
    assert conflict.status_code == 409


async def test_api_log_without_idempotency_header_remains_backward_compatible(lagging):
    body = {"name":"Banana","calories":105,"protein":1,"carbs":27,"fat":0,
            "fiber":3,"macro_source":"Known food: Banana","meal":"Snack"}
    response = await srv.api_log(log_request(body))
    assert response.status_code == 200
    assert json.loads(response.body)["logged"]["name"] == "Banana"


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


async def test_idempotent_meal_response_survives_decimal_row_values():
    """asyncpg returns Postgres NUMERIC columns as decimal.Decimal. The
    response builder (including the Atwater cross-check it runs on the raw
    insert-returning row) must not raise `float * Decimal` when it sees them.
    """
    day = date(2026, 9, 12)

    class FakeConn:
        async def fetch(self, sql, *args):
            if "FROM nutrition_entries" in sql:
                return [{
                    "id": "row-1",
                    "name": "6 oz ground beef (93/7, estimated) + 100 g sweet potato",
                    "meal": "Snack", "calories": Decimal("446"),
                    "protein": Decimal("40.6"), "carbs": Decimal("20.1"),
                    "fat": Decimal("21.1"), "fiber": Decimal("3.0"),
                    "day": day, "created_at": "2026-09-12T20:00:00+00:00",
                }]
            if "FROM meals" in sql:
                return [{
                    "id": "meal-1", "day": day, "meal_type": "Snack",
                    "calories": Decimal("446"), "protein": Decimal("40.6"),
                    "carbs": Decimal("20.1"), "fat": Decimal("21.1"),
                    "fiber": Decimal("3.0"),
                }]
            raise AssertionError(f"unexpected fetch: {sql}")

        async def fetchrow(self, sql, *args):
            if "FROM days" in sql:
                return {
                    "date": day, "calories": Decimal("446"),
                    "protein": Decimal("40.6"), "carbs": Decimal("20.1"),
                    "fat": Decimal("21.1"), "fiber": Decimal("3.0"),
                }
            if "FROM macro_targets" in sql:
                return {
                    "calories": Decimal("2400"), "protein": Decimal("180"),
                    "carbs": Decimal("200"), "fat": Decimal("70"),
                    "fiber": Decimal("30"),
                }
            raise AssertionError(f"unexpected fetchrow: {sql}")

    row = {
        "id": "row-1",
        "name": "6 oz ground beef (93/7, estimated) + 100 g sweet potato",
        "meal": "Snack",
        "calories": Decimal("446"), "protein": Decimal("40.6"),
        "carbs": Decimal("20.1"), "fat": Decimal("21.1"), "fiber": Decimal("3.0"),
        "day": day, "created_at": "2026-09-12T20:00:00+00:00",
        "macro_source": "ESTIMATE — 6 oz 93/7 ground beef, typical values; "
                        "FatSecret: 36617 — 100 g sweet potato",
    }

    payload = await srv._idempotent_meal_response(FakeConn(), uuid4(), row)

    assert payload["logged"]["calories"] == 446.0
    assert isinstance(payload["logged"]["calories"], float)
    assert payload["totals"] == {
        "calories": 446.0, "protein": 40.6, "carbs": 20.1,
        "fat": 21.1, "fiber": 3.0,
    }
    assert payload["remaining"]["calories"] == 2400.0 - 446.0
    assert payload["day_rollup"]["calories"] == 446.0
    assert isinstance(payload["day_rollup"]["calories"], float)
    assert payload["day_label"] == srv.domain.day_label(day)
    # Within Atwater tolerance for these numbers, so no warning is raised.
    assert "warning" not in payload


async def test_voice_meal_response_is_lean_and_carries_the_spoken_confirmation():
    """The voice path never needs the full day snapshot `_idempotent_meal_response`
    builds for /api/log's dashboard — one row from `days` is enough for the
    logged item plus the running total, and the confirmation sentence it
    builds is what the delegated model relays instead of composing numbers.
    """
    day = date(2026, 9, 12)
    queries: list[str] = []

    class FakeConn:
        async def fetchrow(self, sql, *args):
            queries.append(sql)
            assert "FROM days" in sql
            return {
                "calories": Decimal("791"), "protein": Decimal("87.6"),
                "carbs": Decimal("44.1"), "fat": Decimal("30.1"), "fiber": Decimal("6.0"),
            }

    row = {
        "id": "row-1", "name": "6 oz 93/7 ground beef", "meal": "Dinner",
        "calories": Decimal("446"), "protein": Decimal("40.6"),
        "carbs": Decimal("20.1"), "fat": Decimal("21.1"), "fiber": Decimal("3.0"),
        "day": day, "created_at": "2026-09-12T20:00:00+00:00",
        "macro_source": "FatSecret: ground beef 93/7",
    }

    payload = await srv._voice_meal_response(FakeConn(), uuid4(), row)

    assert queries == ["SELECT calories,protein,carbs,fat,fiber FROM days WHERE user_id=$1 AND date=$2"]
    assert payload["logged"]["name"] == "6 oz 93/7 ground beef"
    assert payload["logged"]["calories"] == 446.0
    assert payload["logged"]["macro_source"] == "FatSecret: ground beef 93/7"
    assert payload["day_total"] == {
        "calories": 791.0, "protein": 87.6, "carbs": 44.1, "fat": 30.1, "fiber": 6.0,
    }
    confirmation = payload["confirmation"]
    assert "6 oz 93/7 ground beef" in confirmation
    assert "446 kcal" in confirmation and "41g protein" in confirmation
    assert "FatSecret: ground beef 93/7" in confirmation
    assert "791 kcal" in confirmation and "88g protein" in confirmation


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
        macro_source="OpenFoodFacts: chicken breast, roasted",
        meal="dinner",
        day_value="2026-07-20",
    )
    row = lagging.inserted[-1]
    assert row["name"] == "Chicken"
    assert row["meal"] == "Dinner"
    assert row["date"] == "2026-07-20"
    assert row["macro_source"] == "OpenFoodFacts: chicken breast, roasted"
    assert row["calories"] == 300.0 and row["protein"] == 55.0 and row["fat"] == 7.0


async def test_fetch_workouts_history_normalizes_filter_and_caps_results(monkeypatch):
    class WorkoutStore:
        async def fetch_workouts(self, start=None, end=None, exercise=None):
            assert start is None and end is None
            assert exercise == "Bench Press"
            return [{"id": str(index)} for index in range(4)]

    monkeypatch.setattr(srv, "_client", WorkoutStore())

    result = await srv.fetch_workouts_history("  Bench Press  ", 2)

    assert result == [{"id": "0"}, {"id": "1"}]


async def test_fetch_trends_builds_daily_weekly_and_weight_payload(monkeypatch):
    class TrendsPool:
        async def fetch(self, query, user_id, start, end):
            assert "LEFT JOIN LATERAL" in query
            assert user_id == "user-1"
            assert start <= end
            return [
                {"date": date(2026, 8, 23), "calories": Decimal("2055"),
                 "protein": Decimal("199"), "carbs": Decimal("207"),
                 "fat": Decimal("58"), "target_calories": Decimal("2400"),
                 "target_protein": Decimal("180")},
                {"date": date(2026, 8, 24), "calories": Decimal("1746"),
                 "protein": Decimal("141.4"), "carbs": Decimal("190"),
                 "fat": Decimal("49"), "target_calories": Decimal("2400"),
                 "target_protein": Decimal("180")},
            ]

        async def fetchrow(self, query, user_id):
            assert "user_metrics" in query and user_id == "user-1"
            return {"weight_kg": Decimal("88.5"), "goal_weight_kg": Decimal("80")}

    class TrendsStore:
        async def connect(self):
            return TrendsPool()

    monkeypatch.setattr(srv, "_client", TrendsStore())
    monkeypatch.setattr(srv, "current_user_id", lambda: "user-1")

    result = await srv.fetch_trends(30)

    assert result["days"][0] == {
        "date": "2026-08-23",
        "day_label": "Sunday, August 23",
        "calories": 2055.0,
        "protein": 199.0,
        "carbs": 207.0,
        "fat": 58.0,
        "target_calories": 2400.0,
        "target_protein": 180.0,
    }
    assert result["weekly"] == [
        {"week_start": "2026-08-17", "avg_calories": 2055.0,
         "avg_protein": 199.0, "days_logged": 1},
        {"week_start": "2026-08-24", "avg_calories": 1746.0,
         "avg_protein": 141.4, "days_logged": 1},
    ]
    assert result["weight"] == {"current_kg": 88.5, "goal_kg": 80.0}


# --------------------------------------------------------------------------- #
# INFO logging configured once for both entrypoints, no sensitive payloads
# (voice-first-utterance-fix-20260912)
# --------------------------------------------------------------------------- #

def test_configure_logging_sets_info_level_exactly_once(monkeypatch):
    import logging as logging_module

    monkeypatch.setattr(srv, "_logging_configured", False)
    root = logging_module.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    try:
        root.setLevel(logging_module.WARNING)
        srv.configure_logging()
        assert root.level == logging_module.INFO
        assert srv._logging_configured is True
        handler_count = len(root.handlers)
        # A second call (e.g. a second create_app()) must not reconfigure —
        # no duplicate handlers, no re-running basicConfig.
        root.setLevel(logging_module.WARNING)
        srv.configure_logging()
        assert root.level == logging_module.WARNING  # untouched: the guard skipped it
        assert len(root.handlers) == handler_count
    finally:
        root.setLevel(original_level)
        root.handlers[:] = original_handlers


def test_create_app_configures_logging(monkeypatch):
    calls = []
    monkeypatch.setattr(srv, "configure_logging", lambda: calls.append(1))
    monkeypatch.setattr(srv, "_client", FakeStore([]))
    srv.create_app()
    assert calls == [1]


def test_voice_tool_call_logs_never_include_transcript_or_credential_markers(caplog):
    """The brief forbids logging transcripts, meal text, audio, tokens, or
    credentials — only ids, tool names, states, timings, and counts. This
    pins the actual log line shape for the one call site most likely to leak
    the user's spoken food text: a successful voice tool call."""
    import logging as logging_module

    caplog.set_level(logging_module.INFO, logger="live_coach")
    import live_coach

    async def run():
        async def handler(_call_id, _args):
            return {"logged": {"calories": 100}}

        return await live_coach.dispatch_voice_tool_call(
            {"call_id": "call-1", "name": "log_meal",
             "arguments": '{"name": "6 oz ground beef", "calories": 255}'},
            tool_handlers={"log_meal": handler},
            allowed_names=frozenset({"log_meal"}),
        )

    import asyncio
    asyncio.run(run())
    messages = [record.message for record in caplog.records]
    joined = " ".join(messages)
    assert "log_meal" in joined
    assert "call-1" in joined
    assert "outcome=ok" in joined
    assert "ground beef" not in joined.casefold()
    assert "255" not in joined
    assert "100" not in joined
    assert "Bearer" not in joined
    assert "sk-" not in joined
    assert "audio" not in joined.casefold()
