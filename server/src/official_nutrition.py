"""Official restaurant nutrition for ANY restaurant, as versioned shared evidence.

There is no list of restaurants. When the chat logs a food from a place:

    find that restaurant's own website (live web search, verified by name)
      -> search that site only for the item (Exa, includeDomains)
      -> render the candidate page or PDF (Firecrawl, markdown only)
      -> read the panel in code from the page itself:
           * an item page (URL / title / heading name exactly this item) with
             labeled values, label-then-value or value-then-label, or
           * a nutrition table or PDF: the header row maps every nutrient to
             a column and exactly one row names exactly this item
      -> validate (complete panel, energy consistency, no ambiguity)
      -> an immutable, versioned Postgres record (food_items /
         food_source_records / food_nutrition_observations)

The restaurant is the source of truth: Exa only finds pages, Firecrawl only
renders them, and nothing is asked of an LLM, so no number can be invented.
A stored record is reused for every later meal (no search or render) until it
is older than MAX_AGE; then it is re-checked once, and a failed re-check keeps
the stored version (disclosed as stale). The verified site is remembered from
stored records, so a place's site is searched for once. Unknown is never
zero: a panel missing any nutrient is rejected. Only numeric facts, the
serving, the official URL and a page hash are stored; never the page.
Official rows get no food_aliases entry, so the legacy alias catalog never
serves them and turning the release gate off restores the previous behavior.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

import meal_intent
from food_catalog import evidence_hash, normalize_food_name

logger = logging.getLogger("official_nutrition")

PIPELINE_VERSION = "2026-11-01-dynamic"
VERIFICATION_STATE = "official_curated"
MAX_AGE = timedelta(days=30)
DEFAULT_SERVING = "1 menu item as published"
# How long a meal waits for a first-sighting ingest. Kept well inside
# food_lookup.RESOLUTION_DEADLINE_SECONDS; a slower ingest keeps running in the
# background and its record is used from the next meal on.
INGEST_DEADLINE_SECONDS = 8.0
BACKGROUND_DEADLINE_SECONDS = 90.0
REVOKED_IDS_FLAG = "OFFICIAL_NUTRITION_REVOKED_IDS"
MISS_TTL_SECONDS = 6 * 3600
SITE_TTL_SECONDS = 24 * 3600
CACHE_MAX = 512
SITE_RESULTS = 8
DISCOVERY_RESULTS = 5
EXTRACT_ATTEMPTS = 3
MAX_IDENTITY_WORDS = 8
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
FIRECRAWL_TIMEOUT_SECONDS = 45.0
NUTRIENTS = ("calories", "protein", "carbs", "fat", "fiber")
STORAGE_BASIS = ("Published numeric nutrition facts with the official page URL and retrieval time; "
                 "no page text, images or markup are stored.")

# The place the conversation tagged for the food being resolved (meal drafts
# set it from the model's restaurant tag). Without it, a place named in the
# words is recognized.
requested_place: ContextVar[str | None] = ContextVar("official_requested_place", default=None)


class Rejected(ValueError):
    """A candidate page failed validation. Nothing is stored."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ProviderUnavailable(RuntimeError):
    """A search/rendering provider is not configured or did not answer."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Places and identity
# --------------------------------------------------------------------------- #

def _compact(text: str) -> str:
    """Letters and digits only: "Chick-fil-A" -> "chickfila", "Raising Cane's" -> "raisingcanes"."""
    text = str(text or "").casefold().replace("&", "and").replace("’", "'")
    return re.sub(r"[^a-z0-9]+", "", text)


@dataclass(frozen=True)
class Site:
    """A restaurant and its verified official website."""
    place: str
    domain: str          # registrable domain, e.g. "chick-fil-a.com"

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", normalize_food_name(self.place.replace("'", ""))).strip("-")

    @property
    def provider(self) -> str:
        return f"official:{self.slug}"

    @property
    def customization(self) -> str:
        return (f"Standard build as published by {self.place}. Sauces, substitutions, added or removed "
                "toppings and size changes are not included.")


def place_for(query: str) -> str | None:
    """The restaurant this food came from: the conversation's tag, else a place
    named in the words. Never guessed from the food itself."""
    tagged = requested_place.get()
    if tagged and tagged.strip():
        return " ".join(tagged.split())[:60]
    return meal_intent.chain_in(query)


def _place_phrases(place: str) -> list[str]:
    phrases = {normalize_food_name(place), normalize_food_name(place.replace("'", "").replace("’", ""))}
    try:
        known = meal_intent.clean_place(place)
    except ValueError:
        known = None
    phrases |= {normalize_food_name(a) for a in meal_intent.CHAINS.get(known or "", ())}
    return sorted({p for p in phrases if p}, key=len, reverse=True)


_FILLER = re.compile(r"^(?:(?:a|an|the|my|one|from|at|of)\s+)+")
_TRAILING_PLACE_GLUE = re.compile(r"\s+(?:from|at|of)$")


def identity_of(text: str, place: str) -> str:
    """The item's identity at one place: normalized, the place's name and
    articles removed, plural forms folded. "Chick-fil-A® Chicken Sandwiches"
    and "a chicken sandwich from chick fil a" are both "chicken sandwich"."""
    from food_lookup import _singularize  # local import: food_lookup is heavy

    normalized = f" {normalize_food_name(str(text).replace('’', '').replace(chr(39), ''))} "
    for words in _place_phrases(place):
        normalized = normalized.replace(f" {words} ", " ")
    normalized = " ".join(normalized.split())
    normalized = _TRAILING_PLACE_GLUE.sub("", _FILLER.sub("", normalized)).strip()
    return _singularize(normalized)


# --------------------------------------------------------------------------- #
# Finding the official website
# --------------------------------------------------------------------------- #

# A restaurant's own domain spells its name, optionally followed by generic
# words ("panerabread", "dunkindonuts", "joespizzanyc"). Anything else after
# the name ("menu", "prices", "nutrition") is a third-party site.
_GENERIC_DOMAIN_WORDS = (
    "restaurants", "restaurant", "mexicangrill", "grill", "bread", "bakery", "cafe", "kitchen",
    "pizzeria", "pizza", "burgers", "burger", "donuts", "subs", "bbq", "barbecue", "chicken",
    "coffee", "tea", "bar", "steakhouse", "diner", "eatery", "eats", "foods", "food", "company",
    "co", "inc", "usa", "us", "official", "nyc", "la", "sf", "chi", "atl", "tx", "fl", "ca",
)
# Aggregators, delivery apps and social sites: never a restaurant's own site.
_THIRD_PARTY = frozenset({
    "nutritionix", "fastfoodnutrition", "fatsecret", "myfitnesspal", "eatthismuch", "menuwithprice",
    "nutritioncharts", "yelp", "doordash", "ubereats", "grubhub", "postmates", "seamless",
    "tripadvisor", "wikipedia", "facebook", "instagram", "twitter", "x", "tiktok", "linkedin",
    "google", "apple", "amazon", "reddit", "youtube", "pinterest", "allmenus", "menupages",
    "zomato", "opentable", "toasttab", "squareup", "clover", "chownow",
})
_TWO_LEVEL_SUFFIXES = frozenset({"co", "com", "org", "net", "ac", "gov"})


def registrable_domain(host: str) -> str:
    labels = [part for part in host.casefold().strip(".").split(".") if part]
    if len(labels) >= 3 and labels[-2] in _TWO_LEVEL_SUFFIXES and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _only_generic(rest: str) -> bool:
    if not rest:
        return True
    return any(rest.startswith(word) and _only_generic(rest[len(word):]) for word in _GENERIC_DOMAIN_WORDS)


def names_place(domain: str, place: str) -> bool:
    """Whether a registrable domain is plausibly the place's own: its name label
    is the place's name (spaces/punctuation aside) plus only generic words, or
    the place's full name is the label plus generic words ("Chipotle Mexican
    Grill" -> chipotle.com)."""
    label = _compact(domain.split(".", 1)[0])
    key = _compact(place)
    if not label or not key or label in _THIRD_PARTY:
        return False
    if label.startswith(key):
        return _only_generic(label[len(key):])
    if key.startswith(label) and len(label) >= 4:
        return _only_generic(key[len(label):])
    return False


