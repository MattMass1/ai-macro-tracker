"""``find_food``: the food agent's lookup tool, wrapping the existing resolvers.

Reuse, not rewrite. The cascade (presets, tenant catalog, CoFID, generic
whole foods, FatSecret, web estimate) stays in ``server.resolve_food`` /
``food_lookup.resolve_food``. This module adds what the agent needs on top:

* The resolver is asked about the food IDENTITY only (brand, name, variant,
  preparation). Portion words never reach the query, so "half" or "6 oz
  instead of 12" cannot be searched as part of a name.
* Identity guards: the candidate must agree with what was asked on brand and
  on distinguishing tokens (cheese, double, deluxe, zero sugar, flavor, raw vs
  cooked). A different variant is never a match. A cosmetic difference
  (spelling, punctuation, plural) is a match reported as "matched to X".
* The resolver dict becomes a :class:`ResolvedFood` with a per-serving (or
  per-100) panel where a missing nutrient is ``None``, never 0, and the
  source's own serving basis, so portion math happens once, in
  :func:`food_agent.portion.multiplier`.
* Questions are one plain question. Never a numbered list, never for
  something the source already states.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from difflib import SequenceMatcher
from typing import Any

from food_agent import portion as portion_math
from food_agent.contracts import (NUTRIENT_KEYS, FoodIdentity, NutrientPanel, Portion, ResolvedFood,
                                  ServingBasis)
from food_agent.ports import FindResult

logger = logging.getLogger(__name__)

Resolver = Callable[[str], Awaitable[Mapping[str, Any] | None]]
Suggester = Callable[[str], Awaitable[Sequence[str]]]
AmbiguityProbe = Callable[[str], bool]


# ---------------------------------------------------------------- production defaults

ROUTE_KIND: ContextVar[str | None] = ContextVar("food_route_kind", default=None)


async def production_resolve(query: str) -> Mapping[str, Any] | None:
    """The live resolver: ``server.resolve_food`` (tenant presets and catalog,
    CoFID, generic foods, FatSecret, web estimate) gated by the same
    ``_trusted_food`` check the existing write path uses.

    ``server`` is imported lazily so importing this module has no server
    side effects. ``atomic=True``: the agent already split the meal into
    items, so one identity is never split again on "and" or commas. The
    item's kind (set by :class:`FoodFinder`) routes the source lane.
    """
    import server  # noqa: PLC0415 (lazy on purpose)

    found = await server.resolve_food(query, atomic=True, kind=ROUTE_KIND.get())
    return found if server._trusted_food(found) else None


async def production_suggest(query: str) -> Sequence[str]:
    """Closest provider names for a query the strict resolver did not accept."""
    import food_lookup  # noqa: PLC0415

    if food_lookup.route_kind(query, ROUTE_KIND.get()) not in food_lookup.BRANDED_KINDS:
        return []
    return await food_lookup.fatsecret_name_options(query)


def production_is_ambiguous(query: str) -> bool:
    """Whether the last provider search for ``query`` ended in close variants."""
    import food_lookup  # noqa: PLC0415

    return food_lookup.recently_ambiguous(query)


# ---------------------------------------------------------------- text helpers

_MEASURE_UNITS = (r"fl\.?\s*oz|fluid\s+ounces?|oz|ounces?|g|grams?|gr|kg|kilograms?|lbs?|pounds?|"
                  r"ml|millilit(?:er|re)s?|l|lit(?:er|re)s?|cups?|tbsp|tablespoons?|tsp|teaspoons?")
_SIZE_RE = re.compile(rf"\b\d+(?:\.\d+)?\s*(?:{_MEASURE_UNITS})(?![a-z])", re.IGNORECASE)
_TRAILING_SIZE_RE = re.compile(rf"\s*[-,]\s*\d+(?:\.\d+)?\s*(?:{_MEASURE_UNITS})\.?\s*$", re.IGNORECASE)
_PAREN_RE = re.compile(r"\(([^)]*)\)")
_PORTION_PAREN_RE = re.compile(
    rf"\d+(?:\.\d+)?\s*(?:{_MEASURE_UNITS})(?![a-z])|\b(?:half|quarter|third|instead)\b", re.IGNORECASE)
_LEADING_PORTION_RE = re.compile(
    r"^\s*(?:(?:about|around|roughly|approximately|maybe)\s+)?"
    r"(?:(?:a\s+|one\s+)?(?:half|quarter|third)\s+(?:of\s+)?(?:an?\s+|the\s+|my\s+)|"
    r"(?:half|quarter|third)\s+of\s+|"
    r"\d+\s*/\s*\d+\s+(?:of\s+)?(?:an?\s+|the\s+)?|"
    rf"\d+(?:\.\d+)?\s*(?:{_MEASURE_UNITS})(?![a-z])\s*(?:of\s+)?(?:an?\s+|the\s+)?)",
    re.IGNORECASE)

_CONNECTIVES = frozenset({"and", "with", "w", "of", "a", "an", "the", "on", "in", "plus"})
_BRAND_FILLER = frozenset({"the", "inc", "co", "llc", "restaurant", "restaurants", "cafe"})
_STATE_WORDS = frozenset({"raw", "fresh", "uncooked"})
_COOKED_WORDS = frozenset({
    "cooked", "grilled", "fried", "baked", "roasted", "boiled", "steamed", "sauteed", "smoked",
    "broiled", "poached", "scrambled", "seared", "toasted", "braised", "stewed", "rotisserie",
    "panfried", "airfried", "microwaved", "barbecued", "bbq",
})
# Words naming what kind of thing an item is, not which one. Missing on one
# side is cosmetic ("Choco Laka smoothie" vs the provider's "Choc-A-Laka").
_CATEGORY_WORDS = frozenset({
    "smoothie", "sandwich", "burger", "drink", "beverage", "shake", "bowl", "meal", "item",
    "entree", "dish", "food", "product", "snack", "bar", "soda", "juice", "coffee", "latte",
})
# Tokens that make a different product. A difference here is never cosmetic.
_VARIANT_MARKERS = frozenset({
    "double", "triple", "single", "quad", "cheese", "deluxe", "zero", "diet", "light", "lite",
    "sugar", "free", "sugarfree", "unsweetened", "sweetened", "bacon", "spicy", "mild", "jr",
    "junior", "mini", "small", "medium", "large", "big", "kid", "kids", "decaf", "plain",
    "original", "classic", "supreme", "ultimate", "crispy", "nugget", "tender", "strip",
    "chocolate", "vanilla", "strawberry", "banana", "caramel", "mocha", "peanut", "mint",
    "lemon", "orange", "cherry", "berry", "blueberry", "raspberry", "coconut", "cinnamon",
    "whole", "skim", "fat", "reduced", "lowfat", "nonfat", "protein", "keto", "vegan",
    "beyond", "impossible", "turkey", "beef", "pork", "chicken", "fish", "steak", "shrimp",
    "veggie", "egg", "sausage", "ham",
}) | _STATE_WORDS | _COOKED_WORDS
# Foods whose weight changes a lot with cooking: a bare weight is ambiguous.
_COOKABLE = frozenset({
    "chicken", "beef", "steak", "pork", "turkey", "lamb", "veal", "bison", "venison", "duck",
    "salmon", "fish", "cod", "tilapia", "halibut", "trout", "shrimp", "prawn", "mince", "ground",
    "rice", "pasta", "spaghetti", "penne", "macaroni", "noodle", "quinoa", "oat", "oatmeal",
    "couscous", "lentil", "bean", "chickpea", "potato", "barley", "bulgur", "farro",
})
_CURATED_STATES = frozenset({"official_source_exact_row", "official_curated", "internal_curated"})
# Variety and ripeness words describe a whole food, never a different one
# ("green apple" is an apple). Only consulted for unbranded curated rows.
_DESCRIPTOR_WORDS = frozenset({"green", "red", "yellow", "golden", "pink", "ripe", "organic", "fresh"})
_NUMBERED_LIST = re.compile(r"(^|\s)\d+\s*[.)](\s|$)")


def _squash(text: Any) -> str:
    return " ".join(str(text or "").split())


def _strip_apostrophes(text: str) -> str:
    return re.sub(r"[’'`]", "", text)


def _stem(token: str) -> str:
    if any(ch.isdigit() for ch in token):
        return token
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "sses", "xes", "zes", "oes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us")):
        return token[:-1]
    return token


def _words(text: Any) -> list[str]:
    text = _strip_apostrophes(str(text or "").casefold()).replace("&", " and ")
    return re.findall(r"[a-z0-9]+", text)


def _tokens(text: Any) -> set[str]:
    return {_stem(word) for word in _words(_SIZE_RE.sub(" ", str(text or "")))
            if word not in _CONNECTIVES}


def _compact(text: Any) -> str:
    return "".join(word for word in _words(text) if word not in _BRAND_FILLER)


def _clean_name(text: Any) -> str:
    """A food name without portion words: leading amounts, measure-bearing
    parentheticals and trailing size suffixes ("Choc-A-Laka - 12oz")."""
    name = _squash(text)
    name = _PAREN_RE.sub(lambda m: " " if _PORTION_PAREN_RE.search(m.group(1)) else m.group(0), name)
    previous = None
    while previous != name:
        previous = name
        name = _LEADING_PORTION_RE.sub("", name, count=1)
    name = _TRAILING_SIZE_RE.sub("", name)
    name = _SIZE_RE.sub(" ", name)
    return _squash(name).strip(" -,;:")


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and _compact(needle) in _compact(haystack)


def identity_query(identity: FoodIdentity) -> str:
    """The resolver query for an identity: brand, preparation, name, variant.

    Never carries portion words, even when a model put them in the name.
    """
    name = _clean_name(identity.name) or _squash(identity.name)
    parts: list[str] = []
    brand = _squash(identity.brand)
    if brand and not _contains(name, brand):
        parts.append(brand)
    preparation = _clean_name(identity.preparation)
    if preparation and not (_tokens(preparation) <= _tokens(name)):
        parts.append(preparation)
    parts.append(name)
    variant = _clean_name(identity.variant)
    if variant and not (_tokens(variant) <= _tokens(name)):
        parts.append(variant)
    return _squash(" ".join(parts))


def _asked_display(identity: FoodIdentity) -> str:
    return identity_query(identity)


def _one_question(text: str) -> str:
    """Guarantee one plain question: no numbered list, no line breaks."""
    text = _squash(text)
    if _NUMBERED_LIST.search(text):  # defensive: never ship a numbered list
        text = _NUMBERED_LIST.sub(" ", text)
    return text[:300]


# ---------------------------------------------------------------- resolver dict -> ResolvedFood

def _nutrient(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or number > 100000:
        return None
    return round(number, 2)


def _panel(values: Any) -> NutrientPanel | None:
    if not isinstance(values, Mapping):
        return None
    panel = NutrientPanel(**{key: _nutrient(values.get(key)) for key in NUTRIENT_KEYS})
    return panel if panel.calories is not None and panel.calories > 0 else None


def _provider(found: Mapping[str, Any]) -> str:
    attribution = found.get("attribution") if isinstance(found.get("attribution"), Mapping) else {}
    provider = _squash(attribution.get("provider"))
    if not provider:
        source = _squash(found.get("source"))
        provider = source.split(":", 1)[0].strip() if ":" in source else ""
    return (provider or "unknown")[:40]


def _external_id(found: Mapping[str, Any]) -> str | None:
    attribution = found.get("attribution") if isinstance(found.get("attribution"), Mapping) else {}
    external = _squash(attribution.get("external_id"))
    return external[:150] or None


def _brand(found: Mapping[str, Any]) -> str:
    return _squash(found.get("brand_name"))


def _display_name(found: Mapping[str, Any]) -> str:
    name = _clean_name(found.get("name")) or _squash(found.get("name")) or "food"
    brand = _brand(found)
    if brand and not _contains(name, brand):
        name = f"{brand} {name}"
    return name[:200]


def _generic_basis(found: Mapping[str, Any]) -> ServingBasis | None:
    """Local generic rows label the serving as "<source> - 52.7 g"."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*g\s*$", _squash(found.get("serving_size")))
    if not match:
        return None
    grams = float(match.group(1))
    if isinstance(found.get("count_grams"), Mapping):
        noun = _squash(found.get("name")) or "item"
        return portion_math.parse_serving_text(f"1 {noun} ({grams:g} g)")
    return portion_math.parse_serving_text(f"{grams:g} g")


