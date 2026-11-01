"""Official restaurant nutrition as versioned, shared evidence.

The restaurant is the source of truth. For a registered chain the pipeline is::

    official source registry
      -> direct official data (when the registry names a direct adapter)
      -> Exa discovery, restricted to the chain's official domains
      -> Firecrawl extraction of one official page (JSON + page text)
      -> validation: official host, exact item identity, defined serving,
         complete panel, energy/macro consistency, every value present in
         the page text
      -> an immutable, versioned Postgres record (food_items /
         food_source_records / food_nutrition_observations)

Exa only finds candidate pages and Firecrawl only reads them; neither result
is trusted until validation passes. A stored record is reused for every later
meal (no search or scrape) until it is older than the source's ``max_age``;
then it is re-checked once, and a failed re-check keeps the stored version
(disclosed as stale) instead of dropping to a guess. Unknown is never zero: a
panel missing any nutrient is rejected, not filled.

Only numeric facts, the serving, the official URL and a hash of the page are
stored; the page text itself is never persisted. Official rows get no entry in
``food_aliases`` so the legacy alias catalog never serves them: turning the
release gate off restores the previous behavior exactly.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import httpx

import meal_intent
from food_catalog import evidence_hash, normalize_food_name

logger = logging.getLogger("official_nutrition")

REGISTRY_VERSION = "2026-11-01"
VERIFICATION_STATE = "official_curated"
INGEST_DEADLINE_SECONDS = 25.0
MISS_TTL_SECONDS = 6 * 3600
MISS_CACHE_MAX = 512
DISCOVERY_RESULTS = 3
EXTRACT_ATTEMPTS = 2
MAX_IDENTITY_WORDS = 8
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
FIRECRAWL_TIMEOUT_SECONDS = 30.0
NUTRIENTS = ("calories", "protein", "carbs", "fat", "fiber")


@dataclass(frozen=True)
class OfficialSource:
    """One chain's official nutrition source and what we may keep from it."""
    place: str                       # display name; matches meal_intent.CHAINS
    slug: str
    domains: tuple[str, ...]         # official hosts (subdomains included)
    nutrition_url: str               # the chain's own nutrition index
    max_age: timedelta               # re-check a stored record after this
    storage_allowed: bool            # numeric facts + URL may be stored and reused
    storage_basis: str               # why, in words a reviewer can check
    customization: str               # what the published panel does not cover
    direct: str | None = None        # a DIRECT_ADAPTERS key, when one exists

    @property
    def provider(self) -> str:
        return f"official:{self.slug}"


REGISTRY: dict[str, OfficialSource] = {
    "Chick-fil-A": OfficialSource(
        place="Chick-fil-A", slug="chick-fil-a", domains=("chick-fil-a.com",),
        nutrition_url="https://www.chick-fil-a.com/nutrition-allergens",
        max_age=timedelta(days=30), storage_allowed=True,
        storage_basis=("Published numeric nutrition facts with the official page URL and retrieval "
                       "time; no page text, images or markup are stored. Owner to confirm against "
                       "the site's terms before live ingestion."),
        customization=("Standard build as published by Chick-fil-A. Sauces, substitutions, added or "
                       "removed toppings and size changes are not included."),
        direct=None,  # no documented public nutrition API; discovery + extraction instead
    ),
}

# Direct official data adapters, keyed by OfficialSource.direct. Each takes
# (source, identity) and returns a list of Candidate. None are registered:
# no chain in the registry publishes a documented machine-readable feed yet.
DIRECT_ADAPTERS: dict[str, Callable[["OfficialSource", str], Awaitable[list["Candidate"]]]] = {}


class Rejected(ValueError):
    """A candidate panel failed validation. Nothing is stored."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ProviderUnavailable(RuntimeError):
    """A discovery/extraction provider is not configured or did not answer."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

_FILLER = re.compile(r"^(?:(?:a|an|the|my|one|from|at|of)\s+)+")
_TRAILING_PLACE_GLUE = re.compile(r"\s+(?:from|at|of)$")