def official_host(url: str, site: Site) -> str | None:
    """The URL's host when it is https on the site's own registrable domain."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    if parts.scheme != "https" or not host or not site.domain:
        return None
    return host if registrable_domain(host) == site.domain else None


def pick_official_site(place: str, results: list[Mapping[str, Any]]) -> Site | None:
    """The official site among search results: a domain that names the place
    AND whose own page titles name it. The domain with most such pages wins."""
    key = _compact(place)
    votes: dict[str, int] = {}
    order: list[str] = []
    for result in results:
        url = result.get("url") if isinstance(result, Mapping) else None
        if not isinstance(url, str):
            continue
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme != "https" or not parts.hostname:
            continue
        domain = registrable_domain(parts.hostname)
        if not names_place(domain, place):
            continue
        title = _compact(str(result.get("title") or ""))
        if key not in title and _compact(domain.split(".", 1)[0]) not in title:
            continue
        if domain not in votes:
            order.append(domain)
        votes[domain] = votes.get(domain, 0) + 1
    if not votes:
        return None
    best = max(order, key=lambda d: (votes[d], -order.index(d)))
    return Site(place=place, domain=best)


# --------------------------------------------------------------------------- #
# Reading a panel from a rendered page (in code; no LLM)
# --------------------------------------------------------------------------- #

def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?:g|kcal|cal|calories)?\s*", value, re.I)
        if not match:
            return None
        value = match.group(1).replace(",", "")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


_NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
_SEP = r"[\s:|*]{0,8}"
# Label words per nutrient, each with ONE qualifier group, so "Saturated Fat",
# "Trans Fat" or "Net Carbs" are skipped instead of read as the total.
_LABEL = {
    "calories": r"(total\s+)?(?:calories|cal\.?|kcal|energy)",
    "protein": r"(total\s+)?protein",
    "carbs": r"((?:total|net)\s+)?(?:carbohydrates?|carbs)",
    "fat": r"((?:total|saturated|trans|polyunsaturated|monounsaturated|unsaturated)\s+)?fat",
    "fiber": r"((?:total|dietary)\s+)?fib(?:er|re)",
}
_SKIP = {"carbs": {"net"}, "fat": {"saturated", "trans", "polyunsaturated", "monounsaturated", "unsaturated"}}
_UNIT = {"calories": r"(?:\s*(?:cal\.?|kcal|calories))?", "protein": r"\s*(?:g|grams?)\b",
         "carbs": r"\s*(?:g|grams?)\b", "fat": r"\s*(?:g|grams?)\b", "fiber": r"\s*(?:g|grams?)\b"}
# label-then-value: "Total Fat\n\n16g", "Protein: 29g". Group 1 = qualifier, group 2 = value.
_AFTER = {key: re.compile(
    rf"(?<![a-z]){_LABEL[key]}\b(?!\s+from){_SEP}{_NUM}" +
    (r"(?!\s*(?:g|mg|%))" if key == "calories" else _UNIT[key])) for key in NUTRIENTS}
# value-then-label on one line, the label ending the line: "45grams Total Carbs",
# "580Cal. Calories". Group 1 = value, group 2 = qualifier.
_BEFORE = {key: re.compile(
    rf"(?<![\d.,]){_NUM}{_UNIT[key]}[ \t]{{0,3}}{_LABEL[key]}\b[ \t]*(?=$|\(|\n)", re.MULTILINE)
    for key in NUTRIENTS}


def labeled_values(section: str) -> dict[str, float | None]:
    """Each nutrient from its own label, whichever side the value is printed on.
    Two readings of one nutrient that disagree make the panel ambiguous."""
    text = section.casefold()
    found: dict[str, float | None] = {}
    for key in NUTRIENTS:
        after = before = None
        for match in _AFTER[key].finditer(text):
            if (match.group(1) or "").strip() not in _SKIP.get(key, set()):
                after = float(match.group(2).replace(",", ""))
                break
        for match in _BEFORE[key].finditer(text):
            if (match.group(2) or "").strip() not in _SKIP.get(key, set()):
                before = float(match.group(1).replace(",", ""))
                break
        if after is not None and before is not None and after != before:
            raise Rejected("ambiguous_panel")
        found[key] = after if after is not None else before
    return found


_HEADING = re.compile(r"^(#+)\s+(\S.*)$")
_PANEL_HEADINGS = frozenset({"nutrition", "nutrition facts", "nutrition information", "nutritional information",
                             "nutrition info", "nutrition allergens", "nutrition and allergens", "nutrition allergen",
                             "nutrition summary", "nutritional facts", "nutrition details"})


def _is_panel_heading(text: str) -> bool:
    return normalize_food_name(text) in _PANEL_HEADINGS


def _title_item(title: str) -> str:
    """"Chick-fil-A® Chicken Sandwich | Chick-fil-A" / "Big Mac®: McDonald's ..." -> the item part."""
    return re.split(r"\s+[|\u2013\u2014-]\s+|:\s+", title, maxsplit=1)[0] if title else ""


def _slug_item(url: str) -> str:
    last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"\.(?:html?|aspx?|php)$", "", last).replace("-", " ").replace("_", " ")


def item_page(url: str, title: str, page: str, place: str, identity: str) -> bool:
    """Whether this is the item's own page. Signals: the URL's last segment, the
    title's item part, and the first top-level heading before the panel. With
    two or more signals, at least two must name exactly this item; a single
    signal must. A top-level heading naming anything else, or a catering page
    (trays, not one item), is never this item's page."""
    if "catering" in urlsplit(url).path.casefold():
        return False
    signals = [identity_of(_slug_item(url), place) == identity]
    if title:
        signals.append(identity_of(_title_item(title), place) == identity)
    for line in page.splitlines():
        heading = _HEADING.match(line)
        if not heading:
            continue
        if _is_panel_heading(heading.group(2)):
            break
        if len(heading.group(1)) == 1:
            if identity_of(heading.group(2), place) != identity:
                return False
            signals.append(True)
            break
    return sum(signals) >= (2 if len(signals) >= 2 else 1)


