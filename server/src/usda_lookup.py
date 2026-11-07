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
import math
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
_ENERGY_FALLBACKS = (2048, 2047)  # Atwater specific, then general: Foundation search rows often lack 1008
_STOP = frozenset({"a", "an", "the", "of", "and", "with", "plain", "some"})
_COOKED = frozenset({"cooked", "fried", "boiled", "scrambled", "poached", "baked", "roasted", "grilled",
                     "toasted", "steamed", "braised", "stewed", "broiled", "sauteed", "rotisserie",
                     "microwaved", "breaded", "battered", "smoked"})
# A row that adds a preparation, sauce, or product form the user did not say is a
# different food, not a closer spelling: these count heavily against it.
_FORM_WORDS = _COOKED | frozenset({"sauce", "gravy", "breading", "batter", "lunchmeat", "roll", "tenders",
                                   "nuggets", "patty", "sausage", "frozen", "canned", "dried", "powder",
                                   "substitute", "pasteurized", "glutinous", "mix", "prepared", "fast",
                                   "restaurant", "babyfood", "infant", "formula"})

PostFn = Callable[[dict[str, Any]], Awaitable[httpx.Response]]
_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_rows_cache: dict[tuple[str, bool], tuple[float, list[dict[str, Any]]]] = {}


def api_key() -> str:
    return os.environ.get("USDA_API_KEY", "").strip()


async def post_search(params: dict[str, Any]) -> httpx.Response:
    """POST JSON: the GET form of this endpoint rejects some ordinary query strings (nginx 400)."""
    client = shared_client(TIMEOUT_SECONDS)
    body = {**params, "dataType": str(params.get("dataType") or DATA_TYPES).split(","),
            "requireAllWords": str(params.get("requireAllWords", "true")).casefold() == "true"}
    return await client.post(SEARCH_URL, json=body, headers={"X-Api-Key": api_key()})


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
    energy: dict[int, float] = {}
    for item in food.get("foodNutrients") or []:
        value = item.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            continue
        key = _NUTRIENTS.get(item.get("nutrientId"))
        if key:
            out[key] = round(float(value), 2)
        elif item.get("nutrientId") in _ENERGY_FALLBACKS:
            energy[item["nutrientId"]] = float(value)
    if out["calories"] is None:
        for nutrient_id in _ENERGY_FALLBACKS:
            if nutrient_id in energy:
                out["calories"] = round(energy[nutrient_id], 2)
                break
    return out


def _portion(food: dict[str, Any], requested: str = "") -> tuple[str, float] | None:
    """Choose a compatible, unambiguous household measure, never the first row.

    Unspecified size may use an explicitly regular item, not thin/mini/large.
    Different weights for equally compatible measures require a weight from the user.
    """
    identity = _tokens(food.get("description"))
    requested_tokens = _tokens(requested) - {
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
        "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand",
        "half", "quarter", "third", "dozen", "point",
    }
    neutral = {"regular", "whole", "plain"}
    candidates = []
    for measure in food.get("foodMeasures") or []:
        text = str(measure.get("disseminationText") or "").strip()
        grams = measure.get("gramWeight")
        if not text or not isinstance(grams, (int, float)) or isinstance(grams, bool):
            continue
        if not math.isfinite(grams) or grams <= 0 or not re.match(r"^1(?:\.0+)?\s", text):
            continue
        tokens = _tokens(text) - {"1", "0"}
        if tokens - identity - requested_tokens - neutral:
            continue
        # Requested size/measure words must be present, not merely allowed.
        qualifiers = requested_tokens - identity - neutral - {t for t in requested_tokens if t.isdigit()}
        if not qualifiers <= tokens:
            continue
        candidates.append((text, float(grams)))
    if not candidates and "piece" in requested_tokens:
        # A "piece" of toast or bread is the row's slice when no measure says piece.
        return _portion(food, re.sub(r"\bpieces?\b", "slice", requested, flags=re.IGNORECASE))
    if not candidates or len({grams for _, grams in candidates}) != 1:
        return None
    return min(candidates, key=lambda item: item[0].casefold())


def compatible(query: str, description: str) -> bool:
    """Authoritative numbers do not make a different recipe the requested food."""
    wanted, shown = _tokens(query), _tokens(description)
    if not wanted or not wanted <= shown:
        return False
    # Only descriptive words that do not introduce a preparation, recipe,
    # species, flavor or ratio may be added. Unlisted differences fail closed.
    neutral = {"whole", "fresh", "plain", "raw", "boneless", "skinless", "meat", "only",
               "broiler", "fryer"}
    if "egg" in wanted:
        neutral.add("chicken")
    if wanted & _COOKED:
        neutral.discard("raw")
    return not (shown - wanted - neutral)


