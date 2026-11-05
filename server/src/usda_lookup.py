"""Whole-food lane: USDA FoodData Central (Foundation, SR Legacy, FNDDS).

One bounded search per query. Rows are CC0 public domain, so the result is
``cache_allowed`` and may be stored permanently. Branded Foods are never
searched here: brands and chains belong to the FatSecret lane.

Selection is code, not the model: every query word must appear in the
description, fewer extra words win (``Egg, whole, raw`` beats ``Egg, duck,
whole, fresh, raw`` for "egg"), and FNDDS rows (as-eaten, with household
portions such as "1 egg") win ties over SR Legacy, then Foundation.
A row with carbohydrate but no reported fiber is skipped: unknown is never 0.
"""
from __future__ import annotations

import logging
import os
import re
import time
from copy import deepcopy
from typing import Any, Awaitable, Callable

import httpx

from http_clients import shared_client

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
DATA_TYPES = "Foundation,SR Legacy,Survey (FNDDS)"
TIMEOUT_SECONDS = 4.0
PAGE_SIZE = 12
CACHE_TTL_SECONDS = 6 * 3600
_CACHE_MAX = 512
_RANK = {"Survey (FNDDS)": 0, "SR Legacy": 1, "Foundation": 2}
_NUTRIENTS = {1008: "calories", 1003: "protein", 1005: "carbs", 1004: "fat", 1079: "fiber"}
_STOP = frozenset({"a", "an", "the", "of", "and", "with", "plain", "some"})
_COOKED = frozenset({"cooked", "fried", "boiled", "scrambled", "poached", "baked", "roasted", "grilled",
                     "toasted", "steamed", "braised", "stewed", "broiled", "sauteed"})

PostFn = Callable[[dict[str, Any]], Awaitable[httpx.Response]]
_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def api_key() -> str:
    return os.environ.get("USDA_API_KEY", "").strip()


async def post_search(params: dict[str, Any]) -> httpx.Response:
    client = shared_client(TIMEOUT_SECONDS)
    return await client.get(SEARCH_URL, params=params, headers={"X-Api-Key": api_key()})


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("es") and token[-3] in "sxzh":
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us")):
        return token[:-1]
    return token


def _tokens(text: Any) -> set[str]:
    words = re.findall(r"[a-z0-9]+", str(text or "").casefold().replace("'", ""))
    return {_stem(w) for w in words if w not in _STOP}


def _nutrients(food: dict[str, Any]) -> dict[str, float | None]:
    out: dict[str, float | None] = {key: None for key in _NUTRIENTS.values()}
    for item in food.get("foodNutrients") or []:
        key = _NUTRIENTS.get(item.get("nutrientId"))
        value = item.get("value")
        if key and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            out[key] = round(float(value), 2)
    return out


def _portion(food: dict[str, Any]) -> tuple[str, float] | None:
    """The first household measure with a gram weight ("1 egg", 50 g)."""
    for measure in food.get("foodMeasures") or []:
        text = str(measure.get("disseminationText") or "").strip()
        grams = measure.get("gramWeight")
        if not text or text.casefold().startswith("quantity not") or not isinstance(grams, (int, float)):
            continue
        if grams <= 0:
            continue
        return text, float(grams)
    return None


def _score(query_tokens: set[str], food: dict[str, Any]) -> tuple[int, int, int] | None:
    desc = _tokens(food.get("description"))
    if not query_tokens <= desc:
        return None
    return (len(desc - query_tokens), _RANK.get(str(food.get("dataType")), 9), 0)


def select(query: str, foods: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pure: the best row for ``query`` or ``None``."""
    wanted = _tokens(query)
    if not wanted:
        return None
    best: tuple[tuple[int, int, int], dict[str, Any]] | None = None
    for food in foods:
        score = _score(wanted, food)
        if score is None:
            continue
        nutrients = _nutrients(food)
        if nutrients["calories"] is None:
            continue
        if (nutrients["carbs"] or 0) > 0 and nutrients["fiber"] is None:
            continue  # unknown fiber is never 0
        if best is None or score < best[0]:
            best = (score, food)
    return best[1] if best else None


def to_result(food: dict[str, Any]) -> dict[str, Any]:
    """Resolver-shaped dict (``food_lookup.resolve_food`` contract) for one FDC row."""
    fdc_id = str(food.get("fdcId"))
    description = " ".join(str(food.get("description") or "").split())
    per_100g = _nutrients(food)
    basis = "cooked" if _tokens(description) & _COOKED else "raw"
    portion = _portion(food)
    if portion:
        text, grams = portion
        factor = grams / 100.0
        serving = {k: (None if v is None else round(v * factor, 2)) for k, v in per_100g.items()}
        serving_size = f"{text} ({grams:g} g)"
        serving_grams = grams
    else:
        serving = dict(per_100g)
        serving_size = "100 g"
        serving_grams = 100.0
    data_type = str(food.get("dataType") or "FDC")
    return {
        "name": description, "basis": basis,
        "macros_per_100g": per_100g, "macros_per_serving": serving, "serving_size": serving_size,
        "source": f"USDA FDC {data_type}: {fdc_id}, per 100 g",
        "carbohydrate_definition": "by_difference",
        "attribution": {
            "provider": "USDA FDC", "external_id": fdc_id, "data_type": data_type,
            "verification_state": "official_source_exact_row", "cache_allowed": True,
            "serving_basis": "per_serving", "serving_grams": serving_grams,
            "source_url": f"https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients",
            "license": "CC0 1.0 (public domain)",
        },
    }


def _remember(key: str, value: dict[str, Any] | None) -> None:
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, value)


async def lookup(query: str, *, post: PostFn | None = None) -> dict[str, Any] | None:
    """One whole-food lookup; ``None`` when unconfigured, unavailable or no exact row."""
    text = " ".join(str(query or "").split())
    if not text or len(text) > 120 or not api_key():
        return None
    key = text.casefold()
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return deepcopy(cached[1])
    params = {"query": text, "dataType": DATA_TYPES, "pageSize": PAGE_SIZE, "requireAllWords": "true"}
    try:
        response = await (post or post_search)(params)
        if response.status_code != 200:
            logger.info("usda lookup: http %s", response.status_code)
            return None
        body = response.json()
    except (httpx.HTTPError, OSError, TimeoutError, ValueError) as exc:
        logger.info("usda lookup unavailable: %s", type(exc).__name__)
        return None
    foods = body.get("foods") if isinstance(body, dict) else None
    chosen = select(text, foods or [])
    result = to_result(chosen) if chosen else None
    _remember(key, result)
    return deepcopy(result)


def clear_cache() -> None:
    _cache.clear()


__all__ = ["lookup", "select", "to_result", "clear_cache", "api_key"]