def panel_blocks(page: str, place: str, identity: str) -> list[str]:
    """The page's nutrition panel(s): from each panel heading to the next heading
    that is neither a panel label nor this item. Another item's or a sauce's
    sub-section never supplies numbers."""
    blocks: list[list[str]] = []
    inside = False
    for line in page.splitlines():
        heading = _HEADING.match(line)
        if heading:
            if _is_panel_heading(heading.group(2)):
                inside = True
                blocks.append([line])
                continue
            if inside and identity_of(heading.group(2), place) != identity:
                inside = False
                continue
        if inside:
            blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


def _item_page_panel(page: str, place: str, identity: str) -> tuple[dict[str, float], str] | None:
    blocks = panel_blocks(page, place, identity)
    if not blocks:
        return None
    parsed = [labeled_values(block) for block in blocks]
    complete = [values for values in parsed if all(values[k] is not None for k in NUTRIENTS)]
    if not complete:
        raise Rejected("nutrient_not_in_page")
    if any(values != complete[0] for values in complete[1:]):
        raise Rejected("ambiguous_panel")
    section = "\n".join(blocks)
    match = re.search(r"serving size[\s:|*]{0,6}([^\n|]{1,60})", section, re.I)
    if match and match.group(1).strip():
        serving = " ".join(match.group(1).split())
    elif "per serving" in section.casefold() or "nutrition summary" in section.casefold():
        serving = DEFAULT_SERVING
    else:
        raise Rejected("serving_undefined")
    return {k: float(complete[0][k]) for k in NUTRIENTS}, serving


