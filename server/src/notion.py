"""Notion HTTP client for API version 2025-09-03.

Raw ``httpx`` on purpose — the ``notion-client`` package lags behind the data
source endpoints this build requires.

Under 2025-09-03 a database parents one or more *data sources*, and row-level
operations moved onto the data source. Rows are queried with
``POST /v1/data_sources/{id}/query``; the pre-2025-09-03 database-level query
form no longer works and deliberately appears nowhere in this repo — a test
greps the source for it.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Iterable, Mapping

import httpx

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0

# --- Property names. Exact and case-sensitive. `Calories` has no (g) suffix. ---
P_NAME = "Name"
P_MEAL = "Meal"
P_CALORIES = "Calories"
P_PROTEIN_G = "Protein (g)"
P_CARBS_G = "Carbs (g)"
P_FAT_G = "Fat (g)"
P_DATE = "Date"

# `Macro Targets` / `Meal Presets` use unsuffixed macro names.
P_PROTEIN = "Protein"
P_CARBS = "Carbs"
P_FAT = "Fat"
P_EFFECTIVE_DATE = "Effective Date"
P_EMOJI = "Emoji"
P_SORT_ORDER = "Sort Order"
P_ACTIVE = "Active"


class NotionError(RuntimeError):
    """A Notion API failure, carrying Notion's own `message` field."""

    def __init__(self, status: int, message: str, code: str = "") -> None:
        self.status = status
        self.code = code
        super().__init__(f"Notion API {status}{f' [{code}]' if code else ''}: {message}")


# --------------------------------------------------------------------------- #
# Property helpers
# --------------------------------------------------------------------------- #


def rich_text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": value}}]


def title_prop(value: str) -> dict[str, Any]:
    return {"title": rich_text(value)}


def rich_text_prop(value: str) -> dict[str, Any]:
    return {"rich_text": rich_text(value)}


def number_prop(value: float | None) -> dict[str, Any]:
    return {"number": None if value is None else float(value)}


def select_prop(value: str | None) -> dict[str, Any]:
    return {"select": None if value is None else {"name": value}}


def date_prop(value: date | str) -> dict[str, Any]:
    iso = value.isoformat() if isinstance(value, date) else str(value)
    return {"date": {"start": iso}}


def checkbox_prop(value: bool) -> dict[str, Any]:
    return {"checkbox": bool(value)}


def paragraph_block(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text(text)},
    }


def _plain_text(items: Iterable[Mapping[str, Any]] | None) -> str:
    if not items:
        return ""
    return "".join(item.get("plain_text", "") for item in items).strip()


def read_title(page: Mapping[str, Any], name: str = P_NAME) -> str:
    prop = (page.get("properties") or {}).get(name) or {}
    return _plain_text(prop.get("title"))


def read_rich_text(page: Mapping[str, Any], name: str) -> str:
    prop = (page.get("properties") or {}).get(name) or {}
    return _plain_text(prop.get("rich_text"))


def read_number(page: Mapping[str, Any], name: str) -> float:
    prop = (page.get("properties") or {}).get(name) or {}
    value = prop.get("number")
    return 0.0 if value is None else float(value)


def read_select(page: Mapping[str, Any], name: str) -> str:
    prop = (page.get("properties") or {}).get(name) or {}
    select = prop.get("select")
    return (select or {}).get("name") or ""


def read_checkbox(page: Mapping[str, Any], name: str) -> bool:
    prop = (page.get("properties") or {}).get(name) or {}
    return bool(prop.get("checkbox"))


