"""Food lookups: USDA FoodData Central, OpenFoodFacts, then Tavily search.

The cascade resolves unknown foods through free databases before any paid LLM
path spends credits on lookup. Every call is read-only with a short
timeout, and every failure degrades to None so a lookup can never take down
chat. Future work: cache resolved foods to avoid repeat calls.
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Mapping

import httpx

TIMEOUT = 5.0
# OpenFoodFacts requires an identifying User-Agent; the default python-httpx
# one gets rejected/throttled.
USER_AGENT = "MacroCoach/1.0 (ai-macro-tracker; contact@biz21.com)"
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
MACRO_KEYS = ("calories", "protein", "carbs", "fat", "fiber")

# FDC nutrient numbers -> macro keys. Foundation and SR Legacy report per 100 g.
_FDC_NUTRIENTS = {1008: "calories", 1003: "protein", 1005: "carbs", 1004: "fat", 1079: "fiber"}
_OFF_NUTRIMENTS = {"energy-kcal_100g": "calories", "proteins_100g": "protein",
                   "carbohydrates_100g": "carbs", "fat_100g": "fat", "fiber_100g": "fiber"}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})


async def _get_json(url: str, params: dict[str, Any]) -> Any:
    async with _client() as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def _post_json(url: str, payload: dict[str, Any]) -> Any:
    async with _client() as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


def _macros(values: Mapping[str, Any]) -> dict[str, float] | None:
    """Coerce one parsed nutrient set to per-100g macros; calories are mandatory."""
    macros: dict[str, float] = {}
    for key in MACRO_KEYS:
        raw = values.get(key)
        if raw is None:
            if key == "calories":
                return None
            raw = 0
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0 or number > 1000:
            return None
        macros[key] = round(number, 2)
    if macros["calories"] <= 0:
        return None
    return macros


async def search_usda(query: str) -> dict[str, Any] | None:
    """Best USDA FoodData Central match, or None (also when no key is configured)."""
    api_key = os.environ.get("USDA_API_KEY", "").strip()
    text = (query or "").strip()
    if not api_key or not text:
        return None
    try:
        data = await _get_json(USDA_SEARCH_URL, {
            "query": text, "pageSize": 3,
            "dataType": "Foundation,SR Legacy", "api_key": api_key,
        })
        for food in (data.get("foods") or [])[:3]:
            if not isinstance(food, dict):
                continue
            values: dict[str, Any] = {}
            for nutrient in food.get("foodNutrients") or []:
                if not isinstance(nutrient, dict):
                    continue
                key = _FDC_NUTRIENTS.get(nutrient.get("nutrientId"))
                if key and key not in values:
                    values[key] = nutrient.get("value")
            macros = _macros(values)
            name = str(food.get("description") or "").strip()
            fdc_id = str(food.get("fdcId") or "").strip()
            if macros and name and fdc_id:
                return {"name": name, "macros_per_100g": macros,
                        "source": f"USDA FDC: {fdc_id}"}
    except Exception:
        return None
    return None


async def search_openfoodfacts(query: str) -> dict[str, Any] | None:
    """Best OpenFoodFacts match (branded/packaged foods, no key), or None."""
    text = (query or "").strip()
    if not text:
        return None
    try:
        data = await _get_json(OFF_SEARCH_URL, {
            "search_terms": text, "search_simple": 1,
            "action": "process", "json": 1, "page_size": 3,
        })
        for product in (data.get("products") or [])[:3]:
            if not isinstance(product, dict):
                continue
            nutriments = product.get("nutriments") or {}
            if not isinstance(nutriments, Mapping):
                continue
            macros = _macros({key: nutriments.get(field)
                              for field, key in _OFF_NUTRIMENTS.items()})
            name = str(product.get("product_name") or "").strip()
            code = str(product.get("code") or "").strip()
            if macros and name and code:
                return {"name": name, "macros_per_100g": macros,
                        "source": f"OpenFoodFacts: {code}"}
    except Exception:
        return None
    return None


def _nutrition_value(text: str, label: str) -> float | None:
    """Extract a nutrient value only when it is explicitly labelled in text."""
    labels = r"(?:carbs?|carbohydrates?)" if label == "carbs" else re.escape(label)
    trailing_unit = r"(?:kcal|calories?)?" if label == "calories" else r"g(?:rams?)?"
    leading_unit = r"(?:kcal|calories?)" if label == "calories" else r"g(?:rams?)?"
    patterns = (
        rf"\b{labels}\b\s*(?:[:=-]|is|of)?\s*(\d+(?:\.\d+)?)\s*{trailing_unit}\b",
        rf"\b(\d+(?:\.\d+)?)\s*{leading_unit}\s+(?:of\s+)?{labels}\b",
    )
    # Prefer the conventional label-first form. A value-first regex can otherwise
    # steal the preceding nutrient's value in compact text such as "20g Carbs: 35g".
    matches = list(re.finditer(patterns[0], text, flags=re.IGNORECASE))
    if not matches:
        matches = list(re.finditer(patterns[1], text, flags=re.IGNORECASE))
    if len(matches) != 1:
        return None
    try:
        return float(matches[0].group(1))
    except ValueError:
        return None


_TAVILY_BASIS = re.compile(
    r"\bper\s+(\d+(?:\.\d+)?)\s*(?:g\b|grams?\b)[^:]{0,80}:\s*",
    re.IGNORECASE,
)
_US_SERVING = re.compile(
    r"\bserving\s*(?:size\s*)?(?::|[-=])?\s*"
    r"(?P<serving>.+?)\s*[,;.]?\s*(?=calories\b)",
    re.IGNORECASE,
)
_GRAM_WEIGHT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:g\b|grams?\b)", re.IGNORECASE)
_QUERY_GRAMS = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:g\b|grams?\b)", re.IGNORECASE)
_WHOLE_SERVING_QUERY = re.compile(
    r"\b(?:one|a|an)\b|\b1(?![0-9]|[/.,])",
    re.IGNORECASE,
)


def _tavily_panels(text: str) -> list[tuple[float, dict[str, float]]] | None:
    """Parse complete nutrient panels, keeping every value with its own basis."""
    cues = list(_TAVILY_BASIS.finditer(text))
    if not cues:
        return None

    panels: list[tuple[float, dict[str, float]]] = []
    for index, cue in enumerate(cues):
        weight = float(cue.group(1))
        if not 0 < weight <= 5000:
            return None
        end = cues[index + 1].start() if index + 1 < len(cues) else len(text)
        block = text[cue.end():end]
        values = {key: _nutrition_value(block, key) for key in MACRO_KEYS}
        if any(value is None for value in values.values()):
            return None
        panels.append((weight, values))  # type: ignore[arg-type]
    return panels


def _panels_agree(left: Mapping[str, float], right: Mapping[str, float]) -> bool:
    """Allow ordinary label rounding, but reject materially conflicting columns."""
    return all(
        math.isclose(left[key], right[key], rel_tol=0.10, abs_tol=1.0)
        for key in MACRO_KEYS
    )


def _us_serving_panel(text: str) -> tuple[str, float | None, dict[str, float]] | None:
    """Parse a US single-column serving label and its following nutrient values."""
    cue = _US_SERVING.search(text)
    if cue is None:
        return None
    serving = cue.group("serving").strip()
    next_basis = _TAVILY_BASIS.search(text, cue.end())
    block = text[cue.end():next_basis.start() if next_basis else len(text)]
    values = {key: _nutrition_value(block, key) for key in MACRO_KEYS}
    if any(value is None for value in values.values()):
        return None
    weights = _GRAM_WEIGHT.findall(serving)
    if len(weights) > 1:
        return None
    weight = float(weights[0]) if weights else None
    if weight is not None and not 0 < weight <= 5000:
        return None
    return serving, weight, values  # type: ignore[return-value]


def _tavily_result(query: str, result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Parse one Tavily snippet, rejecting incomplete or unrelated nutrition text."""
    title = str(result.get("title") or "").strip()
    content = str(result.get("content") or "").strip()
    text = f"{title} {content}".strip()
    query_words = {
        word
        for word in re.findall(r"[a-z0-9]{3,}", query.casefold())
        if word not in {"log", "one"}
    }
    searchable = text.casefold()
    matched_words = sum(word in searchable for word in query_words)
    if (
        not text
        or not query_words
        or matched_words < math.ceil(len(query_words) / 2)
    ):
        return None

    serving_panel = _us_serving_panel(text)
    panels = _tavily_panels(text)
    if serving_panel is None and panels is None:
        return None

    url = str(result.get("url") or "").strip()
    source_ref = url or title
    if not source_ref:
        return None

    if serving_panel is not None and serving_panel[1] is None:
        serving, _weight, values = serving_panel
        # A gram request cannot use a weightless serving. Only explicitly whole-item
        # queries may consume these values without inventing a conversion.
        if _QUERY_GRAMS.search(query) or not _WHOLE_SERVING_QUERY.search(query):
            return None
        macros = _macros(values)
        if macros is None:
            return None
        return {
            "name": title or query,
            "macros_per_serving": macros,
            "basis": "serving",
            "serving": serving,
            "source": f"Tavily: {source_ref}",
        }

    all_panels = list(panels or [])
    if serving_panel is not None:
        _serving, serving_weight, serving_values = serving_panel
        assert serving_weight is not None
        all_panels.insert(0, (serving_weight, serving_values))

    normalized: list[tuple[float, dict[str, float]]] = []
    for basis_grams, values in all_panels:
        macros = _macros({
            key: value / (basis_grams / 100.0)
            for key, value in values.items()
        })
        if macros is None:
            return None
        normalized.append((basis_grams, macros))

    item_panels = [panel for panel in normalized if panel[0] != 100.0]
    hundred_panels = [panel for panel in normalized if panel[0] == 100.0]
    # A US serving cue plus an equivalent legacy "per N g" cue can describe the
    # same column. More than one distinct item column remains ambiguous.
    if len(item_panels) > 1 and not all(
        panel[0] == item_panels[0][0]
        and _panels_agree(panel[1], item_panels[0][1])
        for panel in item_panels[1:]
    ):
        return None
    if len(hundred_panels) > 1:
        return None
    if item_panels and hundred_panels and not _panels_agree(
        item_panels[0][1], hundred_panels[0][1]
    ):
        return None
    # Prefer the explicitly labelled item column when both representations agree.
    macros = (item_panels or hundred_panels)[0][1]

    return {
        "name": title or query,
        "macros_per_100g": macros,
        "source": f"Tavily: {source_ref}",
    }