# A markdown link, including an optional quoted title that may hold "(V)".
_LINK = re.compile(r'\[([^\]]*)\]\([^()\s"]+(?:\s+"[^"]*")?\)')


def _cells(line: str) -> list[str]:
    text = re.sub(r"\\\[[^\]]*\\\]", "", line)  # escaped "\[more info\]" link texts
    text = _LINK.sub(r"\1", text)
    return [cell.strip() for cell in text.strip().strip("|").split("|")]


_COLUMN = {
    "calories": lambda h: bool(re.search(r"\b(?:calories|cal|kcal|energy)\b", h)) and "from fat" not in h
                          and "%" not in h,
    "protein": lambda h: "protein" in h,
    "carbs": lambda h: bool(re.search(r"carbohydrate|carbs", h)) and "net" not in h,
    "fat": lambda h: bool(re.search(r"\bfat\b", h)) and not re.search(
        r"saturated|trans|from fat|calories|polyunsat|monounsat", h),
    "fiber": lambda h: bool(re.search(r"fib(?:er|re)", h)),
}


def _table_rows(page: str, place: str, identity: str) -> list[tuple[dict[str, float], str]]:
    """Rows naming exactly this item in tables whose header maps every nutrient
    to exactly one column. A header that can't be mapped makes the table
    unusable: columns are never guessed by position."""
    lines = page.splitlines()
    rows: list[tuple[dict[str, float], str]] = []
    for index, line in enumerate(lines):
        if index == 0 or not re.match(r"^\|\s*:?-{3}", line):
            continue
        header = [cell.casefold() for cell in _cells(lines[index - 1])]
        columns: dict[str, int] = {}
        for key, accepts in _COLUMN.items():
            hits = [i for i, cell in enumerate(header) if i > 0 and accepts(cell)]
            if len(hits) == 1:
                columns[key] = hits[0]
        if len(columns) != len(NUTRIENTS) or len(set(columns.values())) != len(NUTRIENTS):
            continue
        serving_col = next((i for i, cell in enumerate(header)
                            if i > 0 and re.search(r"serving|portion", cell)), None)
        for row in lines[index + 1:]:
            if not row.startswith("|"):
                break
            cells = _cells(row)
            if len(cells) != len(header) or identity_of(cells[0], place) != identity:
                continue
            values = {key: _number(cells[col]) for key, col in columns.items()}
            if any(value is None for value in values.values()):
                raise Rejected("nutrient_not_in_page")
            serving = cells[serving_col] if serving_col is not None and cells[serving_col] else DEFAULT_SERVING
            rows.append(({k: float(v) for k, v in values.items()}, serving))
    return rows