def _place_words(source: OfficialSource) -> list[str]:
    aliases = [normalize_food_name(a) for a in meal_intent.CHAINS.get(source.place, ())]
    aliases.append(normalize_food_name(source.place))
    return sorted({a for a in aliases if a}, key=len, reverse=True)


def identity_of(text: str, source: OfficialSource) -> str:
    """The item's identity under one chain: normalized, place words and
    articles removed, plural forms folded. ``"Chick-fil-A® Chicken
    Sandwiches"`` and ``"a chicken sandwich from chick fil a"`` are both
    ``"chicken sandwich"``."""
    from food_lookup import _singularize  # local: food_lookup imports this module's callers

    normalized = f" {normalize_food_name(text)} "
    for words in _place_words(source):
        normalized = normalized.replace(f" {words} ", " ")
    normalized = " ".join(normalized.split())
    normalized = _TRAILING_PLACE_GLUE.sub("", _FILLER.sub("", normalized)).strip()
    return _singularize(normalized)


def source_for(query: str) -> OfficialSource | None:
    place = meal_intent.chain_in(query)
    return REGISTRY.get(place) if place else None


# --------------------------------------------------------------------------- #
# Candidates and validation
# --------------------------------------------------------------------------- #

def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:g|kcal|cal|calories)?\s*", value, re.I)
        if not match:
            return None
        value = match.group(1)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class Candidate:
    """One unvalidated panel as a provider returned it."""
    name: str
    serving_text: str
    serving_grams: float | None
    nutrients: dict[str, float | None]
    source_url: str
    page_text: str
    discovered_via: str
    extracted_via: str

    @classmethod
    def from_extraction(cls, raw: Mapping[str, Any], *, source_url: str, page_text: str,
                        discovered_via: str, extracted_via: str) -> "Candidate":
        return cls(
            name=" ".join(str(raw.get("item_name") or "").split())[:160],
            serving_text=" ".join(str(raw.get("serving_size") or "").split())[:80],
            serving_grams=_number(raw.get("serving_grams")),
            nutrients={key: _number(raw.get(key)) for key in NUTRIENTS},
            source_url=str(source_url or ""), page_text=str(page_text or ""),
            discovered_via=discovered_via, extracted_via=extracted_via)


@dataclass(frozen=True)
class Panel:
    """A validated official panel, ready to store."""
    place: str
    identity: str
    display_name: str
    serving_text: str
    serving_grams: float | None
    nutrients: dict[str, float]
    source_url: str
    page_sha256: str
    discovered_via: str
    extracted_via: str
    retrieved_at: datetime = field(default_factory=utcnow)


