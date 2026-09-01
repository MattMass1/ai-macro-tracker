"""Catalog, curated menu, FatSecret, and OpenFoodFacts food lookup."""
from __future__ import annotations

import asyncio, base64, hashlib, hmac, json, math, os, re, secrets, time
from typing import Any, Callable, Mapping
from urllib.parse import quote
import httpx
from food_catalog import normalize_food_name
from restaurant_menu import restaurant_lookup

TIMEOUT = 5.0
USER_AGENT = "MacroCoach/1.0 (ai-macro-tracker; contact@biz21.com)"
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
FATSECRET_TOKEN_URL = "https://oauth.fatsecret.com/connect/token"
FATSECRET_API_URL = "https://platform.fatsecret.com/rest/server.api"
OFF_LICENSE_NAME = "Open Database License (ODbL) 1.0"
OFF_LICENSE_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
MACRO_KEYS = ("calories", "protein", "carbs", "fat", "fiber")
_OFF_NUTRIMENTS = {"energy-kcal_100g":"calories", "proteins_100g":"protein",
    "carbohydrates_100g":"carbs", "fat_100g":"fat", "fiber_100g":"fiber"}
_OFF_SERVING = {"energy-kcal_serving":"calories", "proteins_serving":"protein",
    "carbohydrates_serving":"carbs", "fat_serving":"fat", "fiber_serving":"fiber"}
_TOKEN: tuple[str, float, str, str] | None = None


def _rfc3986(value: Any) -> str:
    """Percent-encode an OAuth value according to RFC 3986."""
    return quote(str(value), safe="~-._")


def _fatsecret_provider_mode() -> str | None:
    """Return the configured FatSecret auth mode, preferring Basic OAuth1."""
    if os.environ.get("FATSECRET_ATTRIBUTION_ENABLED", "false").strip().casefold() != "true":
        return None
    consumer = tuple(os.environ.get(key, "").strip() for key in
        ("FATSECRET_CONSUMER_KEY", "FATSECRET_CONSUMER_SECRET"))
    premier = tuple(os.environ.get(key, "").strip() for key in
        ("FATSECRET_CLIENT_ID", "FATSECRET_CLIENT_SECRET"))
    if all(consumer):
        return "oauth1"
    if all(premier):
        return "oauth2"
    return None


def _fatsecret_oauth1_data(
    data: Mapping[str, Any], consumer_key: str, consumer_secret: str, *,
    nonce: str | None = None, timestamp: int | None = None,
) -> dict[str, str]:
    """Build a signed OAuth 1.0 POST form for the FatSecret REST endpoint."""
    parameters = {str(key): str(value) for key, value in data.items()}
    parameters.update({
        "format": "json",
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time()) if timestamp is None else timestamp),
        "oauth_version": "1.0",
    })
    encoded = sorted((_rfc3986(key), _rfc3986(value)) for key, value in parameters.items())
    normalized = "&".join(f"{key}={value}" for key, value in encoded)
    signature_base = "&".join(("POST", _rfc3986(FATSECRET_API_URL), _rfc3986(normalized)))
    signing_key = f"{_rfc3986(consumer_secret)}&"
    digest = hmac.new(signing_key.encode(), signature_base.encode(), hashlib.sha1).digest()
    parameters["oauth_signature"] = base64.b64encode(digest).decode()
    return parameters


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT), headers={"User-Agent": USER_AGENT})


async def _request_with_backoff(
    client, method: str, url: str, *,
    request_kwargs_factory: Callable[[], dict[str, Any]] | None = None, **kwargs,
):
    """Retry only transport, 429, and 5xx failures with bounded sleeps."""
    for attempt in range(3):
        try:
            attempt_kwargs = request_kwargs_factory() if request_kwargs_factory else kwargs
            response = await client.request(method, url, **attempt_kwargs)
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status(); return response
        except (httpx.TransportError, httpx.TimeoutException):
            if attempt == 2: raise
        if attempt < 2:
            retry_after = response.headers.get("Retry-After") if "response" in locals() else None
            try: delay = min(float(retry_after), 2.0) if retry_after else 0.25 * (2 ** attempt)
            except ValueError: delay = 0.25 * (2 ** attempt)
            await asyncio.sleep(delay)
    response.raise_for_status()


async def _get_json(url: str, params: dict[str, Any]) -> Any:
    async with _client() as client:
        return (await _request_with_backoff(client, "GET", url, params=params)).json()


async def _fatsecret_token(client, client_id: str, secret: str) -> str:
    global _TOKEN
    now = time.monotonic()
    if _TOKEN and _TOKEN[2:] == (client_id, secret) and _TOKEN[1] > now + 30:
        return _TOKEN[0]
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    response = await _request_with_backoff(client, "POST", FATSECRET_TOKEN_URL,
        data={"grant_type":"client_credentials", "scope":"basic"},
        headers={"Authorization": f"Basic {basic}"})
    body = response.json(); token = str(body["access_token"])
    _TOKEN = (token, now + max(float(body.get("expires_in", 3600)), 60), client_id, secret)
    return token