def read_panel(url: str, title: str, page: str, place: str, identity: str) -> tuple[dict[str, float], str, str]:
    """(nutrients, serving, layout) for exactly this item, read from the page in
    code. Raises Rejected when the page holds no single, complete, unambiguous
    panel for it."""
    found: list[tuple[dict[str, float], str, str]] = []
    if item_page(url, title, page, place, identity):
        panel = _item_page_panel(page, place, identity)
        if panel is not None:
            found.append((*panel, "item_page"))
    for values, serving in _table_rows(page, place, identity):
        found.append((values, serving, "table_row"))
    if not found:
        raise Rejected("no_panel_for_item")
    if any(values != found[0][0] for values, _serving, _layout in found[1:]):
        raise Rejected("ambiguous_panel")
    return found[0]


# --------------------------------------------------------------------------- #
# Validation and records
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Candidate:
    """One rendered official page, before validation."""
    source_url: str
    title: str
    page_text: str
    discovered_via: str = "exa"
    extracted_via: str = "firecrawl_markdown"


@dataclass(frozen=True)
class Panel:
    """A validated official panel, ready to store."""
    place: str
    identity: str
    display_name: str
    serving_text: str
    nutrients: dict[str, float]
    source_url: str
    layout: str
    page_sha256: str
    discovered_via: str
    extracted_via: str
    retrieved_at: datetime = field(default_factory=utcnow)


def validate(candidate: Candidate, site: Site, identity: str) -> Panel:
    """Accept a page only when it is provably the restaurant's own panel for
    exactly this item. Raises Rejected with the first failing reason."""
    if official_host(candidate.source_url, site) is None:
        raise Rejected("not_official_source")
    values, serving, layout = read_panel(candidate.source_url, candidate.title, candidate.page_text,
                                         site.place, identity)
    if not 0 < values["calories"] <= 3000 or any(not 0 <= values[k] <= 500 for k in NUTRIENTS[1:]):
        raise Rejected("values_out_of_range")
    energy = 4 * values["protein"] + 4 * values["carbs"] + 9 * values["fat"]
    if abs(energy - values["calories"]) > max(25.0, 0.2 * values["calories"]):
        raise Rejected("energy_inconsistent")
    name = _title_item(candidate.title) if layout == "item_page" else ""
    if not name or identity_of(name, site.place) != identity:
        name = identity.title()
    display = " ".join(re.sub(r"\s*[®™©]\s*", " ", name).split())
    return Panel(place=site.place, identity=identity, display_name=display, serving_text=serving,
                 nutrients=values, source_url=candidate.source_url, layout=layout,
                 page_sha256=hashlib.sha256(candidate.page_text.encode("utf-8")).hexdigest(),
                 discovered_via=candidate.discovered_via, extracted_via=candidate.extracted_via,
                 retrieved_at=utcnow())