def official_host(url: str, source: OfficialSource) -> str | None:
    """The URL's host when it is https on one of the source's official domains."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    if parts.scheme != "https" or not host:
        return None
    if any(host == domain or host.endswith("." + domain) for domain in source.domains):
        return host
    return None


def _in_page(value: float, page: str) -> bool:
    text = f"{value:g}"
    return re.search(rf"(?<![\d.]){re.escape(text)}(?![\d.])", page) is not None


def validate(candidate: Candidate, source: OfficialSource, identity: str) -> Panel:
    """Accept a candidate only when it is provably this chain's own panel for
    exactly this item. Raises Rejected with the first failing reason."""
    if official_host(candidate.source_url, source) is None:
        raise Rejected("not_official_source")
    display = re.sub(r"\s*[®™©]\s*", " ", candidate.name).strip()
    display = " ".join(display.split())
    if not display or identity_of(display, source) != identity:
        raise Rejected("identity_mismatch")
    if not candidate.serving_text:
        raise Rejected("serving_undefined")
    if candidate.serving_grams is not None and not 0 < candidate.serving_grams <= 3000:
        raise Rejected("serving_grams_out_of_range")
    nutrients = candidate.nutrients
    if any(nutrients.get(key) is None for key in NUTRIENTS):
        # The shared observation table has no NULL nutrients, and unknown is
        # never stored as zero: an incomplete panel is not evidence.
        raise Rejected("incomplete_panel")
    values = {key: float(nutrients[key]) for key in NUTRIENTS}
    if not 0 < values["calories"] <= 3000 or any(not 0 <= values[k] <= 500 for k in NUTRIENTS[1:]):
        raise Rejected("values_out_of_range")
    energy = 4 * values["protein"] + 4 * values["carbs"] + 9 * values["fat"]
    if abs(energy - values["calories"]) > max(25.0, 0.2 * values["calories"]):
        raise Rejected("energy_inconsistent")
    page = candidate.page_text
    page_identity = identity_of(page, source) if page else ""
    if not page or not all(word in page_identity.split() for word in identity.split()):
        raise Rejected("item_not_in_page")
    if not all(_in_page(values[key], page) for key in NUTRIENTS) or not (
            normalize_food_name(candidate.serving_text) in normalize_food_name(page)):
        raise Rejected("values_not_in_page")
    return Panel(place=source.place, identity=identity, display_name=display,
                 serving_text=candidate.serving_text, serving_grams=candidate.serving_grams,
                 nutrients=values, source_url=candidate.source_url,
                 page_sha256=hashlib.sha256(page.encode("utf-8")).hexdigest(),
                 discovered_via=candidate.discovered_via, extracted_via=candidate.extracted_via,
                 retrieved_at=utcnow())


def record_for(panel: Panel, source: OfficialSource) -> dict[str, Any]:
    """The store write for one validated panel.

    ``values_digest`` identifies the nutrition version (same numbers, same
    serving, same URL -> same digest). The source record digest adds the
    verification day, so a re-check on a later day is its own dated record
    while a replay on the same day is idempotent."""
    evidence = {"place": source.place, "identity": panel.identity, "name": panel.display_name,
                "serving_text": panel.serving_text, "serving_grams": panel.serving_grams,
                "nutrients": panel.nutrients, "source_url": panel.source_url}
    values_digest = evidence_hash(evidence)
    verified_on = panel.retrieved_at.date().isoformat()
    return {
        "provider": source.provider,
        "normalized_name": panel.identity,
        "display_name": f"{source.place} {identity_display(panel, source)}",
        "brand": source.place,
        "product_identity": f"{source.provider}:{panel.identity}",
        "source_reference": f"{source.place} official nutrition: {panel.source_url}",
        "source_url": panel.source_url,
        "retrieved_at": panel.retrieved_at,
        "evidence_hash": evidence_hash({"values_digest": values_digest, "verified_on": verified_on}),
        "trusted": True,
        "cache_allowed": source.storage_allowed,
        "metadata": {**evidence, "values_digest": values_digest, "verified_on": verified_on,
                     "discovered_via": panel.discovered_via, "extracted_via": panel.extracted_via,
                     "page_sha256": panel.page_sha256, "customization": source.customization,
                     "storage_basis": source.storage_basis, "registry_version": REGISTRY_VERSION},
        "serving_text": panel.serving_text,
        "serving_grams": panel.serving_grams,
        "nutrients": dict(panel.nutrients),
    }


def identity_display(panel: Panel, source: OfficialSource) -> str:
    """The official item name without the chain's own name ("Chicken Sandwich")."""
    text = panel.display_name
    for words in _place_words(source):
        pattern = r"\b" + r"[\s\-]*".join(re.escape(w) for w in words.split()) + r"\b"
        text = re.sub(pattern, " ", text, flags=re.I)
    return " ".join(text.split()) or panel.display_name


# --------------------------------------------------------------------------- #
# Providers (HTTP is module-level so tests replace only the transport)
# --------------------------------------------------------------------------- #

async def post_exa_search(key: str, payload: dict[str, Any]) -> httpx.Response:
    import web_nutrition_lookup
    return await web_nutrition_lookup.post_exa_search(key, payload)


async def post_firecrawl_scrape(key: str, payload: dict[str, Any]) -> httpx.Response:
    from http_clients import shared_client
    return await shared_client(FIRECRAWL_TIMEOUT_SECONDS).post(FIRECRAWL_SCRAPE_URL, headers={
        "authorization": f"Bearer {key}", "content-type": "application/json"}, json=payload)