def _basis_and_panel(found: Mapping[str, Any]) -> tuple[ServingBasis, NutrientPanel] | None:
    applied = found.get("applied_quantity")
    per_serving = _panel(found.get("macros_per_serving"))
    per_100g = _panel(found.get("macros_per_100g"))
    if applied not in (None, 1, 1.0):
        # Already scaled by the resolver; only an unscaled per-100 g panel is safe.
        return (portion_math.basis_per_100g(), per_100g) if per_100g else None
    if per_serving is None:
        return (portion_math.basis_per_100g(), per_100g) if per_100g else None
    basis = None
    if str(found.get("source") or "").startswith("Generic:"):
        basis = _generic_basis(found)
    if basis is None:
        basis = portion_math.basis_from_resolver(found)
    if basis.basis == "per_100g":
        return (basis, per_100g) if per_100g else None
    return basis, per_serving


def _stable_id(provider: str, found: Mapping[str, Any], basis: ServingBasis) -> str:
    key = "|".join((provider.casefold(), " ".join(_words(found.get("name"))),
                    " ".join(_words(_brand(found))), basis.basis, basis.text.casefold()))
    return f"{provider}:h{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"


def to_resolved_food(found: Mapping[str, Any]) -> ResolvedFood | None:
    """Convert one resolver result to a :class:`ResolvedFood`, or ``None`` if unusable."""
    shaped = _basis_and_panel(found)
    if shaped is None:
        return None
    basis, panel = shaped
    provider = _provider(found)
    external_id = _external_id(found)
    food_id = f"{provider}:{external_id}" if external_id else _stable_id(provider, found, basis)
    macro_source = _squash(found.get("source")) or provider
    if found.get("estimate_provenance") and "estimate" not in macro_source.casefold():
        macro_source = f"Estimate: {macro_source}"
    from food_agent.source_policy import permits_cache, policy_only
    attribution = policy_only(dict(found.get("attribution") or {}))
    for key, value in found.items():
        if "retention" in key or key in ("expires_at", "cache_expires_at", "cache_allowed"):
            attribution.setdefault(key, value)
    nested = policy_only(list(found.get("component_attributions") or []) +
                         list(found.get("component_metadata") or []))
    allowed = permits_cache(found) and permits_cache({"provider": provider, **attribution})
    return ResolvedFood(food_id=food_id[:200], provider=provider, external_id=external_id,
                        cache_allowed=allowed, attribution=attribution, component_attributions=nested,
                        display_name=_display_name(found), macro_source=macro_source[:200],
                        serving_basis=basis, panel=panel)