def _without_place(text: str, place: str) -> str:
    for phrase in _place_phrases(place):
        pattern = r"\b" + r"[\s\-'’]*".join(re.escape(w) for w in phrase.split()) + r"\b"
        text = re.sub(pattern, " ", text, flags=re.I)
    return " ".join(text.split())


def record_for(panel: Panel, site: Site) -> dict[str, Any]:
    """The store write for one validated panel.

    ``values_digest`` identifies the nutrition version (same numbers, serving and
    URL -> same digest). The source record digest adds the verification day, so
    a re-check on a later day is its own dated record while a same-day replay
    is idempotent."""
    evidence = {"place": site.place, "identity": panel.identity, "name": panel.display_name,
                "serving_text": panel.serving_text, "nutrients": panel.nutrients,
                "source_url": panel.source_url}
    values_digest = evidence_hash(evidence)
    verified_on = panel.retrieved_at.date().isoformat()
    item = _without_place(panel.display_name, site.place) or panel.display_name
    return {
        "provider": site.provider,
        "normalized_name": panel.identity,
        "display_name": f"{site.place} {item}",
        "brand": site.place,
        "product_identity": f"{site.provider}:{panel.identity}",
        "source_reference": f"{site.place} official nutrition: {panel.source_url}",
        "source_url": panel.source_url,
        "retrieved_at": panel.retrieved_at,
        "evidence_hash": evidence_hash({"values_digest": values_digest, "verified_on": verified_on}),
        "trusted": True,
        "cache_allowed": True,
        "metadata": {**evidence, "values_digest": values_digest, "verified_on": verified_on,
                     "official_domain": site.domain, "layout": panel.layout,
                     "discovered_via": panel.discovered_via, "extracted_via": panel.extracted_via,
                     "page_sha256": panel.page_sha256, "customization": site.customization,
                     "storage_basis": STORAGE_BASIS, "pipeline_version": PIPELINE_VERSION},
        "serving_text": panel.serving_text,
        "serving_grams": None,
        "nutrients": dict(panel.nutrients),
    }


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


