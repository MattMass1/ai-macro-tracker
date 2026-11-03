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
    # Not "cheese": it comes as slices, sticks, cubes or ounces ("string cheese" asks).
    "slice": frozenset({"bacon", "bread", "toast", "ham", "turkey", "salami", "pepperoni"}),
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


# Count words that are a unit of measure for a food (not the food itself).
# "2 slices" names a unit; "2 Big Macs" names the food.
_UNIT_WORDS = frozenset(_UNIT_EQUIVALENTS) | _CONTAINERS | frozenset(
    {"loaf", "bar", "stick", "cube", "scoop", "link", "patty", "wedge", "square", "sheet"})
_UNCLEAR = object()
_SUB_UNITS = frozenset(_UNIT_EQUIVALENTS)  # slice, piece: parts of a whole item
_FLUID_UNITS = frozenset({"fl_oz", "ml", "l"})
_GENERIC_PIECE = frozenset({"piece", "pc"})
_FRACTION_WORDS: dict[str, Decimal] = {
    "half": Decimal("0.5"), "halve": Decimal("0.5"), "quarter": Decimal("0.25"),
}
_FRACTION_KEY = {"half": "half", "halves": "half", "quarter": "quarter", "quarters": "quarter"}
# A standalone fraction word: not inside a hyphenated word ("half-and-half").
_FRACTION_RE = re.compile(r"(?<![a-z-])(half|halves|quarters?)(?![a-z-])")
_FRACTION_FOLLOWERS = frozenset({"of", "a", "an", "the"})
# Single-portion dishes: "1 serving (215 g)" of one of these is the dish.
_SINGLE_PORTION = frozenset({
    "sandwich", "burger", "wrap", "bowl", "burrito", "salad", "smoothie", "shake", "bar", "sub", "hoagie",
    "bagel", "muffin", "quesadilla", "panini", "melt", "whopper", "croissant", "parfait", "taco",
})
# Small multiples: "20 fries" or "10 almonds" is never 20 or 10 servings.
_SMALL_MULTIPLES = frozenset({
    "almond", "nut", "cashew", "peanut", "pistachio", "walnut", "pecan", "fry", "chip", "crisp", "cracker",
    "pretzel", "grape", "berry", "cherry", "olive", "raisin", "candy", "popcorn", "tot",
})
_NO_MATCH = object()


def _fraction_in_words(text: str | None, identity: FoodIdentity | None) -> Decimal | None:
    """A standalone fraction word in the portion words ("1 half", "half a sandwich",
    "2 bacon halves"), or None. Never a word from the food's NAME ("Quarter
    Pounder", "half-and-half", "half moon cookie")."""
    lowered = (text or "").lower()
    named = " ".join(part for part in (identity.name, identity.variant) if part).lower() if identity else ""
    in_name = {_FRACTION_KEY.get(word) for word in re.findall(r"[a-z]+", named)}
    head = _head_noun(identity.name) if identity is not None else None
    for match in _FRACTION_RE.finditer(lowered):
        key = _FRACTION_KEY[match.group(1)]
        if key in in_name:
            continue
        rest = re.findall(r"[a-z]+", lowered[match.end():])
        following = rest[0] if rest else None
        if following is None or following in _FRACTION_FOLLOWERS or (
                head is not None and _stem(following) == head):
            return _FRACTION_WORDS[key]
    return None


def _inner_counts(text: str) -> list[tuple[Decimal, set[str]]]:
    """Parenthesized piece counts in serving text: "1 pizza (8 slices)" -> [(8, {slice})]."""
    out = []
    for inner in _PAREN.findall(text or ""):
        if _measure(inner):
            continue
        match = _LEADING.match(inner)
        if not match:
            continue
        qty = _qty(match["qty"])
        units = _canonical_units(_noun_tokens(match["rest"]))
        if qty is not None and qty > 0 and units:
            out.append((qty, units))
    return out


def _identity_units(identity: FoodIdentity | None) -> Any:
    """Count unit implied by the HEAD noun of the food name.

    Returns ``None`` (no identity), ``_UNCLEAR`` (the name states a pack size
    like "10 piece"), or ``(units, unit_bearing)``.
    ``unit_bearing`` is True when the head is itself counted in a unit word
    (slice, piece, McNugget, bacon), so a mismatch must ask. A whole item
    ("pepperoni pizza", "toast sandwich", "string cheese") is counted as itself.
    """
    if identity is None:
        return None
    named = " ".join(part for part in (identity.name, identity.variant) if part)
    if re.search(r"\d", named):
        return _UNCLEAR
    head = _head_noun(identity.name)
    if head is None:
        return None
    if _member(head, _WHOLE_ITEMS) or _member(head, _SINGLE_PORTION):
        return {head}, False
    units = _canonical_units({head})
    if not _COMBINATION.search(identity.name):  # "bacon, egg and cheese" is a sandwich, not slices
        units |= {unit for unit, foods in _NATURAL_UNIT.items() if _member(head, foods)}
    return units, bool(units & _UNIT_WORDS)