def with_serving_unit(food: ResolvedFood | None, name: str, portion: Portion) -> ResolvedFood | None:
    """A per-100 g (or per-100 ml) panel for a food with a serving unit, counted in that unit
    ("1 slice (29 g)", "1 cup") at its pinned typical weight, so "a slice" is never a "how much?"
    question with ounce choices. Anything else is returned unchanged."""
    import serving_units  # noqa: PLC0415 (pure module, kept beside its callers)
    if food is None or food.serving_basis.basis not in ("per_100g", "per_100ml"):
        return food
    family = serving_units.for_food(name)
    if family is None or not serving_units.applies(family, portion.text, portion.kind, portion.unit):
        return food
    if food.serving_basis.basis == "per_100ml":
        if not family.drink:
            return food
        factor = float(portion_math.ML_PER[family.unit]) / 100.0
    else:
        factor = family.grams / 100.0
    values = {key: getattr(food.panel, key) for key in NUTRIENT_KEYS}
    panel = NutrientPanel(**{key: None if value is None else round(value * factor, 2) for key, value in values.items()})
    basis = portion_math.parse_serving_text(serving_units.serving_text(family, family.grams))
    attribution = {**food.attribution, "serving_basis": "per_serving",
                   **serving_units.attribution(family, family.grams, None)}
    return food.model_copy(update={"serving_basis": basis, "panel": panel, "attribution": attribution})


