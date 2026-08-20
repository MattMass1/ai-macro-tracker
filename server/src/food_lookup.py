"""Free structured food-database lookups: USDA FoodData Central, then OpenFoodFacts.

The cascade resolves unknown foods through free databases before any paid LLM
path spends credits on lookup. Every call is a read-only GET with a short
timeout, and every failure degrades to None so a lookup can never take down
chat. Future work: cache resolved foods to avoid repeat calls.
"""
from __future__ import annotations

import math
import os
from typing import Any, Mapping

import httpx

TIMEOUT = 5.0
# OpenFoodFacts requires an identifying User-Agent; the default python-httpx
# one gets rejected/throttled.
USER_AGENT = "MacroCoach/1.0 (ai-macro-tracker; contact@biz21.com)"
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
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


async def resolve_food(query: str) -> dict[str, Any] | None:
    """The cascade: USDA FDC first, then OpenFoodFacts. None when both miss."""
    return await search_usda(query) or await search_openfoodfacts(query)


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