async def search_tavily(query: str) -> dict[str, Any] | None:
    """Best conservatively parsed Tavily nutrition result, or None."""
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    text = (query or "").strip()
    if not api_key or not text:
        return None
    try:
        data = await _post_json(TAVILY_SEARCH_URL, {
            "api_key": api_key,
            "query": text,
            "search_depth": "basic",
            "max_results": 3,
            "include_answer": False,
        })
        for result in (data.get("results") or [])[:3]:
            if isinstance(result, Mapping):
                parsed = _tavily_result(text, result)
                if parsed:
                    return parsed
    except Exception:
        return None
    return None


async def resolve_food(query: str) -> dict[str, Any] | None:
    """The cascade: USDA FDC, OpenFoodFacts, then Tavily. None on a total miss."""
    return (
        await search_usda(query)
        or await search_openfoodfacts(query)
        or await search_tavily(query)
    )


def portion_from_grams(
    found: Mapping[str, Any], grams: Any
) -> tuple[dict[str, float], str] | None:
    """Scale a per-100g database hit to a user-stated gram portion.

    Only a portion the user actually gave can size the entry; scaling by the
    parser's estimated calories would be circular. Returns (portion macros,
    macro_source) or None when the grams are unusable.
    """
    per100 = found.get("macros_per_100g") if isinstance(found, Mapping) else None
    if not isinstance(per100, Mapping):
        return None
    try:
        weight = float(grams)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(weight) or weight <= 0 or weight > 5000:  # >5 kg is not a meal
        return None
    factor = weight / 100.0
    macros = {key: round(float(per100.get(key, 0)) * factor, 2) for key in MACRO_KEYS}
    if macros["calories"] <= 0:
        return None
    source = f"{found['source']} — {found['name']}, {round(weight)} g"
    return macros, source