def is_estimate(food: ResolvedFood) -> bool:
    """Whether the preview must label this food's numbers as an estimate."""
    return "estimate" in food.macro_source.casefold()


# ---------------------------------------------------------------- identity guards

def _cook_class(tokens: set[str]) -> str | None:
    if tokens & _STATE_WORDS:
        return "raw"
    if tokens & _COOKED_WORDS:
        return "cooked"
    return None


def _asked_cook(identity: FoodIdentity) -> str | None:
    words = " ".join(filter(None, (identity.name, identity.variant, identity.preparation)))
    return _cook_class(_tokens(words))


def _brand_agrees(asked_brand: str, cand_brand: str, cand_name: str) -> bool:
    asked = _compact(asked_brand)
    if not asked:
        return True
    cand = _compact(cand_brand)
    if cand:
        return asked in cand or cand in asked
    return asked in _compact(cand_name)  # name options carry the brand in the label


def _cosmetic(asked_left: set[str], cand_left: set[str]) -> bool:
    """Remaining tokens differ only in spelling ("choco" vs "choc")."""
    if (asked_left | cand_left) & _VARIANT_MARKERS:
        return False
    asked_left -= _CATEGORY_WORDS
    cand_left -= _CATEGORY_WORDS
    if not asked_left and not cand_left:
        return True
    if not asked_left or not cand_left:
        return False
    left, right = "".join(sorted(asked_left)), "".join(sorted(cand_left))
    return min(len(left), len(right)) >= 3 and SequenceMatcher(None, left, right).ratio() >= 0.8