_PANEL_SCHEMA = {
    "type": "object",
    "properties": {
        "item_name": {"type": "string"},
        "serving_size": {"type": "string"},
        "serving_grams": {"type": ["number", "null"]},
        "calories": {"type": ["number", "null"]},
        "protein": {"type": ["number", "null"]},
        "carbs": {"type": ["number", "null"]},
        "fat": {"type": ["number", "null"]},
        "fiber": {"type": ["number", "null"]},
    },
    "required": ["item_name", "serving_size", "serving_grams", *NUTRIENTS],
}


def _key(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ProviderUnavailable(f"{name.lower()}_not_configured")
    return value


async def discover(source: OfficialSource, identity: str) -> list[str]:
    """Official pages that may hold this item's panel (Exa, official domains only)."""
    key = _key("EXA_API_KEY")
    response = await post_exa_search(key, {
        "query": f"{source.place} {identity} nutrition facts",
        "type": "auto", "numResults": DISCOVERY_RESULTS,
        "includeDomains": list(source.domains),
    })
    if response.status_code != 200:
        raise ProviderUnavailable(f"exa_http_{response.status_code}")
    try:
        results = response.json().get("results")
    except (ValueError, AttributeError):
        raise ProviderUnavailable("exa_invalid_json") from None
    urls: list[str] = []
    for result in results if isinstance(results, list) else []:
        url = result.get("url") if isinstance(result, Mapping) else None
        if isinstance(url, str) and official_host(url, source) and url not in urls:
            urls.append(url)
    return urls


async def extract(source: OfficialSource, url: str, identity: str, *,
                  discovered_via: str) -> Candidate | None:
    """Read one official page with Firecrawl: a JSON panel plus the page text
    the panel is checked against. Returns None when the page had no panel."""
    key = _key("FIRECRAWL_API_KEY")
    response = await post_firecrawl_scrape(key, {
        "url": url,
        "formats": ["markdown", {
            "type": "json", "schema": _PANEL_SCHEMA,
            "prompt": (f"Extract the published nutrition facts for exactly the {source.place} menu item "
                       f"'{identity}' as shown on this page: its exact item name, the serving size text, "
                       "serving weight in grams if shown, calories, protein, total carbohydrates, total fat "
                       "and dietary fiber in grams. Use null for any value the page does not show. Do not "
                       "estimate or use other items."),
        }],
        "onlyMainContent": True,
        "timeout": 20000,
    })
    if response.status_code != 200:
        raise ProviderUnavailable(f"firecrawl_http_{response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise ProviderUnavailable("firecrawl_invalid_json") from None
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, Mapping) or not body.get("success", False):
        raise ProviderUnavailable("firecrawl_unsuccessful")
    panel = data.get("json")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
    final_url = str(metadata.get("url") or metadata.get("sourceURL") or url)
    if metadata.get("statusCode") not in (None, 200) or not isinstance(panel, Mapping):
        return None
    return Candidate.from_extraction(panel, source_url=final_url,
                                     page_text=str(data.get("markdown") or ""),
                                     discovered_via=discovered_via, extracted_via="firecrawl")


async def ingest(source: OfficialSource, identity: str) -> Panel | None:
    """registry -> direct -> Exa -> Firecrawl -> validation. None when no
    official page yields a valid panel; provider outages raise."""
    reasons: list[str] = []
    adapter = DIRECT_ADAPTERS.get(source.direct or "")
    if adapter is not None:
        for candidate in await adapter(source, identity):
            try:
                return validate(candidate, source, identity)
            except Rejected as exc:
                reasons.append(exc.reason)
    for url in (await discover(source, identity))[:EXTRACT_ATTEMPTS]:
        candidate = await extract(source, url, identity, discovered_via="exa")
        if candidate is None:
            reasons.append("no_panel")
            continue
        try:
            return validate(candidate, source, identity)
        except Rejected as exc:
            reasons.append(exc.reason)
    logger.info("official nutrition: no valid panel place=%s reasons=%s", source.slug, ",".join(reasons) or "none")
    return None


# --------------------------------------------------------------------------- #
# Resolution with reuse
# --------------------------------------------------------------------------- #

_misses: "OrderedDict[tuple[str, str], float]" = OrderedDict()
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def clear_process_caches() -> None:
    _misses.clear()
    _locks.clear()


def _recent_miss(key: tuple[str, str]) -> bool:
    import time
    at = _misses.get(key)
    if at is None:
        return False
    if time.monotonic() - at > MISS_TTL_SECONDS:
        _misses.pop(key, None)
        return False
    return True


def _remember_miss(key: tuple[str, str]) -> None:
    import time
    _misses[key] = time.monotonic()
    _misses.move_to_end(key)
    while len(_misses) > MISS_CACHE_MAX:
        _misses.popitem(last=False)


def _fresh(stored: Mapping[str, Any], source: OfficialSource) -> bool:
    retrieved = stored.get("retrieved_at")
    return isinstance(retrieved, datetime) and utcnow() - retrieved <= source.max_age


def _result(stored: Mapping[str, Any], source: OfficialSource, freshness: str) -> dict[str, Any]:
    """The resolver shape, built only from the persisted row."""
    retrieved = stored["retrieved_at"]
    host = official_host(str(stored["source_url"]), source) or ""
    checked = retrieved.date().isoformat() if isinstance(retrieved, datetime) else str(retrieved)
    return {
        "name": stored["display_name"],
        "macros_per_serving": {key: float(stored[key]) for key in NUTRIENTS},
        "serving_size": stored["serving_text"],
        "source": f"Curated restaurant: {source.place} official nutrition ({stored['source_url']}, checked {checked})",
        "customization": source.customization,
        "attribution": {
            "provider": source.provider, "external_id": stored["product_identity"],
            "verification_state": VERIFICATION_STATE, "cache_allowed": bool(stored["cache_allowed"]),
            "source_host": host, "source_url": stored["source_url"],
            "food_item_id": str(stored["food_item_id"]), "observation_id": str(stored["observation_id"]),
            "source_record_id": str(stored["source_record_id"]),
            "retrieved_at": retrieved.isoformat() if isinstance(retrieved, datetime) else str(retrieved),
            "freshness": freshness, "customization": source.customization,
        },
    }


async def lookup(query: str, *, store) -> dict[str, Any] | None:
    """Official nutrition for a chain item, reusing stored evidence first.

    None means "not handled here" (unregistered place, unusable identity, or
    no valid official panel), and the caller's existing chain continues."""
    source = source_for(query)
    if source is None:
        return None
    identity = identity_of(query, source)
    if not identity or len(identity.split()) > MAX_IDENTITY_WORDS or re.match(r"^\d", identity):
        return None  # a count or empty item is never searched as a name
    stored = await store.fetch_official_observation(source.provider, identity)
    if stored is not None and _fresh(stored, source):
        return _result(stored, source, "fresh")
    key = (source.slug, identity)
    if _recent_miss(key):
        return _result(stored, source, "stale") if stored is not None else None
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        stored = await store.fetch_official_observation(source.provider, identity)
        if stored is not None and _fresh(stored, source):
            return _result(stored, source, "fresh")
        try:
            panel = await asyncio.wait_for(ingest(source, identity), INGEST_DEADLINE_SECONDS)
        except ProviderUnavailable as exc:
            logger.info("official nutrition: provider unavailable place=%s reason=%s", source.slug, exc.reason)
            panel = None
        except (TimeoutError, httpx.HTTPError) as exc:
            logger.info("official nutrition: provider failed place=%s error=%s", source.slug, type(exc).__name__)
            panel = None
        if panel is None:
            _remember_miss(key)
            return _result(stored, source, "stale") if stored is not None else None
        await store.save_official_observation(record_for(panel, source))
        saved = await store.fetch_official_observation(source.provider, identity)
        return _result(saved, source, "fresh") if saved is not None else None