async def _fatsecret_request(data: dict[str, Any]) -> Any:
    mode = _fatsecret_provider_mode()
    if mode is None: return None
    try:
        async with _client() as client:
            if mode == "oauth1":
                consumer_key, consumer_secret = (os.environ[key].strip() for key in
                    ("FATSECRET_CONSUMER_KEY", "FATSECRET_CONSUMER_SECRET"))
                response = await _request_with_backoff(
                    client, "POST", FATSECRET_API_URL,
                    request_kwargs_factory=lambda: {"data": _fatsecret_oauth1_data(
                        data, consumer_key, consumer_secret)})
            else:
                client_id, secret = (os.environ[key].strip() for key in
                    ("FATSECRET_CLIENT_ID", "FATSECRET_CLIENT_SECRET"))
                token = await _fatsecret_token(client, client_id, secret)
                response = await _request_with_backoff(client, "POST", FATSECRET_API_URL,
                    data={**data, "format":"json"}, headers={"Authorization": f"Bearer {token}"})
            return response.json()
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else ([value] if value else [])


def _unwrap_foods(body: Any) -> list[Any]:
    if not isinstance(body, Mapping): return []
    value = body.get("foods_search") or body
    if isinstance(value, Mapping): value = value.get("results") or value
    if isinstance(value, Mapping): value = value.get("food") or value.get("foods")
    if isinstance(value, Mapping) and "food" in value: value = value["food"]
    return _as_list(value)


def _unwrap_food(body: Any) -> Mapping[str, Any] | None:
    if not isinstance(body, Mapping): return None
    value: Any = body.get("food_get") or body
    if isinstance(value, Mapping): value = value.get("food") or value
    return value if isinstance(value, Mapping) else None


def _fatsecret_relevant(query: str, food: Mapping[str, Any]) -> bool:
    """Require exact normalized name/brand identity for provider text search."""
    normalized_query = normalize_food_name(query)
    name = normalize_food_name(str(food.get("food_name") or ""))
    brand = normalize_food_name(str(food.get("brand_name") or ""))
    if not normalized_query or not name:
        return False
    accepted = {name}
    if brand:
        accepted.update((f"{brand} {name}", f"{name} {brand}"))
    return normalized_query in accepted


def _relevant(query: str, food: Mapping[str, Any]) -> bool:
    """Conservative token relevance for non-trusted OFF text search results."""
    query_words = set(normalize_food_name(query).split())
    candidate = normalize_food_name(" ".join(
        str(food.get(key) or "") for key in ("food_name", "brand_name", "food_description")
    ))
    words = set(candidate.split())
    meaningful = {word for word in query_words if len(word) > 1}
    comparable = words | {word[:-1] for word in words if len(word) > 3 and word.endswith("s")}
    return bool(meaningful and (meaningful <= comparable
        or len(meaningful & comparable) / len(meaningful) >= 0.6))


async def search_fatsecret(query: str) -> dict[str, Any] | None:
    search = await _fatsecret_request({"method":"foods.search", "search_expression":query, "max_results":5})
    candidate = next((food for food in _unwrap_foods(search)
        if isinstance(food, Mapping) and food.get("food_id") and _fatsecret_relevant(query, food)), None)
    if candidate is None: return None
    food_id = str(candidate["food_id"]); food = _unwrap_food(await _fatsecret_request({"method":"food.get.v4", "food_id":food_id}))
    if food is None or not _fatsecret_relevant(query, food): return None
    servings_value = food.get("servings")
    if isinstance(servings_value, Mapping): servings_value = servings_value.get("serving")
    for serving in _as_list(servings_value):
        if not isinstance(serving, Mapping): continue
        macros = _macros({k: serving.get({"calories":"calories","protein":"protein","carbs":"carbohydrate","fat":"fat","fiber":"fiber"}[k]) for k in MACRO_KEYS})
        if macros is None: continue
        description = str(serving.get("serving_description") or "1 serving")
        try: grams = float(serving.get("metric_serving_amount")) if str(serving.get("metric_serving_unit") or "").casefold() == "g" else None
        except (TypeError, ValueError): grams = None
        result = {"name":str(food.get("food_name") or candidate.get("food_name") or query),
            "macros_per_serving":macros, "serving_size":description, "source":f"FatSecret: {food_id}",
            "attribution":{"provider":"FatSecret", "external_id":food_id,
                "source_url":str(food.get("food_url") or candidate.get("food_url") or "") or None,
                "serving_grams":grams, "serving_basis":"per_serving", "confidence":0.9,
                "cache_allowed":os.environ.get("FATSECRET_CACHE_ALLOWED", "false").casefold()=="true",
                "attribution_text":"Nutrition data from FatSecret"}}
        if grams and grams > 0: result["macros_per_100g"] = {k:round(v*100/grams,2) for k,v in macros.items()}
        result["attribution"]["evidence_hash"] = _result_hash(result)
        return result
    return None