def _curated_generic(found: Mapping[str, Any]) -> bool:
    attribution = found.get("attribution") if isinstance(found.get("attribution"), Mapping) else {}
    source = str(found.get("source") or "")
    return (not _brand(found)
            and (source.startswith(("Generic:", "CoFID")) or "BOUNDED ESTIMATE" in source
                 or attribution.get("verification_state") in _CURATED_STATES))


def _identity_check(identity: FoodIdentity, cand_name: str, cand_brand: str,
                    found: Mapping[str, Any] | None = None) -> str:
    """``"ok"``, ``"brand"`` (different brand), ``"cook"`` (raw vs cooked
    panel) or ``"variant"`` (different product)."""
    if identity.brand and not _brand_agrees(identity.brand, cand_brand, cand_name):
        return "brand"
    brand_tokens = _tokens(identity.brand) | _tokens(cand_brand)
    asked = _tokens(" ".join(filter(None, (_clean_name(identity.name), _clean_name(identity.variant),
                                           _clean_name(identity.preparation))))) - brand_tokens
    cand = _tokens(_clean_name(cand_name)) - brand_tokens
    cook_text = " ".join(filter(None, (cand_name, str((found or {}).get("basis") or ""))))
    asked_cook, cand_cook = _cook_class(asked), _cook_class(_tokens(cook_text))
    if asked_cook and cand_cook and asked_cook != cand_cook:
        return "cook"
    if found is not None and _provider(found) == "USDA FDC":
        import usda_lookup
        return "ok" if usda_lookup.compatible(identity_query(identity), cand_name) else "variant"
    if found is not None and _curated_generic(found) and not identity.brand:
        # Curated generic rows add descriptors ("chicken egg", "farmed
        # salmon"); everything asked must still be there.
        cook_words = _STATE_WORDS | _COOKED_WORDS
        missing = (asked - cook_words - _DESCRIPTOR_WORDS) - cand
        return "ok" if not missing or _cosmetic(missing, (cand - cook_words) - asked) else "variant"
    asked = asked - _STATE_WORDS if asked_cook != "raw" else asked
    cand = cand - _STATE_WORDS if asked_cook != "raw" else cand
    if asked == cand:
        return "ok"
    return "ok" if _cosmetic(asked - cand, cand - asked) else "variant"