def _count_multiplier(portion: Portion, basis: ServingBasis, amount: Decimal,
                      identity: FoodIdentity | None) -> Decimal | None:
    """Servings for a count, by one rule. ``None`` means ask; it never guesses.

    1. **Fractions first, once.** A standalone "half", "halves" or "quarter" in
       the portion words scales a WHOLE count (``1 half`` = 0.5, ``2 bacon
       halves`` = 1). Never when the amount already holds it (1.5 "one and a
       half"), and never from the food's name ("Quarter Pounder",
       "half-and-half", "half moon cookie").
    2. **The user's unit U.** The portion words' nouns when they match the
       serving or name a unit word ("2 slices", "6 nuggets", "1 box");
       otherwise the HEAD noun of the food name ("2" eggs, "three" bacon -> slice,
       "6" McNuggets -> piece, "pepperoni pizza" -> pizza). Plurals and
       synonyms are normalised (pc/nugget/wing/tender -> piece; rasher -> slice).
    3. **The serving's units.** Parsed from its text, before and inside
       parentheses: outer (n, noun) as in "2 large eggs", "1 pizza", "1 order";
       inner (n, noun) as in "(8 slices)", "(3 tacos)", "(6 pieces)".
    4. **Match.** U equals an inner noun: count / inner n ("2 slices" of
       "1 pizza (8 slices)" = 0.25; "2 tacos" of "1 order (3 tacos)" = 0.667).
       U equals the outer noun: count / outer n ("6 nuggets" of "4 pieces" =
       1.5; "1" pizza of "1 pizza (8 slices)" = 1).
    5. **Generic piece.** "piece"/"pc" said against a single countable unit with
       no inner count ("1 slice (47 g)", "1 bar") is that unit.
    6. **The food itself.** When U is the food (not a unit word like slice,
       piece, bar, box) and the serving is one countable whole item with no
       inner count ("2 Big Macs" of "1 sandwich"), the count is servings. Drinks
       served by fluid volume ("2 smoothies" of "12 fl oz") are servings too, as
       are recalled portions ("one portion as logged") and a plain "1 serving"
       with no weight. "1 serving (215 g)" is the dish only for a count of 1 of
       a single-portion dish (sandwich, burger, bowl, burrito, salad, smoothie,
       bar). Whole-to-portion foods (pie, cake, loaf, pizza) and small
       multiples (fries, almonds, chips) against a "1 serving" ask.
    7. **Otherwise ask.** A different unit ("2 slices" of "1 loaf"), a whole
       item against a part ("1 pizza" of "2 slices"), or a food count against a
       mass or cup serving ("2 eggs" of "100 g") returns ``None``. So does a
       bare "1" of a piece food against a multi-piece serving (one McNugget, or
       one box?) and any name that states its own pack size ("10 piece").
    """
    if amount == amount.to_integral_value():  # 1.5 "one and a half" already holds its fraction
        fraction = _fraction_in_words(portion.text, identity)
        if fraction is not None:
            amount = amount * fraction
    said = _noun_tokens(portion.text) - set(_FRACTION_WORDS)
    said_units = _canonical_units(said)

    per = _dec(basis.amount) if basis.unit == "count" and basis.amount is not None else None
    outer = _canonical_units(_noun_tokens(basis.count_noun)) if per is not None else set()
    inner = _inner_counts(basis.text)
    single = per == 1 and bool(outer) and not (outer & _CONTAINERS) and not inner
    fluid = per is None and basis.unit in _FLUID_UNITS

    def match(units: set[str]) -> Any:
        for qty, inner_units in inner:
            if units & inner_units:
                return qty, units & inner_units
        if per is not None and units & outer:
            return per, units & outer
        return _NO_MATCH

    undescribed = basis.basis == "per_serving" and basis.amount is None and basis.unit is None and not inner
    generic_serving = (per == 1 and outer == {"serving"} and not inner) or undescribed

    # A bare "1" serving with no noun (a recalled "one portion as logged") is one portion.
    bare_one = per == 1 and not outer and not inner

    def food_itself() -> Decimal | None:
        if (single and not (outer & _SUB_UNITS)) or fluid or bare_one:
            return _result(amount)
        if generic_serving:
            head = _head_noun(identity.name) if identity is not None else None
            if head is not None and _member(head, _SMALL_MULTIPLES):
                return None  # "20 fries" of "1 serving" is not 20 servings
            # "1 serving" with no weight is one of whatever the source sells.
            if basis.grams is None and basis.ml is None:
                return _result(amount)
            # "1 serving (215 g)" is the dish only for ONE single-portion dish
            # (sandwich, burger, bowl...); a pie, cake, loaf or pizza asks.
            if amount == 1 and head is not None and _member(head, _SINGLE_PORTION):
                return _result(amount)
        return None

    # 2a. The user's own words.
    if said:
        found = match(said_units)
        if found is not _NO_MATCH:
            return _result(amount / found[0])
        if said & _GENERIC_PIECE and single:
            return _result(amount)
        if said_units & _UNIT_WORDS:
            return None  # a unit the serving does not have: "2 slices" of "1 loaf", "1 box"

    # 2b. The head noun of the food name.
    ident = _identity_units(identity)
    if ident is _UNCLEAR:
        # The name states its pack size ("McNuggets (10 piece)"): one of the
        # pack against the product's own "1 serving" is that serving.
        if generic_serving and amount == 1:
            return _result(amount)
        return food_itself() if per is None or per == 1 else None
    if ident is None:
        if said:  # the words name the food itself ("2 smoothies"), no identity given
            return food_itself()
        # Nothing names a unit at all: n of a one-unit serving is n servings.
        return _result(amount) if (per == 1 and not inner) or fluid else None
    units, bearing = ident
    found = match(units)
    if found is not _NO_MATCH:
        divisor, matched = found
        if divisor != 1 and amount == 1 and "piece" in matched:
            return None  # "1" McNugget against "4 pieces": one piece or one box?
        return _result(amount / divisor)
    if bearing:
        return None  # "2" bread against "1 loaf": a slice is not a loaf
    return food_itself()


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
        return _count_multiplier(portion, basis, amount, identity)

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