def for_portion(found: dict[str, Any], query: str, text: str, kind: str) -> dict[str, Any]:
    """Rebind source measures for the requested amount; mass stays per 100 g."""
    food = {"description": query, "foodMeasures": found.get("usda_measures") or []}
    selected = None if kind == "mass" else _portion(food, text)
    out = dict(found)
    out["attribution"] = dict(found.get("attribution") or {})
    if selected:
        label, grams = selected
        out["serving_size"] = f"{label} ({grams:g} g)"
        out["macros_per_serving"] = {k: None if v is None else round(v * grams / 100, 2)
                                    for k, v in found["macros_per_100g"].items()}
        out["attribution"].update(serving_basis="per_serving", serving_grams=grams)
    else:
        out.pop("macros_per_serving", None)
        out["serving_size"] = "100 g"
        out["attribution"].update(serving_basis="per_100g", serving_grams=100.0)
    return out


def _score(query_tokens: set[str], food: dict[str, Any]) -> tuple[int, int, int] | None:
    """Lower is better: (unasked form words x3 + other extra words, raw-vs-asked, source rank).

    A query with no cooking word prefers the raw / as-sold row (the app's
    raw-or-cooked question covers weighed foods); a query that names one
    prefers rows that name it too.
    """
    desc = _tokens(food.get("description"))
    if not compatible(" ".join(query_tokens), str(food.get("description") or "")):
        return None
    extra = desc - query_tokens
    forms = extra & _FORM_WORDS
    asked_cooked = bool(query_tokens & _COOKED)
    row_cooked = bool(desc & _COOKED)
    state = 0 if asked_cooked == row_cooked else 1
    return (3 * len(forms) + len(extra - forms), state, _RANK.get(str(food.get("dataType")), 9))


def select(query: str, foods: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pure: the best row for ``query`` or ``None``."""
    wanted = _tokens(query)
    if not wanted:
        return None
    best: tuple[tuple[int, int, int], dict[str, Any]] | None = None
    for food in foods:
        if not isinstance(food, dict) or food.get("dataType") not in _RANK:
            continue
        if not isinstance(food.get("fdcId"), int) or isinstance(food.get("fdcId"), bool):
            continue
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
        "usda_measures": deepcopy(food.get("foodMeasures") or []),
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


async def _search(text: str, *, require_all: bool = True, post: PostFn | None = None) -> list[dict[str, Any]] | None:
    """One FDC search, remembered so an exact lookup and a closest-row search share the wire call.

    ``None`` means unavailable, which is never remembered. An empty list is a real answer.
    """
    key = (text.casefold(), require_all)
    cached = _rows_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return deepcopy(cached[1])
    params = {"query": text, "dataType": DATA_TYPES, "pageSize": PAGE_SIZE,
              "requireAllWords": "true" if require_all else "false"}
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
    found = [food for food in foods or [] if isinstance(food, dict)]
    if len(_rows_cache) >= _CACHE_MAX:
        _rows_cache.pop(next(iter(_rows_cache)))
    _rows_cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, found)
    return deepcopy(found)


async def rows(query: str, *, require_all: bool = True, post: PostFn | None = None) -> list[dict[str, Any]]:
    """The raw FDC rows for ``query``, for callers that rank them themselves."""
    text = " ".join(str(query or "").split())
    if not text or len(text) > 120 or not api_key():
        return []
    return await _search(text, require_all=require_all, post=post) or []


async def lookup(query: str, *, post: PostFn | None = None) -> dict[str, Any] | None:
    """One whole-food lookup; ``None`` when unconfigured, unavailable or no exact row."""
    text = " ".join(str(query or "").split())
    if not text or len(text) > 120 or not api_key():
        return None
    key = text.casefold()
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return deepcopy(cached[1])
    foods = await _search(text, post=post)
    if foods is None:
        return None
    chosen = select(text, foods)
    result = to_result(chosen) if chosen else None
    _remember(key, result)
    return deepcopy(result)


def clear_cache() -> None:
    _cache.clear()
    _rows_cache.clear()


__all__ = ["lookup", "rows", "select", "to_result", "clear_cache", "api_key"]