# ---------------------------------------------------------------- questions

_UNIT_WORDS = {"fl_oz": "fl oz", "ml": "ml", "l": "liters", "cup": "cups", "tbsp": "tablespoons",
               "tsp": "teaspoons", "g": "grams", "kg": "kg", "oz": "oz", "lb": "lb"}


def _amount_question(food: ResolvedFood, portion: Portion) -> str:
    basis = food.serving_basis
    name = food.display_name
    if basis.basis == "per_100ml":
        return _one_question(f"How many ml of {name} did you have?")
    if basis.basis == "per_100g":
        return _one_question(f"How many grams of {name} did you have?")
    unit = basis.unit if basis.unit and basis.unit != "count" else ("ml" if basis.ml else "g" if basis.grams else None)
    serving = f" One serving is {basis.text}." if basis.text else ""
    if unit is None:
        return _one_question(f"How many servings of {name} was that?{serving}")
    return _one_question(f"How many {_UNIT_WORDS.get(unit, unit)} of {name} was that?{serving}")


def _needs_raw_or_cooked(identity: FoodIdentity, portion: Portion, found: Mapping[str, Any]) -> bool:
    """Raw/cooked matters only for a weighed, unbranded, cookable food."""
    if portion.kind != "mass" or identity.kind in ("restaurant", "packaged"):
        return False
    if identity.brand or _brand(found):
        return False
    asked = _tokens(" ".join(filter(None, (identity.name, identity.variant, identity.preparation))))
    if _cook_class(asked) is not None:
        return False
    return bool(asked & _COOKABLE)


# ---------------------------------------------------------------- the finder

