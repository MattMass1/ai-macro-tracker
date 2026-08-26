"""Food lookups: OpenFoodFacts, then Tavily search.

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
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
MACRO_KEYS = ("calories", "protein", "carbs", "fat", "fiber")
FOOD_CLASSES = {"whole", "branded", "restaurant", "unknown"}

_OFF_NUTRIMENTS = {"energy-kcal_100g": "calories", "proteins_100g": "protein",
                   "carbohydrates_100g": "carbs", "fat_100g": "fat", "fiber_100g": "fiber"}
_OFF_SERVING_NUTRIMENTS = {
    "energy-kcal_serving": "calories",
    "proteins_serving": "protein",
    "carbohydrates_serving": "carbs",
    "fat_serving": "fat",
    "fiber_serving": "fiber",
}


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


async def classify_food(name: str) -> str:
    """Classify a food for cascade ordering, degrading safely to unknown."""
    token = os.environ.get("OPENAI_ACCESS_TOKEN", "").strip()
    text = str(name or "").strip()
    if not token or not text:
        return "unknown"
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Classify the food submission by its best nutrition source. "
                    "Reply with exactly one word: whole, branded, restaurant, "
                    "or unknown. Whole means an unbranded basic food; branded "
                    "means a packaged grocery product; restaurant means a menu "
                    "item or restaurant chain."
                ),
            },
            {"role": "user", "content": text},
        ],
        "max_tokens": 10,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.post(
                OPENAI_CHAT_URL,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        classification = str(content).strip().casefold()
        return classification if classification in FOOD_CLASSES else "unknown"
    except Exception:
        return "unknown"


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


async def search_openfoodfacts(query: str) -> dict[str, Any] | None:
    """Best OpenFoodFacts match (branded/packaged foods, no key), or None."""
    text = (query or "").strip()
    if not text:
        return None
    try:
        data = await _get_json(OFF_SEARCH_URL, {
            "search_terms": text, "search_simple": 1,
            "action": "process", "json": 1, "page_size": 5,
        })
        for product in (data.get("products") or [])[:5]:
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
                result: dict[str, Any] = {
                    "name": name,
                    "macros_per_100g": macros,
                    "source": f"OpenFoodFacts: {code}",
                }
                serving_size = str(product.get("serving_size") or "").strip()
                serving_macros = _macros({
                    key: nutriments.get(field)
                    for field, key in _OFF_SERVING_NUTRIMENTS.items()
                })
                if serving_size:
                    result["serving_size"] = serving_size
                if serving_macros is not None:
                    result["macros_per_serving"] = serving_macros
                return result
    except Exception:
        return None
    return None


async def search_openfoodfacts_by_code(barcode: str) -> dict[str, Any] | None:
    """Return an exact OpenFoodFacts barcode match, or None on any failure."""
    code = str(barcode or "").strip()
    if not code:
        return None
    try:
        data = await _get_json(OFF_PRODUCT_URL.format(barcode=code), {})
        if not isinstance(data, Mapping) or data.get("status") != 1:
            return None
        product = data.get("product")
        if not isinstance(product, Mapping):
            return None
        nutriments = product.get("nutriments")
        if not isinstance(nutriments, Mapping):
            return None
        macros = _macros({
            key: nutriments.get(field) for field, key in _OFF_NUTRIMENTS.items()
        })
        name = str(product.get("product_name") or "").strip()
        if macros is None or not name:
            return None
        result: dict[str, Any] = {
            "name": name,
            "macros_per_100g": macros,
            "source": f"OpenFoodFacts barcode: {code}",
        }
        serving_size = str(product.get("serving_size") or "").strip()
        serving_macros = _macros({
            key: nutriments.get(field)
            for field, key in _OFF_SERVING_NUTRIMENTS.items()
        })
        if serving_size:
            result["serving_size"] = serving_size
        if serving_macros is not None:
            result["macros_per_serving"] = serving_macros
        return result
    except Exception:
        return None


async def resolve_by_barcode(code: str) -> dict[str, Any] | None:
    """Resolve a barcode separately from the text-search cascade."""
    return await search_openfoodfacts_by_code(code)


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
# "Serving size 1 bar (55g), Calories 200..." and label panels that lead with
# a unit cue instead — "Per slice: Calories 60..." — are both serving columns.
# The (?!\d) keeps "per 100g"-style weight cues on the _TAVILY_BASIS path.
_US_SERVING = re.compile(
    r"\b(?:serving\s*(?:size\s*)?(?::|[-=])?|per\s+(?!\d))\s*"
    r"(?P<serving>.{1,60}?)\s*[,;.:]?\s*(?=calories\b)",
    re.IGNORECASE,
)
_GRAM_WEIGHT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:g\b|grams?\b)", re.IGNORECASE)
_QUERY_GRAMS = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:g\b|grams?\b)", re.IGNORECASE)
_WHOLE_SERVING_QUERY = re.compile(
    r"\b(?:one|a|an)\b|\b1(?![0-9]|[/.,])",
    re.IGNORECASE,
)

_PLAIN_CAL = re.compile(r"\b(\d{2,4})\s*k?cal(?:ories?)?\b", re.IGNORECASE)
_PLAIN_PROTEIN = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*g\s+protein\b", re.IGNORECASE)
_PLAIN_CARBS = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*g\s+(?:carbs?|net carbs)\b", re.IGNORECASE)
_PLAIN_FAT = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*g\s+fat\b", re.IGNORECASE)


def _plain_macro_line(text: str) -> dict[str, float] | None:
    """Extract a whole-serving macro set from a plain line like
    '650 cal | 43g protein | 46g fat'. Returns None unless calories and at
    least one other macro are present."""
    cal = _PLAIN_CAL.search(text)
    if cal is None:
        return None
    protein = _PLAIN_PROTEIN.search(text)
    carbs = _PLAIN_CARBS.search(text)
    fat = _PLAIN_FAT.search(text)
    values: dict[str, float] = {"calories": float(cal.group(1))}
    if protein:
        values["protein"] = float(protein.group(1))
    if carbs:
        values["carbs"] = float(carbs.group(1))
    if fat:
        values["fat"] = float(fat.group(1))
    if len(values) < 2:  # calories alone is not enough to trust
        return None
    values.setdefault("protein", 0.0)
    values.setdefault("carbs", 0.0)
    values.setdefault("fat", 0.0)
    return values


def is_whole_serving_query(query: str) -> bool:
    """Whether the user explicitly requested one whole item or serving."""
    return _WHOLE_SERVING_QUERY.search(str(query or "")) is not None


def _tavily_panels(text: str) -> list[tuple[float, dict[str, float]]] | None:
    """Parse nutrient panels with calories required and missing macros zeroed."""
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
        if values["calories"] is None:
            return None
        panels.append((weight, {
            key: value if value is not None else 0.0
            for key, value in values.items()
        }))
    return panels


def _panels_agree(left: Mapping[str, float], right: Mapping[str, float]) -> bool:
    """Allow ordinary label rounding, but reject materially conflicting columns."""
    return all(
        math.isclose(left[key], right[key], rel_tol=0.10, abs_tol=1.0)
        for key in MACRO_KEYS
    )


def _us_serving_panel(text: str) -> tuple[str, float | None, dict[str, float]] | None:
    """Parse a serving panel with calories required and missing macros zeroed."""
    cue = _US_SERVING.search(text)
    if cue is None:
        return None
    serving = cue.group("serving").strip()
    next_basis = _TAVILY_BASIS.search(text, cue.end())
    block = text[cue.end():next_basis.start() if next_basis else len(text)]
    values = {key: _nutrition_value(block, key) for key in MACRO_KEYS}
    if values["calories"] is None:
        return None
    complete_values = {
        key: value if value is not None else 0.0
        for key, value in values.items()
    }
    weights = _GRAM_WEIGHT.findall(serving)
    if len(weights) > 1:
        return None
    weight = float(weights[0]) if weights else None
    if weight is not None and not 0 < weight <= 5000:
        return None
    return serving, weight, complete_values


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
        # Plain-format fallback: "650 cal | 43g protein | 46g fat" (no per-100g
        # or serving-size cue). Treat as one whole-serving macro line.
        plain = _plain_macro_line(text)
        if plain is not None:
            url = str(result.get("url") or "").strip()
            source_ref = url or title
            if not source_ref:
                return None
            return {
                "name": title or query,
                "macros_per_serving": plain,
                "basis": "serving",
                "source": f"Tavily: {source_ref}",
            }
        return None

    url = str(result.get("url") or "").strip()
    source_ref = url or title
    if not source_ref:
        return None

    if serving_panel is not None and serving_panel[1] is None:
        serving, _weight, values = serving_panel
        # A gram request cannot use a weightless serving, and an explicit
        # numeric quantity other than 1 (1/2, 1.5, 2) can't be sized from one
        # serving without inventing a conversion. A query with no stated
        # quantity means one serving — never reject it for lacking a weight.
        if _QUERY_GRAMS.search(query):
            return None
        if re.search(r"\d", query) and not _WHOLE_SERVING_QUERY.search(query):
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

    parsed: dict[str, Any] = {
        "name": title or query,
        "macros_per_100g": macros,
        "source": f"Tavily: {source_ref}",
    }
    if serving_panel is not None:
        serving, _serving_weight, serving_values = serving_panel
        serving_macros = _macros(serving_values)
        if serving_macros is not None:
            parsed["serving_size"] = serving
            parsed["macros_per_serving"] = serving_macros
    return parsed


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


def _query_variants(query: str) -> list[str]:
    """Fallback query ladder for branded names the sources miss verbatim.

    "L'oven Fresh Cinnamon Raisin Bread" tries itself, then the phrase with
    one and two leading brand words dropped, then the brand alone, then the
    trailing generic food word — so a brand+flavor entry degrades to a close
    generic match instead of resolving to nothing.
    """
    text = " ".join(str(query or "").split())
    if not text:
        return []
    words = text.split()
    candidates = [text]
    if len(words) >= 3:
        candidates.append(" ".join(words[1:]))
        candidates.append(" ".join(words[2:]))
        candidates.append(" ".join(words[:2]))
        if len(words[-1]) >= 4:
            candidates.append(words[-1])
    variants: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.casefold()
        if candidate and key not in seen:
            seen.add(key)
            variants.append(candidate)
    return variants


async def resolve_food(
    query: str, classify: bool = True, *, whole_item: bool = False
) -> dict[str, Any] | None:
    """Resolve food with a classified first tier and a complete fallback cascade."""
    searches = [search_openfoodfacts, search_tavily]
    if classify:
        try:
            classification = await classify_food(query)
        except Exception:
            classification = "unknown"
        preferred = {
            "whole": search_openfoodfacts,
            "branded": search_openfoodfacts,
            "restaurant": search_tavily,
        }.get(classification)
        if preferred is not None:
            searches = [preferred] + [search for search in searches if search is not preferred]
    for variant in _query_variants(query):
        for search in searches:
            search_query = f"1 {variant}" if whole_item and search is search_tavily else variant
            found = await search(search_query)
            if found:
                if whole_item:
                    serving = found.get("macros_per_serving")
                    if not isinstance(serving, Mapping) or _macros(serving) is None:
                        continue
                return found
    return None


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


def portion_from_serving(
    found: Mapping[str, Any],
) -> tuple[dict[str, float], str] | None:
    """Return an explicitly sourced one-serving panel without inferring weight."""
    values = found.get("macros_per_serving") if isinstance(found, Mapping) else None
    if not isinstance(values, Mapping):
        return None
    macros = _macros(values)
    if macros is None:
        return None
    source = str(found.get("source") or "").strip()
    if not source:
        return None
    return macros, source