def read_date(page: Mapping[str, Any], name: str) -> str:
    prop = (page.get("properties") or {}).get(name) or {}
    value = prop.get("date")
    return (value or {}).get("start") or ""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class NotionClient:
    """Thin async wrapper over the Notion REST API."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        if not token:
            raise ValueError("NOTION_TOKEN is required to talk to Notion")
        self._token = token
        self._client = client
        self._owns_client = client is None

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "NotionClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            header = response.headers.get("Retry-After")
            if header:
                try:
                    return min(float(header), MAX_BACKOFF_SECONDS)
                except ValueError:
                    pass
        return min(BACKOFF_BASE_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)

    async def request(
        self, method: str, path: str, json: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Issue one Notion call, retrying 429/5xx with backoff up to 4 attempts."""
        client = await self._http()
        url = f"{NOTION_API}{path}"
        last_error: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            response: httpx.Response | None = None
            try:
                response = await client.request(
                    method, url, headers=self.headers, json=json
                )
            except httpx.HTTPError as exc:  # network-level failure
                last_error = exc
                if attempt == MAX_ATTEMPTS - 1:
                    raise NotionError(0, f"network error talking to Notion: {exc}")
                await asyncio.sleep(self._retry_delay(None, attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == MAX_ATTEMPTS - 1:
                    break
                await asyncio.sleep(self._retry_delay(response, attempt))
                continue

            if response.status_code >= 400:
                break
            return response.json() if response.content else {}

        if response is None:  # pragma: no cover - defensive
            raise NotionError(0, f"request failed: {last_error}")

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        raise NotionError(
            response.status_code,
            payload.get("message") or response.text or "no message returned",
            payload.get("code", ""),
        )

    # ----------------------------- rows ----------------------------------- #

    async def query_data_source(
        self,
        data_source_id: str,
        filter: Mapping[str, Any] | None = None,
        sorts: list[Mapping[str, Any]] | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """All rows matching `filter`, following `next_cursor` until exhausted."""
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if filter:
                body["filter"] = dict(filter)
            if sorts:
                body["sorts"] = [dict(sort) for sort in sorts]
            if cursor:
                body["start_cursor"] = cursor
            payload = await self.request(
                "POST", f"/data_sources/{data_source_id}/query", body
            )
            results.extend(payload.get("results", []))
            if not payload.get("has_more"):
                return results
            cursor = payload.get("next_cursor")
            if not cursor:
                return results

    async def create_page(
        self,
        data_source_id: str,
        properties: Mapping[str, Any],
        children: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": dict(properties),
        }
        if children:
            body["children"] = [dict(block) for block in children]
        return await self.request("POST", "/pages", body)

    async def update_page(
        self,
        page_id: str,
        properties: Mapping[str, Any] | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if properties:
            body["properties"] = dict(properties)
        if archived is not None:
            body["archived"] = archived
        return await self.request("PATCH", f"/pages/{page_id}", body)

    async def archive_page(self, page_id: str) -> dict[str, Any]:
        return await self.update_page(page_id, archived=True)

    async def append_children(
        self, page_id: str, children: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return await self.request(
            "PATCH",
            f"/blocks/{page_id}/children",
            {"children": [dict(block) for block in children]},
        )

    # -------------------------- schema / databases ------------------------- #

    async def get_database(self, database_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/databases/{database_id}")

    async def get_data_source(self, data_source_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/data_sources/{data_source_id}")

    async def primary_data_source_id(self, database_id: str) -> str:
        database = await self.get_database(database_id)
        sources = database.get("data_sources") or []
        if not sources:
            raise NotionError(
                404, f"database {database_id} has no data sources attached"
            )
        return sources[0]["id"]

    async def create_database(
        self,
        parent_page_id: str,
        title: str,
        properties: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/databases",
            {
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": rich_text(title),
                "initial_data_source": {"properties": dict(properties)},
            },
        )

    async def update_data_source(
        self, data_source_id: str, properties: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self.request(
            "PATCH", f"/data_sources/{data_source_id}", {"properties": dict(properties)}
        )

    async def search_data_sources(self, query: str = "") -> list[dict[str, Any]]:
        """Search for data sources. Note the object filter value is `data_source`."""
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "filter": {"property": "object", "value": "data_source"},
                "page_size": 100,
            }
            if query:
                body["query"] = query
            if cursor:
                body["start_cursor"] = cursor
            payload = await self.request("POST", "/search", body)
            results.extend(payload.get("results", []))
            if not payload.get("has_more") or not payload.get("next_cursor"):
                return results
            cursor = payload["next_cursor"]

    async def child_databases(self, page_id: str) -> list[dict[str, Any]]:
        """Child `child_database` blocks of a page, as `{id, title}` dicts."""
        blocks: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            suffix = f"?start_cursor={cursor}&page_size=100" if cursor else "?page_size=100"
            payload = await self.request("GET", f"/blocks/{page_id}/children{suffix}")
            for block in payload.get("results", []):
                if block.get("type") == "child_database":
                    blocks.append(
                        {
                            "id": block["id"],
                            "title": (block.get("child_database") or {}).get(
                                "title", ""
                            ),
                        }
                    )
            if not payload.get("has_more") or not payload.get("next_cursor"):
                return blocks
            cursor = payload["next_cursor"]


# --------------------------------------------------------------------------- #
# Row mapping — Notion pages in, plain dicts out
# --------------------------------------------------------------------------- #


def meal_from_page(page: Mapping[str, Any]) -> dict[str, Any]:
    """One `Nutrition Entries` row as a flat dict. Never writes to the `C` formula."""
    return {
        "id": page.get("id", ""),
        "name": read_title(page),
        "meal": read_select(page, P_MEAL) or "Snack",
        "calories": read_number(page, P_CALORIES),
        "protein": read_number(page, P_PROTEIN_G),
        "carbs": read_number(page, P_CARBS_G),
        "fat": read_number(page, P_FAT_G),
        "date": read_date(page, P_DATE),
        "created_time": page.get("created_time", ""),
    }


def target_from_page(page: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": page.get("id", ""),
        "name": read_title(page),
        "effective_date": read_date(page, P_EFFECTIVE_DATE),
        "calories": read_number(page, P_CALORIES),
        "protein": read_number(page, P_PROTEIN),
        "carbs": read_number(page, P_CARBS),
        "fat": read_number(page, P_FAT),
    }


def preset_from_page(page: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": page.get("id", ""),
        "name": read_title(page),
        "emoji": read_rich_text(page, P_EMOJI) or "🍽️",
        "calories": read_number(page, P_CALORIES),
        "protein": read_number(page, P_PROTEIN),
        "carbs": read_number(page, P_CARBS),
        "fat": read_number(page, P_FAT),
        "meal": read_select(page, P_MEAL) or "Snack",
        "sort_order": read_number(page, P_SORT_ORDER),
        "active": read_checkbox(page, P_ACTIVE),
    }


def meal_properties(
    name: str,
    meal: str,
    calories: float,
    protein: float,
    carbs: float,
    fat: float,
    day: date,
) -> dict[str, Any]:
    """Properties for a `Nutrition Entries` row. `C` is a formula and is never sent."""
    return {
        P_NAME: title_prop(name),
        P_MEAL: select_prop(meal),
        P_CALORIES: number_prop(calories),
        P_PROTEIN_G: number_prop(protein),
        P_CARBS_G: number_prop(carbs),
        P_FAT_G: number_prop(fat),
        P_DATE: date_prop(day),
    }


def date_equals_filter(day: date) -> dict[str, Any]:
    return {"property": P_DATE, "date": {"equals": day.isoformat()}}


def date_range_filter(start: date, end: date) -> dict[str, Any]:
    return {
        "and": [
            {"property": P_DATE, "date": {"on_or_after": start.isoformat()}},
            {"property": P_DATE, "date": {"on_or_before": end.isoformat()}},
        ]
    }
