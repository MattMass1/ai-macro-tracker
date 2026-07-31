"""Shape tests for the Notion client. Every request is mocked — no network."""

from datetime import date
from pathlib import Path

import httpx
import pytest

import notion as notion_api
from notion import (
    NotionClient,
    NotionError,
    date_equals_filter,
    date_range_filter,
    meal_from_page,
    meal_properties,
    preset_from_page,
    target_from_page,
    workout_from_page,
    workout_properties,
)

SRC_DIR = Path(__file__).resolve().parent.parent / "src"


def make_client(handler) -> tuple[NotionClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    http = httpx.AsyncClient(transport=transport)
    return NotionClient("secret_test", client=http), seen


def json_response(payload, status=200, headers=None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers or {})


# --------------------------------------------------------------------------- #
# Headers and versioning
# --------------------------------------------------------------------------- #


async def test_every_request_sends_the_pinned_api_version():
    client, seen = make_client(lambda request: json_response({"results": []}))
    await client.query_data_source("ds-1")
    request = seen[0]
    assert request.headers["Notion-Version"] == "2025-09-03"
    assert request.headers["Authorization"] == "Bearer secret_test"
    assert request.headers["Content-Type"] == "application/json"


def test_client_requires_a_token():
    with pytest.raises(ValueError):
        NotionClient("")


# --------------------------------------------------------------------------- #
# Endpoint shapes (2025-09-03)
# --------------------------------------------------------------------------- #


async def test_query_uses_the_data_source_endpoint_and_paginates():
    pages = [
        {"results": [{"id": "a"}], "has_more": True, "next_cursor": "cur-2"},
        {"results": [{"id": "b"}], "has_more": False, "next_cursor": None},
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = pages[calls["n"]]
        calls["n"] += 1
        return json_response(payload)

    client, seen = make_client(handler)
    rows = await client.query_data_source(
        "ds-1", filter=date_equals_filter(date(2026, 7, 25))
    )

    assert [row["id"] for row in rows] == ["a", "b"]
    assert len(seen) == 2
    for request in seen:
        assert request.method == "POST"
        assert request.url.path == "/v1/data_sources/ds-1/query"
    import json

    assert json.loads(seen[0].content)["filter"] == {
        "property": "Date",
        "date": {"equals": "2026-07-25"},
    }
    assert json.loads(seen[1].content)["start_cursor"] == "cur-2"


async def test_create_page_parents_a_data_source():
    import json

    client, seen = make_client(lambda request: json_response({"id": "page-1"}))
    await client.create_page(
        "ds-1",
        meal_properties("Chicken", "Dinner", 300, 55, 0, 7, date(2026, 7, 25)),
        children=[notion_api.paragraph_block("Macro source: FDA FoodData Central")],
    )
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/pages"
    body = json.loads(request.content)
    assert body["parent"] == {"type": "data_source_id", "data_source_id": "ds-1"}
    assert body["properties"]["Calories"] == {"number": 300.0}
    assert body["properties"]["Protein (g)"] == {"number": 55.0}
    assert "C" not in body["properties"]
    assert body["children"][0]["type"] == "paragraph"


async def test_archive_page_patches_the_page():
    import json

    client, seen = make_client(lambda request: json_response({"id": "page-1"}))
    await client.archive_page("page-1")
    request = seen[0]
    assert request.method == "PATCH"
    assert request.url.path == "/v1/pages/page-1"
    assert json.loads(request.content) == {"archived": True}


async def test_create_database_puts_properties_under_initial_data_source():
    import json

    client, seen = make_client(lambda request: json_response({"id": "db-1"}))
    await client.create_database("page-1", "Macro Targets", {"Name": {"title": {}}})
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/databases"
    assert body["parent"] == {"type": "page_id", "page_id": "page-1"}
    assert body["initial_data_source"]["properties"] == {"Name": {"title": {}}}


async def test_update_data_source_patches_the_data_source():
    client, seen = make_client(lambda request: json_response({"id": "ds-1"}))
    await client.update_data_source("ds-1", {"Meal": {"select": {"options": []}}})
    assert seen[0].method == "PATCH"
    assert seen[0].url.path == "/v1/data_sources/ds-1"


async def test_primary_data_source_id():
    client, _ = make_client(
        lambda request: json_response({"data_sources": [{"id": "ds-9", "name": "x"}]})
    )
    assert await client.primary_data_source_id("db-1") == "ds-9"


async def test_primary_data_source_id_errors_when_none_attached():
    client, _ = make_client(lambda request: json_response({"data_sources": []}))
    with pytest.raises(NotionError):
        await client.primary_data_source_id("db-1")


async def test_search_filters_on_data_source_not_database():
    import json

    client, seen = make_client(
        lambda request: json_response({"results": [], "has_more": False})
    )
    await client.search_data_sources("Macro Targets")
    body = json.loads(seen[0].content)
    assert body["filter"] == {"property": "object", "value": "data_source"}


# --------------------------------------------------------------------------- #
# Retries and errors
# --------------------------------------------------------------------------- #


async def test_retries_429_honoring_retry_after(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(notion_api.asyncio, "sleep", fake_sleep)
    responses = [
        json_response({"message": "rate limited"}, 429, {"Retry-After": "2"}),
        json_response({"results": [], "has_more": False}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client, seen = make_client(handler)
    assert await client.query_data_source("ds-1") == []
    assert len(seen) == 2
    assert slept == [2.0]


async def test_retries_5xx_then_gives_up_with_the_notion_message(monkeypatch):
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(notion_api.asyncio, "sleep", fake_sleep)
    client, seen = make_client(
        lambda request: json_response({"message": "internal error"}, 502)
    )
    with pytest.raises(NotionError) as excinfo:
        await client.query_data_source("ds-1")
    assert len(seen) == 4  # capped at 4 attempts
    assert "internal error" in str(excinfo.value)


async def test_client_errors_are_not_retried_and_surface_the_message():
    client, seen = make_client(
        lambda request: json_response(
            {"message": "Calories is not a property that exists.", "code": "validation_error"},
            400,
        )
    )
    with pytest.raises(NotionError) as excinfo:
        await client.create_page("ds-1", {})
    assert len(seen) == 1
    assert "Calories is not a property that exists." in str(excinfo.value)
    assert "validation_error" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Property mapping
# --------------------------------------------------------------------------- #


MEAL_PAGE = {
    "id": "35a5aac7-7d31-81bb-84e9-cc9338611008",
    "created_time": "2026-07-25T14:22:00.000Z",
    "properties": {
        "Name": {"title": [{"plain_text": "Fairlife Core Power 30g shake"}]},
        "Meal": {"select": {"name": "Snack"}},
        "Calories": {"number": 150},
        "Protein (g)": {"number": 30},
        "Carbs (g)": {"number": None},
        "Fat (g)": {"number": 2.5},
        "Date": {"date": {"start": "2026-07-25"}},
        "C": {"formula": {"type": "number", "number": 150}},
    },
}


def test_meal_from_page_coerces_null_numbers_to_zero():
    meal = meal_from_page(MEAL_PAGE)
    assert meal == {
        "id": "35a5aac7-7d31-81bb-84e9-cc9338611008",
        "name": "Fairlife Core Power 30g shake",
        "meal": "Snack",
        "calories": 150.0,
        "protein": 30.0,
        "carbs": 0.0,
        "fat": 2.5,
        "date": "2026-07-25",
        "created_time": "2026-07-25T14:22:00.000Z",
    }


def test_meal_from_page_tolerates_an_empty_row():
    meal = meal_from_page({"id": "x", "properties": {}})
    assert meal["calories"] == 0.0
    assert meal["meal"] == "Snack"
    assert meal["name"] == ""


def test_meal_properties_never_writes_the_formula_column():
    props = meal_properties("Eggs", "Breakfast", 140, 12, 1, 10, date(2026, 7, 25))
    assert set(props) == {
        "Name",
        "Meal",
        "Calories",
        "Protein (g)",
        "Carbs (g)",
        "Fat (g)",
        "Date",
    }
    assert props["Date"] == {"date": {"start": "2026-07-25"}}


def test_target_and_preset_mapping():
    target = target_from_page(
        {
            "id": "t1",
            "properties": {
                "Name": {"title": [{"plain_text": "Initial"}]},
                "Effective Date": {"date": {"start": "2026-01-01"}},
                "Calories": {"number": 2400},
                "Protein": {"number": 215},
                "Carbs": {"number": 200},
                "Fat": {"number": 70},
            },
        }
    )
    assert target["calories"] == 2400.0 and target["protein"] == 215.0
    assert target["effective_date"] == "2026-01-01"

    preset = preset_from_page(
        {
            "id": "p1",
            "properties": {
                "Name": {"title": [{"plain_text": "Usual dinner"}]},
                "Emoji": {"rich_text": [{"plain_text": "🍗"}]},
                "Calories": {"number": 620},
                "Protein": {"number": 68},
                "Carbs": {"number": 45},
                "Fat": {"number": 18},
                "Meal": {"select": {"name": "Dinner"}},
                "Sort Order": {"number": 1},
                "Active": {"checkbox": True},
            },
        }
    )
    assert preset["emoji"] == "🍗" and preset["active"] is True
    assert preset["sort_order"] == 1.0


def test_preset_falls_back_to_a_default_emoji():
    assert preset_from_page({"id": "p", "properties": {}})["emoji"] == "🍽️"


def test_date_range_filter_is_inclusive_of_both_ends():
    assert date_range_filter(date(2026, 7, 1), date(2026, 7, 7)) == {
        "and": [
            {"property": "Date", "date": {"on_or_after": "2026-07-01"}},
            {"property": "Date", "date": {"on_or_before": "2026-07-07"}},
        ]
    }


def test_workout_properties_builds_sets_and_tags():
    props = workout_properties(
        "Barbell Bench Press",
        "Push",
        ["Push"],
        [{"weight": 225, "reps": 5}, {"weight": 185, "reps": 8}],
        date(2026, 7, 31),
    )
    assert props["Exercise Name"] == {
        "title": [{"type": "text", "text": {"content": "Barbell Bench Press"}}]
    }
    assert props["Workout type"] == {"multi_select": [{"name": "Push"}]}
    assert props["Muscle Group"] == {"multi_select": [{"name": "Push"}]}
    assert props["Date (user input)"] == {"date": {"start": "2026-07-31"}}
    assert props["Weight 1"] == {"number": 225.0}
    assert props["Reps 1"] == {"number": 5.0}
    assert props["Weight 2"] == {"number": 185.0}
    assert props["Reps 2"] == {"number": 8.0}
    # Unused set slots are simply absent — never written as null.
    assert "Weight 3" not in props and "Reps 4" not in props


def test_workout_from_page_reads_sets_and_tags():
    workout = workout_from_page(
        {
            "id": "w1",
            "created_time": "2026-07-31T14:00:00.000Z",
            "properties": {
                "Exercise Name": {"title": [{"plain_text": "Hack Squats"}]},
                "Workout type": {"multi_select": [{"name": "Legs"}]},
                "Muscle Group": {"multi_select": [{"name": "Quads"}]},
                "Weight 1": {"number": 90},
                "Reps 1": {"number": 10},
                "Weight 2": {"number": 140},
                "Reps 2": {"number": 8},
                "Weight 3": {"number": None},
                "Reps 3": {"number": None},
                "Date (user input)": {"date": {"start": "2026-07-31"}},
            },
        }
    )
    assert workout["exercise"] == "Hack Squats"
    assert workout["workout_type"] == ["Legs"]
    assert workout["muscle_group"] == ["Quads"]
    assert workout["sets"] == [
        {"weight": 90.0, "reps": 10.0},
        {"weight": 140.0, "reps": 8.0},
    ]
    assert workout["date"] == "2026-07-31"


# --------------------------------------------------------------------------- #
# Guardrails against the pre-2025-09-03 API shape
# --------------------------------------------------------------------------- #


def test_no_source_file_uses_the_legacy_database_query_endpoint():
    import re

    legacy = re.compile(r"/databases/\{?[^/\s\"']*\}?/query")
    for path in list(SRC_DIR.glob("*.py")) + [SRC_DIR.parent.parent / "setup.py"]:
        assert not legacy.search(path.read_text(encoding="utf-8")), path


def test_no_source_file_mentions_the_abandoned_daily_meals_database():
    for path in list(SRC_DIR.glob("*.py")) + [SRC_DIR.parent.parent / "setup.py"]:
        text = path.read_text(encoding="utf-8")
        assert "c967e104" not in text, path
        assert "Daily Meals" not in text, path
