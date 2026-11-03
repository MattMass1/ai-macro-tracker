"""Pure portion math: how many servings of the source a user portion is.

No I/O. Decimal arithmetic. ``None`` means "cannot know, ask"; it is never
silently turned into 1 serving or into 0 nutrients.

Rules (Matthew's):
* Fractions and multiples of a serving always work (0.5 x a sandwich).
* A bare "oz" from the user against a fluid-ounce serving is fluid ounces of
  that serving ("6 oz" of a "12 fl oz" smoothie = 0.5). Against a mass basis
  it is mass.
* Mass against a volume-only basis (or the reverse) has no density: ``None``.
* An unspecified amount is one serving only when the source defines a single
  serving (not for per-100 g / per-100 ml panels).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from food_agent.contracts import (MASS_UNITS, NUTRIENT_KEYS, VOLUME_UNITS, FoodIdentity, NutrientPanel,
                                  Portion, ServingBasis)

GRAMS_PER: dict[str, Decimal] = {
    "g": Decimal("1"), "kg": Decimal("1000"), "oz": Decimal("28.349523125"), "lb": Decimal("453.59237"),
}
ML_PER: dict[str, Decimal] = {
    "ml": Decimal("1"), "l": Decimal("1000"), "fl_oz": Decimal("29.5735295625"),
    "cup": Decimal("236.5882365"), "tbsp": Decimal("14.78676478125"), "tsp": Decimal("4.92892159375"),
}
_MULTIPLIER_STEP = Decimal("0.0001")
_NUTRIENT_STEP = Decimal("0.1")

_UNIT_ALIASES: dict[str, str] = {
    "fluid ounces": "fl_oz", "fluid ounce": "fl_oz", "fl. oz.": "fl_oz", "fl. oz": "fl_oz",
    "fl oz": "fl_oz", "fl_oz": "fl_oz", "floz": "fl_oz",
    "ounces": "oz", "ounce": "oz", "oz": "oz",
    "grams": "g", "gram": "g", "gr": "g", "g": "g",
    "kilograms": "kg", "kilogram": "kg", "kg": "kg",
    "pounds": "lb", "pound": "lb", "lbs": "lb", "lb": "lb",
    "milliliters": "ml", "millilitres": "ml", "milliliter": "ml", "millilitre": "ml", "ml": "ml",
    "liters": "l", "litres": "l", "liter": "l", "litre": "l", "l": "l",
    "cups": "cup", "cup": "cup", "c": "cup",
    "tablespoons": "tbsp", "tablespoon": "tbsp", "tbsp": "tbsp", "tbs": "tbsp",
    "teaspoons": "tsp", "teaspoon": "tsp", "tsp": "tsp",
}
_UNIT_RE = "|".join(re.escape(alias) for alias in sorted(_UNIT_ALIASES, key=len, reverse=True))
_QTY_RE = r"(?P<qty>\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:\.\d+)?|\.\d+)"
_LEADING = re.compile(rf"^\s*{_QTY_RE}\s*(?P<rest>.*)$", re.IGNORECASE)
_MEASURE = re.compile(rf"{_QTY_RE}\s*(?P<unit>{_UNIT_RE})(?![a-z])", re.IGNORECASE)
_UNIT_AT_START = re.compile(rf"^(?P<unit>{_UNIT_RE})(?![a-z])", re.IGNORECASE)
_PAREN = re.compile(r"\(([^)]*)\)")


def _qty(text: str) -> Decimal | None:
    text = re.sub(r"\s+", " ", text.strip())
    try:
        if " " in text and "/" in text:  # 1 1/2
            whole, frac = text.split(" ", 1)
            num, den = frac.replace(" ", "").split("/")
            return Decimal(whole) + Decimal(num) / Decimal(den)
        if "/" in text:
            num, den = text.replace(" ", "").split("/")
            return Decimal(num) / Decimal(den)
        return Decimal(text)
    except (InvalidOperation, ZeroDivisionError, ValueError):
        return None


def _positive(value: Decimal | None) -> float | None:
    if value is None or not value.is_finite() or value <= 0:
        return None
    return float(value)


def _measure(text: str) -> tuple[Decimal, str] | None:
    """First ``<qty> <unit>`` inside ``text``, unit normalized."""
    match = _MEASURE.search(text)
    if not match:
        return None
    qty = _qty(match["qty"])
    if qty is None or qty <= 0:
        return None
    return qty, _UNIT_ALIASES[match["unit"].lower()]


def parse_serving_text(text: str | None, basis: str = "per_serving") -> ServingBasis:
    """Parse a source serving description into a :class:`ServingBasis`.

    ``"12 oz"`` -> 12 oz (mass); ``"12 fl oz"`` -> 12 fl_oz; ``"1 sandwich (202 g)"``
    -> 1 count of "sandwich" weighing 202 g; ``"1 cup (240 ml)"`` -> 1 cup = 240 ml.
    Unparseable text keeps the text with no amount or unit.
    """
    raw = (text or "").strip()[:200]
    amount: Decimal | None = None
    unit: str | None = None
    noun: str | None = None
    grams: Decimal | None = None
    ml: Decimal | None = None

    outer = _PAREN.sub(" ", raw).strip()
    leading = _LEADING.match(outer)
    if leading:
        amount = _qty(leading["qty"])
        rest = leading["rest"].strip()
        at_start = _UNIT_AT_START.match(rest)
        if at_start:
            unit = _UNIT_ALIASES[at_start["unit"].lower()]
        else:
            unit = "count"
            noun = re.sub(r"\s+", " ", rest).strip(" ,.-") or None
    else:
        found = _measure(outer)
        if found:
            amount, unit = found

    for inner in _PAREN.findall(raw):
        found = _measure(inner)
        if not found:
            continue
        qty, inner_unit = found
        if inner_unit in GRAMS_PER and grams is None:
            grams = qty * GRAMS_PER[inner_unit]
        elif inner_unit in ML_PER and ml is None:
            ml = qty * ML_PER[inner_unit]

    if amount is not None and unit in GRAMS_PER and grams is None:
        grams = amount * GRAMS_PER[unit]
    if amount is not None and unit in ML_PER and ml is None:
        ml = amount * ML_PER[unit]
    if _positive(amount) is None:
        amount, unit, noun = None, None, None
    return ServingBasis(text=raw, basis=basis, amount=_positive(amount), unit=unit,
                        count_noun=noun[:80] if noun else None, grams=_positive(grams), ml=_positive(ml))


def basis_per_100g() -> ServingBasis:
    return ServingBasis(text="100 g", basis="per_100g", amount=100.0, unit="g", grams=100.0)


def basis_per_100ml() -> ServingBasis:
    return ServingBasis(text="100 ml", basis="per_100ml", amount=100.0, unit="ml", ml=100.0)


def basis_from_resolver(found: Mapping[str, Any]) -> ServingBasis:
    """Serving basis for an existing resolver result (``food_lookup.resolve_food`` shape).

    ``attribution.serving_basis == "per_100g"`` with no per-serving panel is a
    per-100 g basis; otherwise the ``serving_size`` text is parsed, and a
    known ``attribution.serving_grams`` fills in the gram weight.
    """
    attribution = found.get("attribution") if isinstance(found.get("attribution"), Mapping) else {}
    if attribution.get("serving_basis") == "per_100g" and not found.get("macros_per_serving"):
        return basis_per_100g()
    parsed = parse_serving_text(str(found.get("serving_size") or ""))
    grams = attribution.get("serving_grams")
    if parsed.grams is None and isinstance(grams, (int, float)) and not isinstance(grams, bool) and grams > 0:
        parsed = parsed.model_copy(update={"grams": float(grams)})
    return parsed


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(repr(value))


def _result(value: Decimal) -> Decimal | None:
    if not value.is_finite():
        return None
    value = value.quantize(_MULTIPLIER_STEP, rounding=ROUND_HALF_UP)
    return value if value > 0 else None


def _basis_grams(basis: ServingBasis) -> Decimal | None:
    if basis.grams is not None:
        return _dec(basis.grams)
    if basis.amount is not None and basis.unit in MASS_UNITS:
        return _dec(basis.amount) * GRAMS_PER[basis.unit]
    return None


def _basis_ml(basis: ServingBasis) -> Decimal | None:
    if basis.ml is not None:
        return _dec(basis.ml)
    if basis.amount is not None and basis.unit in VOLUME_UNITS:
        return _dec(basis.amount) * ML_PER[basis.unit]
    return None


_NOUN_IGNORE = frozenset({
    "a", "an", "the", "of", "and", "or", "my", "about", "around", "like", "just", "only", "x",
    "half", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "dozen", "couple", "few", "several", "large", "small", "medium", "big", "little",
    "mini", "whole", "extra", "regular",
})


def _stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("xes", "ches", "shes", "sses", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 2:
        return word[:-1]
    return word


def _noun_tokens(text: str | None) -> set[str]:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return {_stem(word) for word in words if word not in _NOUN_IGNORE}


# Count units that mean the same thing: "6 nuggets" against a "4 pieces" serving.
_UNIT_EQUIVALENTS: dict[str, frozenset[str]] = {
    "piece": frozenset({"piece", "pc", "nugget", "tender", "wing", "dumpling", "bite", "strip", "ball", "chunk"}),
    "slice": frozenset({"slice", "rasher"}),
}
# Foods counted in a unit by default, used only when the user's words carry no
# unit ("three" bacon against a "2 slices" serving).
_NATURAL_UNIT: dict[str, frozenset[str]] = {
    "slice": frozenset({"bacon", "bread", "toast", "cheese", "ham", "turkey", "salami", "pepperoni"}),
}
# Whole items: a count of these is whole items, never slices or pieces.
_WHOLE_ITEMS = frozenset({
    "pizza", "sandwich", "burger", "cheeseburger", "hamburger", "wrap", "bowl", "sub", "hoagie", "burrito",
    "quesadilla", "flatbread", "pita", "bagel", "muffin", "bun", "roll", "panini", "melt", "calzone",
    "salad", "plate", "platter", "meal", "combo", "box", "whopper", "pie", "cake", "loaf",
})
_HEAD_CUT = re.compile(r"\s(?:with|w/|in|on|over|topped)\s", re.IGNORECASE)
_COMBINATION = re.compile(r"\band\b|&|,", re.IGNORECASE)


def _head_noun(name: str) -> str | None:
    """The head noun of a food name: the last noun, or the noun before "of".

    "pepperoni pizza" -> pizza; "toast sandwich" -> sandwich; "slice of pizza"
    -> slice; "burger with bacon" -> burger; "Chicken McNuggets" -> mcnugget.
    """
    text = _PAREN.sub(" ", name.lower())
    text = _HEAD_CUT.split(f" {text} ", maxsplit=1)[0]
    if " of " in text:
        text = text.split(" of ", 1)[0]
    words = [_stem(word) for word in re.findall(r"[a-z]+", text) if word not in _NOUN_IGNORE]
    return words[-1] if words else None
# Words that count a container, not a unit: "1 box" of a "10 pieces" serving is unclear.
_CONTAINERS = frozenset({"box", "pack", "package", "packet", "bag", "order", "meal", "combo", "serving",
                         "container", "carton", "bowl", "plate", "tray", "bucket", "sleeve", "tub"})


def _member(token: str, words: frozenset[str]) -> bool:
    return any(token == word or (len(word) >= 5 and token.endswith(word)) for word in words)


def _canonical_units(tokens: set[str]) -> set[str]:
    out = set()
    for token in tokens:
        group = next((key for key, words in _UNIT_EQUIVALENTS.items() if _member(token, words)), None)
        out.add(group or token)
    return out


def _same_count_unit(user_text: str | None, serving_noun: str | None, amount: Decimal,
                     identity: FoodIdentity | None) -> bool:
    """True when the user's count is in the serving's own unit ("4 pieces" vs "10 pieces").

    The user's own words decide when they name a unit. When they are just a
    number ("2"), the unit comes from the HEAD noun of the food's name only
    ("eggs", "bacon slices", "McNuggets"). A whole item ("pepperoni pizza",
    "toast sandwich") is never slices or pieces; a name that states a pack size
    ("10 piece") is the pack, not the unit; a bare "1" of a piece food (one
    McNugget?) stays unclear.
    """
    serving = _canonical_units(_noun_tokens(serving_noun))
    if not serving:
        return False
    said = _noun_tokens(user_text)
    if (said & _CONTAINERS) - serving:  # "1 box" of "10 pieces"; "2 boxes" of "3 boxes" is fine
        return False
    if said:
        return bool(serving & _canonical_units(said))
    if identity is None:
        return False
    named = " ".join(part for part in (identity.name, identity.variant) if part)
    if re.search(r"\d", named):
        return False
    if _noun_tokens(named) & _CONTAINERS:
        return False
    head = _head_noun(identity.name)
    if head is None or _member(head, _WHOLE_ITEMS):
        return False
    units = _canonical_units({head})
    if not _COMBINATION.search(identity.name):  # "bacon, egg and cheese" is a sandwich, not slices
        units |= {unit for unit, foods in _NATURAL_UNIT.items() if _member(head, foods)}
    matched = serving & units
    if not matched:
        return False
    if amount == 1 and "piece" in matched:
        return False
    return True


def multiplier(portion: Portion, basis: ServingBasis,
               identity: FoodIdentity | None = None) -> Decimal | None:
    """How many source servings ``portion`` is, or ``None`` when it cannot be known.

    The result is quantized to 4 decimal places. ``None`` means the agent must
    ask; it is never replaced by a default of 1. ``identity`` (optional) lets a
    bare count ("2") take its unit from the food's name against a multi-piece
    serving ("2 large eggs"); pass it whenever the item's identity is known.
    """
    amount = _dec(portion.amount)
    if portion.kind == "unspecified":
        return Decimal(1) if basis.basis == "per_serving" else None
    if amount is None:
        return None
    if portion.kind == "serving_fraction":
        # "Half" is half of the source's own serving. A per-100 g / per-100 ml
        # panel has no serving, so "half a chicken breast" is not 50 g: ask.
        return _result(amount) if basis.basis == "per_serving" else None
    if portion.kind == "count":
        if basis.basis != "per_serving":
            return None
        if basis.unit == "count" and basis.amount is not None and _dec(basis.amount) != 1:
            # A multi-piece serving ("10 pieces"): divide only when the user
            # counted in that same unit ("4 pieces"). "1" or "1 box" is unclear.
            if not _same_count_unit(portion.text, basis.count_noun, amount, identity):
                return None
            return _result(amount / _dec(basis.amount))
        return _result(amount)

    unit = portion.unit
    if unit is not None and unit == basis.unit and basis.amount is not None:
        return _result(amount / _dec(basis.amount))

    if portion.kind == "mass":
        # A bare "oz" means fluid ounces of a drink measured in fluid ounces,
        # and of a volume-only serving (no stated weight).
        if unit == "oz" and (basis.unit == "fl_oz" or (basis.unit in VOLUME_UNITS and basis.grams is None)):
            basis_ml = _basis_ml(basis)
            return _result(amount * ML_PER["fl_oz"] / basis_ml) if basis_ml else None
        basis_g = _basis_grams(basis)
        return _result(amount * GRAMS_PER[unit] / basis_g) if basis_g else None

    if portion.kind == "volume":
        basis_ml = _basis_ml(basis)
        if basis_ml is None and unit == "fl_oz" and basis.unit == "oz" and basis.amount is not None:
            # Mirror rule, ounces only: a source serving of bare "20 oz" with no
            # stated volume, measured by the user in fluid ounces, is a drink.
            # Other volumes (a cup) against a mass basis stay unknown.
            basis_ml = _dec(basis.amount) * ML_PER["fl_oz"]
        return _result(amount * ML_PER[unit] / basis_ml) if basis_ml else None
    return None


def scale_panel(panel: NutrientPanel, factor: Decimal | float | int) -> NutrientPanel:
    """Scale every known nutrient by ``factor`` (1 decimal). Unknown stays ``None``."""
    try:
        factor = factor if isinstance(factor, Decimal) else Decimal(repr(factor))
    except InvalidOperation as exc:
        raise ValueError("multiplier must be a number") from exc
    if not factor.is_finite() or factor <= 0:
        raise ValueError("multiplier must be a positive finite number")
    scaled: dict[str, float | None] = {}
    for key in NUTRIENT_KEYS:
        value = getattr(panel, key)
        scaled[key] = None if value is None else float(
            (Decimal(repr(value)) * factor).quantize(_NUTRIENT_STEP, rounding=ROUND_HALF_UP))
    return NutrientPanel(**scaled)


__all__ = ["GRAMS_PER", "ML_PER", "parse_serving_text", "basis_per_100g", "basis_per_100ml",
           "basis_from_resolver", "multiplier", "scale_panel"]
