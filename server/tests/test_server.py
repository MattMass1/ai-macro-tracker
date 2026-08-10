"""Service-level tests against a deliberately lagging fake Notion.

Notion's query index is eventually consistent: a row created a moment ago is
often absent from the next query, and an archived row often still appears. These
tests pin the reconciliation that keeps a write's response honest anyway.
"""

import json
import os

import httpx
import pytest

os.environ.setdefault("NOTION_TOKEN", "secret_test")
os.environ.setdefault("NUTRITION_DS_ID", "ds-nutrition")
os.environ.setdefault("TARGETS_DS_ID", "ds-targets")
os.environ.setdefault("PRESETS_DS_ID", "ds-presets")
os.environ.setdefault("APP_SHARED_TOKEN", "test-token")

import notion as notion_api  # noqa: E402
import server as srv  # noqa: E402

TODAY = srv.domain.effective_date().isoformat()


def meal_page(page_id, name, meal, cal, protein, carbs, fat, created):
    return {
        "id": page_id,
        "created_time": created,
        "properties": {
            "Name": {"title": [{"plain_text": name}]},
            "Meal": {"select": {"name": meal}},
            "Calories": {"number": cal},
            "Protein (g)": {"number": protein},
            "Carbs (g)": {"number": carbs},
            "Fat (g)": {"number": fat},
            "Date": {"date": {"start": TODAY}},
        },
    }


TARGET_PAGE = {
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


class LaggingNotion:
    """Serves queries from a snapshot that ignores the most recent write."""

    def __init__(self, meals):
        self.visible = list(meals)
        self.created: list[dict] = []
        self.archived: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path.endswith("/query"):
            if "ds-nutrition" in path:
                # The lag: creations are invisible, archives still show up.
                return httpx.Response(
                    200, json={"results": self.visible, "has_more": False}
                )
            if "ds-targets" in path:
                return httpx.Response(
                    200, json={"results": [TARGET_PAGE], "has_more": False}
                )
            return httpx.Response(200, json={"results": [], "has_more": False})
        if path == "/v1/pages":
            self.created.append(body)
            return httpx.Response(
                200, json={"id": "new-page", "created_time": "2026-07-25T20:00:00.000Z"}
            )
        if request.method == "PATCH" and path.startswith("/v1/pages/"):
            self.archived.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})
        return httpx.Response(200, json={})


@pytest.fixture
def lagging(monkeypatch):
    fake = LaggingNotion(
        [meal_page("meal-1", "Eggs", "Breakfast", 280, 28, 2, 18, "2026-07-25T12:00:00.000Z")]
    )
    client = notion_api.NotionClient(
        "secret_test", client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )
    monkeypatch.setattr(srv, "_client", client)
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

    assert [meal["id"] for meal in payload["meals"]] == ["new-page", "meal-1"]
    assert payload["totals"] == {
        "calories": 430.0,
        "protein": 58.0,
        "carbs": 5.0,
        "fat": 20.5,
    }
    assert payload["remaining"]["protein"] == 157.0
    assert payload["logged"]["macro_source"] == "Fairlife Core Power label"
    assert "warning" not in payload


async def test_log_meal_does_not_double_count_once_the_query_catches_up(lagging):
    lagging.visible.append(
        meal_page("new-page", "Core Power shake", "Snack", 150, 30, 3, 2.5, "2026-07-25T20:00:00.000Z")
    )
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
    assert payload["logged"]["id"] == "new-page"  # still written


async def test_undo_excludes_the_row_it_just_archived(lagging):
    payload = await srv.undo_last_meal()
    assert lagging.archived == ["meal-1"]
    assert payload["removed"]["name"] == "Eggs"
    assert payload["meals"] == []
    assert payload["totals"] == {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
    assert payload["remaining"]["protein"] == 215.0


async def test_undo_with_nothing_logged_is_an_actionable_error(monkeypatch):
    empty = LaggingNotion([])
    monkeypatch.setattr(
        srv,
        "_client",
        notion_api.NotionClient(
            "secret_test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(empty.handler)),
        ),
    )
    with pytest.raises(Exception) as excinfo:
        await srv.undo_last_meal()
    assert "nothing to undo" in str(excinfo.value)


async def test_write_sends_exact_property_names_and_a_source_paragraph(lagging):
    await srv.write_meal(
        name="Chicken",
        calories=300,
        protein=55,
        carbs=0,
        fat=7,
        macro_source="FDA FoodData Central: chicken breast, roasted",
        meal="dinner",
        day_value="2026-07-20",
    )
    body = lagging.created[-1]
    assert body["parent"] == {
        "type": "data_source_id",
        "data_source_id": "ds-nutrition",
    }
    assert set(body["properties"]) == {
        "Name",
        "Meal",
        "Calories",
        "Protein (g)",
        "Carbs (g)",
        "Fat (g)",
        "Date",
    }
    assert body["properties"]["Meal"] == {"select": {"name": "Dinner"}}
    assert body["properties"]["Date"] == {"date": {"start": "2026-07-20"}}
    paragraph = body["children"][0]["paragraph"]["rich_text"][0]["text"]["content"]
    assert paragraph == "Macro source: FDA FoodData Central: chicken breast, roasted"


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
    assert captured["payload"]["model"] == "gpt-5.6-luna"
    assert captured["payload"]["messages"][1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,AAAA"},
    }
    assert result["name"] == "Chicken bowl"
    assert result["meal"] == "Lunch"