def _macros(values: Mapping[str, Any]) -> dict[str, float] | None:
    result = {}
    for key in MACRO_KEYS:
        raw = values.get(key, 0 if key != "calories" else None)
        try: value = float(raw)
        except (TypeError, ValueError): return None
        if not math.isfinite(value) or value < 0 or value > 1000: return None
        result[key] = round(value, 2)
    return result if result["calories"] > 0 else None


def _result_hash(result: Mapping[str, Any]) -> str:
    body = {k:v for k,v in result.items() if k != "attribution"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",",":"), default=str).encode()).hexdigest()


def _off_result(product: Mapping[str, Any], code: str, *, exact: bool) -> dict[str, Any] | None:
    nutrients = product.get("nutriments"); name = str(product.get("product_name") or "").strip()
    if not isinstance(nutrients, Mapping) or not name: return None
    macros = _macros({key:nutrients.get(field) for field,key in _OFF_NUTRIMENTS.items()})
    if macros is None: return None
    source_url = f"https://world.openfoodfacts.org/product/{code}" if code else None
    result: dict[str, Any] = {"name":name, "macros_per_100g":macros,
        "source":f"OpenFoodFacts{' barcode' if exact else ''}: {code}"}
    serving = str(product.get("serving_size") or "").strip()
    serving_macros = _macros({key:nutrients.get(field) for field,key in _OFF_SERVING.items()})
    if serving: result["serving_size"] = serving
    if serving_macros: result["macros_per_serving"] = serving_macros
    result["attribution"] = {"provider":"OpenFoodFacts", "external_id":code,
        "identifier_type":"barcode" if exact else "text_search", "source_url":source_url,
        "serving_grams":None, "serving_basis":"per_100g", "confidence":1.0 if exact else 0.65,
        "cache_allowed":bool(exact and code), "license":OFF_LICENSE_NAME,
        "license_url":OFF_LICENSE_URL, "attribution_text":"Data from OpenFoodFacts",
        "verification_state":"exact_identifier" if exact else "unknown"}
    result["attribution"]["evidence_hash"] = _result_hash(result)
    return result


async def search_openfoodfacts(query: str) -> dict[str, Any] | None:
    if not str(query).strip(): return None
    try:
        body = await _get_json(OFF_SEARCH_URL, {"search_terms":query,"search_simple":1,"action":"process","json":1,"page_size":5})
        for product in (body.get("products") or [])[:5]:
            if isinstance(product, Mapping):
                found = _off_result(product, str(product.get("code") or ""), exact=False)
                if found and _relevant(query, {"food_name":found["name"]}): return found
    except (httpx.HTTPError, TypeError, ValueError): pass
    return None


async def search_openfoodfacts_by_code(barcode: str) -> dict[str, Any] | None:
    code = str(barcode or "").strip()
    if not code: return None
    try:
        body = await _get_json(OFF_PRODUCT_URL.format(barcode=code), {})
        return _off_result(body["product"], code, exact=True) if body.get("status") == 1 else None
    except (httpx.HTTPError, KeyError, TypeError, ValueError): return None


async def resolve_by_barcode(code: str) -> dict[str, Any] | None:
    return await search_openfoodfacts_by_code(code)


def _query_variants(query: str) -> list[str]:
    text = " ".join(str(query or "").split()); words = text.split(); candidates = [text]
    if len(words) >= 3:
        candidates += [" ".join(words[1:]), " ".join(words[2:]), " ".join(words[:2])]
        if len(words[-1]) >= 4:
            candidates.append(words[-1])
    return list(dict.fromkeys(value for value in candidates if value))


async def resolve_food(query: str, classify: bool = True, *, whole_item: bool = False, catalog_lookup=None) -> dict[str, Any] | None:
    if catalog_lookup:
        try:
            found = await catalog_lookup(query)
            if found: return found
        except Exception: pass
    curated = restaurant_lookup(query)
    if curated: return curated
    fatsecret_enabled = _fatsecret_provider_mode() is not None
    providers = [search_fatsecret, search_openfoodfacts] if fatsecret_enabled else [search_openfoodfacts]
    for variant in _query_variants(query):
        for provider in providers:
            found = await provider(variant)
            if found and (not whole_item or _macros(found.get("macros_per_serving", {}))): return found
    return None


def portion_from_grams(found: Mapping[str, Any], grams: Any):
    values = found.get("macros_per_100g")
    try: weight = float(grams)
    except (TypeError, ValueError): return None
    if not isinstance(values, Mapping) or not math.isfinite(weight) or not 0 < weight <= 5000: return None
    macros = {key:round(float(values.get(key,0))*weight/100,2) for key in MACRO_KEYS}
    return (macros, f"{found['source']} — {found['name']}, {round(weight)} g") if macros["calories"] > 0 else None


def portion_from_serving(found: Mapping[str, Any]):
    macros = _macros(found.get("macros_per_serving", {})) if isinstance(found, Mapping) else None
    source = str(found.get("source") or "").strip() if isinstance(found, Mapping) else ""
    return (macros, source) if macros and source else None