def _key(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ProviderUnavailable(f"{name.lower()}_not_configured")
    return value


async def _exa(payload: dict[str, Any]) -> list[Mapping[str, Any]]:
    response = await post_exa_search(_key("EXA_API_KEY"), payload)
    if response.status_code != 200:
        raise ProviderUnavailable(f"exa_http_{response.status_code}")
    try:
        results = response.json().get("results")
    except (ValueError, AttributeError):
        raise ProviderUnavailable("exa_invalid_json") from None
    return [r for r in results if isinstance(r, Mapping)] if isinstance(results, list) else []


async def find_site(place: str) -> Site | None:
    """Search the web for the place's own website and verify it by name."""
    results = await _exa({"query": f"{place} official website menu nutrition", "type": "auto",
                          "numResults": SITE_RESULTS})
    return pick_official_site(place, results)


async def discover(site: Site, identity: str) -> list[str]:
    """Pages on the official site that may hold this item's panel: item pages
    first (the URL names the item), then nutrition pages and PDFs."""
    results = await _exa({"query": f"{site.place} {identity} nutrition facts calories", "type": "auto",
                          "numResults": DISCOVERY_RESULTS, "includeDomains": [site.domain]})
    urls: list[str] = []
    for result in results:
        url = result.get("url")
        if isinstance(url, str) and official_host(url, site) and url not in urls:
            urls.append(url)
    urls.sort(key=lambda url: 0 if identity_of(_slug_item(url), site.place) == identity else 1)
    return urls


async def render(site: Site, url: str, *, discovered_via: str) -> Candidate | None:
    """Render one official page or PDF to markdown (Firecrawl). No extraction
    is requested: the panel is read from the page in code."""
    response = await post_firecrawl_scrape(_key("FIRECRAWL_API_KEY"), {
        "url": url, "formats": ["markdown"], "onlyMainContent": True, "timeout": 30000,
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
    metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
    page = str(data.get("markdown") or "")
    if metadata.get("statusCode") not in (None, 200) or not page:
        return None
    return Candidate(source_url=str(metadata.get("url") or metadata.get("sourceURL") or url),
                     title=str(metadata.get("title") or metadata.get("ogTitle") or ""), page_text=page,
                     discovered_via=discovered_via, extracted_via="firecrawl_markdown")


async def ingest(site: Site, identity: str) -> Panel | None:
    """Candidate pages on the official site -> render -> validate. None when
    no official page yields a valid panel; provider outages raise."""
    reasons: list[str] = []
    for url in (await discover(site, identity))[:EXTRACT_ATTEMPTS]:
        candidate = await render(site, url, discovered_via="exa")
        if candidate is None:
            reasons.append("no_page")
            continue
        try:
            return validate(candidate, site, identity)
        except Rejected as exc:
            reasons.append(exc.reason)
    logger.info("official nutrition: no valid panel place=%s reasons=%s", site.slug, ",".join(reasons) or "none")
    return None


# --------------------------------------------------------------------------- #
# Resolution with reuse
# --------------------------------------------------------------------------- #

_misses: "OrderedDict[tuple[str, str], float]" = OrderedDict()
_sites: "OrderedDict[str, tuple[float, Site | None]]" = OrderedDict()
_inflight: dict[tuple[str, str], "asyncio.Task[dict[str, Any] | None]"] = {}

# Words that mean the phrase is a modified, sized, partial or multi-food order,
# never one published item name; it could only end as a mismatch, so it is
# not searched (no paid call).
_NOT_ONE_ITEM = re.compile(r"\b(?:and|with|without|no|plus|half|quarter|small|medium|large)\b")


def clear_process_caches() -> None:
    _misses.clear()
    _sites.clear()
    _inflight.clear()


async def wait_for_background() -> None:
    """Let in-flight ingests finish (tests and orderly shutdown)."""
    tasks = list(_inflight.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def revoked_ids() -> list[str] | None:
    """Evidence an operator withdrew: source record / observation ids (UUIDs)
    or a nutrition version's ``values_digest`` (64 hex), which also blocks the
    same panel from being stored again on any later day. A malformed list
    fails closed: None means no official record may be served at all."""
    from uuid import UUID
    raw = os.environ.get(REVOKED_IDS_FLAG, "").strip()
    revoked: list[str] = []
    for part in (piece.strip() for piece in raw.split(",")):
        if not part:
            continue
        if re.fullmatch(r"[0-9a-f]{64}", part.casefold()):
            revoked.append(part.casefold())
            continue
        try:
            revoked.append(str(UUID(part)))
        except ValueError:
            return None
    return revoked


def is_revoked(record: Mapping[str, Any]) -> bool:
    revoked = revoked_ids()
    return revoked is None or record["metadata"]["values_digest"] in revoked


def _monotonic() -> float:
    import time
    return time.monotonic()


def _bounded(cache: OrderedDict, key: Any, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > CACHE_MAX:
        cache.popitem(last=False)


def _recent_miss(key: tuple[str, str]) -> bool:
    at = _misses.get(key)
    if at is None:
        return False
    if _monotonic() - at > MISS_TTL_SECONDS:
        _misses.pop(key, None)
        return False
    return True


def _fresh(stored: Mapping[str, Any]) -> bool:
    retrieved = stored.get("retrieved_at")
    return isinstance(retrieved, datetime) and utcnow() - retrieved <= MAX_AGE


def _result(stored: Mapping[str, Any], site: Site, freshness: str) -> dict[str, Any]:
    """The resolver shape, built only from the persisted row."""
    retrieved = stored["retrieved_at"]
    checked = retrieved.date().isoformat() if isinstance(retrieved, datetime) else str(retrieved)
    return {
        "name": stored["display_name"],
        "macros_per_serving": {key: float(stored[key]) for key in NUTRIENTS},
        "serving_size": stored["serving_text"],
        "source": f"Curated restaurant: {site.place} official nutrition ({stored['source_url']}, checked {checked})",
        "customization": site.customization,
        "attribution": {
            "provider": site.provider, "external_id": stored["product_identity"],
            "verification_state": VERIFICATION_STATE, "cache_allowed": bool(stored["cache_allowed"]),
            "source_host": urlsplit(str(stored["source_url"])).hostname or "",
            "source_url": stored["source_url"],
            "food_item_id": str(stored["food_item_id"]), "observation_id": str(stored["observation_id"]),
            "source_record_id": str(stored["source_record_id"]),
            "retrieved_at": retrieved.isoformat() if isinstance(retrieved, datetime) else str(retrieved),
            "freshness": freshness, "customization": site.customization,
        },
    }


async def official_site(place: str, store) -> Site | None:
    """The place's verified official site: from this process, else from a
    stored official record of the same place, else found live (once)."""
    key = _compact(place)
    cached = _sites.get(key)
    if cached is not None and _monotonic() - cached[0] <= SITE_TTL_SECONDS:
        return cached[1]
    known = await store.fetch_official_domain(Site(place=place, domain="").provider)
    site = Site(place=place, domain=known) if known else await find_site(place)
    _bounded(_sites, key, (_monotonic(), site))
    return site


async def _ingest_and_save(place: str, identity: str, store, key: tuple[str, str]) -> dict[str, Any] | None:
    """One ingest for one identity, saved and read back. Any outcome other than
    a persisted record (no site, rejection, outage, timeout, store error,
    cancel) is remembered, so later meals do not pay for the same search."""
    saved = None
    try:
        site = panel = None
        try:
            site = await official_site(place, store)
            if site is None:
                logger.info("official nutrition: no official site found place=%s", key[0])
            else:
                panel = await asyncio.wait_for(ingest(site, identity), BACKGROUND_DEADLINE_SECONDS)
        except ProviderUnavailable as exc:
            logger.info("official nutrition: provider unavailable place=%s reason=%s", key[0], exc.reason)
        except (TimeoutError, httpx.HTTPError) as exc:
            logger.info("official nutrition: provider failed place=%s error=%s", key[0], type(exc).__name__)
        if site is not None and panel is not None:
            record = record_for(panel, site)
            if is_revoked(record):
                logger.info("official nutrition: revoked panel not stored place=%s", key[0])
                return None
            await store.save_official_observation(record)
            saved = await store.fetch_official_observation(site.provider, identity)
        return saved
    finally:
        if saved is None:
            _bounded(_misses, key, _monotonic())


async def lookup(query: str, *, store, place: str | None = None) -> dict[str, Any] | None:
    """Official nutrition for a restaurant item, reusing stored evidence first.

    None means "not handled here" (no restaurant, a phrase that is not one
    item name, no official site or panel, or a first-sighting ingest still
    running), and the caller's existing chain continues."""
    place = place or place_for(query)
    if not place or not _compact(place):
        return None
    identity = identity_of(query, place)
    if (not identity or len(identity.split()) > MAX_IDENTITY_WORDS or re.match(r"^\d", identity)
            or "," in query or _NOT_ONE_ITEM.search(identity)):
        return None  # never searched as a name
    hint = Site(place=place, domain="")
    stored = await store.fetch_official_observation(hint.provider, identity)
    if stored is not None and _fresh(stored):
        return _result(stored, hint, "fresh")
    stale = _result(stored, hint, "stale") if stored is not None else None
    key = (hint.slug, identity)
    if _recent_miss(key):
        return stale
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_ingest_and_save(place, identity, store, key))
        _inflight[key] = task

        def _done(finished, key=key):
            if _inflight.get(key) is finished:
                _inflight.pop(key, None)
            # A meal that stopped waiting never awaits this task: consume and
            # log its failure here instead of "exception was never retrieved".
            if not finished.cancelled() and finished.exception() is not None:
                logger.warning("official nutrition: background ingest failed place=%s error=%s",
                               key[0], type(finished.exception()).__name__)
        task.add_done_callback(_done)
    try:
        # shield: a meal's own deadline never cancels the ingest it started.
        saved = await asyncio.wait_for(asyncio.shield(task), INGEST_DEADLINE_SECONDS)
    except TimeoutError:
        return stale
    return _result(saved, hint, "fresh") if saved is not None else stale