class FoodFinder:
    """Implements :class:`food_agent.ports.FoodFinder` over the existing resolvers.

    ``resolve(query) -> dict | None`` is the resolver (default
    :func:`production_resolve`). ``suggest(query) -> [names]`` lists close
    provider names when the strict resolver returns nothing (default
    :func:`production_suggest`). ``is_ambiguous(query)`` says whether the last
    provider search for the query ended in close variants (default
    :func:`production_is_ambiguous`). Tests inject fakes for all three.
    """

    def __init__(self, resolve: Resolver | None = None, *, suggest: Suggester | None = None,
                 is_ambiguous: AmbiguityProbe | None = None) -> None:
        self._resolve = resolve or production_resolve
        self._suggest = suggest or production_suggest
        self._is_ambiguous = is_ambiguous or production_is_ambiguous

    async def _lookup(self, query: str) -> Mapping[str, Any] | None:
        try:
            found = await self._resolve(query)
        except Exception:  # noqa: BLE001 - provider failure is "not found", never a crash
            logger.exception("find_food: resolver failed")
            return None
        return found if isinstance(found, Mapping) else None

    async def _options(self, query: str) -> list[str]:
        try:
            return [_squash(option) for option in (await self._suggest(query) or []) if _squash(option)]
        except Exception:  # noqa: BLE001
            logger.exception("find_food: name options failed")
            return []

    def _ambiguous(self, query: str) -> bool:
        try:
            return bool(self._is_ambiguous(query))
        except Exception:  # noqa: BLE001
            return False

    async def find(self, identity: FoodIdentity, portion: Portion) -> FindResult:
        query = identity_query(identity)
        asked = _asked_display(identity)
        import food_lookup
        kind = "packaged" if identity.brand and identity.kind in food_lookup.WHOLE_FOOD_KINDS else identity.kind
        lane = food_lookup.route_kind(query, kind)
        token = ROUTE_KIND.set(lane)
        try:
            found = await self._lookup(query)
            if found is None:
                if lane not in food_lookup.BRANDED_KINDS:
                    return FindResult("no_match", None, note=f"I couldn't find nutrition for {asked}.")
                return await self._from_options(identity, portion, query, asked)
        finally:
            ROUTE_KIND.reset(token)
        return self._judge(identity, portion, found, asked)

    @staticmethod
    def _closest(identity: FoodIdentity, options: list[str]) -> list[str]:
        """Options ordered by how little they differ from what was asked."""
        brand_tokens = _tokens(identity.brand)
        asked = _tokens(" ".join(filter(None, (identity.name, identity.variant)))) - brand_tokens

        def distance(option: str) -> tuple[int, int]:
            cand = _tokens(option) - brand_tokens
            return (len(asked - cand), len(cand - asked))
        return sorted(options, key=distance)

    async def _from_options(self, identity: FoodIdentity, portion: Portion, query: str,
                            asked: str) -> FindResult:
        """The user named the product: take the closest real one and SAY which.
        Never "Did you mean X?" or "Was it X or Y?"; a wrong pick costs one correction."""
        options = await self._options(query)
        if identity.brand:
            options = [option for option in options if _brand_agrees(identity.brand, "", option)]
        cosmetic = [option for option in options if _identity_check(identity, option, "") == "ok"]
        ranked = self._closest(identity, cosmetic)
        for option in ranked[:2]:
            found = await self._lookup(option)
            if found is None:
                continue
            judged = self._judge(identity, portion, found, asked)
            if judged.status != "no_match":
                return judged
        return FindResult("no_match", None, note=f"I couldn't find nutrition for {asked}.")

    def _judge(self, identity: FoodIdentity, portion: Portion, found: Mapping[str, Any],
               asked: str) -> FindResult:
        if _provider(found) == "USDA FDC":
            import usda_lookup
            found = usda_lookup.for_portion(dict(found), identity_query(identity), portion.text, portion.kind,
                                             portion.unit)
        food = with_serving_unit(to_resolved_food(found), identity_query(identity), portion)
        if food is None:
            return FindResult("no_match", None,
                              note=f"I couldn't find usable nutrition for {asked}.")
        verdict = _identity_check(identity, _squash(found.get("name")), _brand(found), found)
        if verdict == "brand":
            return FindResult("no_match", None,
                              note=f"I only found {food.display_name}, not {asked}.")
        if verdict == "cook":
            said = "cooked" if _asked_cook(identity) == "cooked" else "raw"
            other = "raw" if said == "cooked" else "cooked"
            return FindResult("no_match", None, note=(
                f"I only found {other} nutrition for {food.display_name}, not {said}."))
        if verdict == "variant":
            return FindResult("no_match", None, note=f"I couldn't match {asked} without changing the food.")
        note = f"matched to {food.display_name}"
        if _needs_raw_or_cooked(identity, portion, found):
            amount = _squash(portion.text) or f"{portion.amount:g} {portion.unit}"
            return FindResult("needs_detail", None, note=note, question=_one_question(
                f"Was the {amount} of {_clean_name(identity.name)} weighed raw or cooked?"))
        if portion_math.multiplier(portion, food.serving_basis, identity) is None:
            return FindResult("needs_detail", food, note=note,
                              question=_amount_question(food, portion))
        return FindResult("matched", food, note=note)


__all__ = ["FoodFinder", "ROUTE_KIND", "identity_query", "to_resolved_food", "with_serving_unit", "is_estimate",
           "production_resolve", "production_suggest", "production_is_ambiguous"]
