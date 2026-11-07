"""Deterministic serving units: what one of a food is counted in.

Bread is counted in slices, milk in cups, peanut butter in tablespoons, a banana
as one medium banana. Code decides the unit from the food's name. The model
never does, and a per-100 g panel never turns "a slice" into a "how much?"
question with ounce choices.

The weight of one unit comes from the matched row's own household measure for
that unit when it has a single clear one (a plain "1 slice", else the one
marked medium or regular). Otherwise it is the pinned typical weight below,
and the food's attribution says so. A unit is only applied when the user's
words name that unit or no unit at all: "2 oz of sourdough" stays a weight and
"a loaf" is left alone.

Pure: no I/O.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

USDA_SR = "USDA FoodData Central SR Legacy"


@dataclass(frozen=True)
class ServingUnit:
    """One food family's unit. ``grams`` is the typical weight of one unit."""

    unit: str
    plural: str
    grams: float
    source: str
    # Household-measure word that names this unit in a USDA row ("slice", "cup", "medium").
    measure: str
    volume: bool = False
    # A drink is measured by volume only: its basis carries no weight, so a bare
    # "8 oz" of milk is fluid ounces, as everywhere else in portion math.
    drink: bool = False
    # Other words a user may say for the same unit ("a piece of toast").
    synonyms: frozenset[str] = frozenset()
    choices: tuple[str, ...] = ()


_BREAD_WORDS = frozenset({
    "white", "wheat", "whole", "grain", "multigrain", "seeded", "seed", "rye", "sourdough", "french",
    "italian", "vienna", "oat", "oatmeal", "honey", "potato", "cinnamon", "raisin", "sprouted", "ezekiel",
    "pumpernickel", "brioche", "challah", "sandwich", "country", "artisan", "toasted", "toast", "bread",
    "plain", "fresh", "homemade", "slice", "slices", "piece", "pieces", "of", "a", "an",
})
_MILK_WORDS = frozenset({
    "whole", "skim", "nonfat", "non", "fat", "free", "low", "lowfat", "reduced", "percent", "1", "2",
    "chocolate", "almond", "oat", "soy", "cashew", "lactose", "organic", "cow", "cows",
    "unsweetened", "sweetened", "vanilla", "original", "dairy", "milk", "cup", "cups", "of", "a",
})

SLICE = ServingUnit(
    unit="slice", plural="slices", grams=29.0, measure="slice",
    source=f"{USDA_SR}, FDC 174924 (bread, white, commercially prepared): 1 slice = 29 g",
    synonyms=frozenset({"piece", "pieces"}), choices=("1 slice", "2 slices", "3 slices"))
WHEAT_SLICE = ServingUnit(
    unit="slice", plural="slices", grams=32.0, measure="slice",
    source=f"{USDA_SR}, FDC 172688 (bread, whole-wheat, commercially prepared): 1 slice = 32 g",
    synonyms=frozenset({"piece", "pieces"}), choices=("1 slice", "2 slices", "3 slices"))
CUP_OF_MILK = ServingUnit(
    unit="cup", plural="cups", grams=244.0, measure="cup", volume=True, drink=True,
    source=f"{USDA_SR}, milk, whole, 3.25% milkfat: 1 cup = 244 g",
    choices=("1/2 cup", "1 cup", "2 cups"))


def _tbsp(grams: float, source: str) -> ServingUnit:
    return ServingUnit(unit="tbsp", plural="tbsp", grams=grams, measure="tbsp", volume=True, source=source,
                       choices=("1 tbsp", "2 tbsp", "3 tbsp"))


def _medium(noun: str, plural: str, grams: float, source: str) -> ServingUnit:
    return ServingUnit(unit=noun, plural=plural, grams=grams, measure="medium", source=source,
                       choices=(f"1 {noun}", f"2 {plural}", f"Half a {noun}"))


# Exact food names (after normalizing) -> unit. Spreads and fruit are listed by
# name: a modifier ("chocolate peanut butter", "banana bread") is a different food.
_BY_NAME: dict[str, ServingUnit] = {
    "peanut butter": _tbsp(16.0, f"{USDA_SR}, peanut butter, smooth style: 2 tbsp = 32 g"),
    "creamy peanut butter": _tbsp(16.0, f"{USDA_SR}, peanut butter, smooth style: 2 tbsp = 32 g"),
    "smooth peanut butter": _tbsp(16.0, f"{USDA_SR}, peanut butter, smooth style: 2 tbsp = 32 g"),
    "crunchy peanut butter": _tbsp(16.0, f"{USDA_SR}, peanut butter, chunk style: 2 tbsp = 32 g"),
    "almond butter": _tbsp(16.0, f"{USDA_SR}, nuts, almond butter, plain: 1 tbsp = 16 g"),
    "butter": _tbsp(14.2, f"{USDA_SR}, butter, salted: 1 tbsp = 14.2 g"),
    "olive oil": _tbsp(13.5, f"{USDA_SR}, oil, olive, salad or cooking: 1 tbsp = 13.5 g"),
    "honey": _tbsp(21.0, f"{USDA_SR}, honey: 1 tbsp = 21 g"),
    "mayo": _tbsp(13.8, f"{USDA_SR}, salad dressing, mayonnaise, regular: 1 tbsp = 13.8 g"),
    "mayonnaise": _tbsp(13.8, f"{USDA_SR}, salad dressing, mayonnaise, regular: 1 tbsp = 13.8 g"),
    "banana": _medium("banana", "bananas", 118.0, f"{USDA_SR}, bananas, raw: 1 medium = 118 g"),
    "apple": _medium("apple", "apples", 182.0, f"{USDA_SR}, apples, raw, with skin: 1 medium = 182 g"),
    "orange": _medium("orange", "oranges", 131.0,
                      f"{USDA_SR}, oranges, raw, all commercial varieties: 1 medium = 131 g"),
}
_BREAD_HEADS = frozenset({"bread", "toast", "sourdough", "rye", "pumpernickel", "brioche", "challah"})
# Bread-shaped names that are not sliced loaf bread.
_NOT_SLICED = frozenset({"french toast", "texas toast", "garlic bread", "banana bread", "zucchini bread",
                         "pumpkin bread", "monkey bread", "bread pudding", "bread bowl", "bread crumbs",
                         "breadcrumbs", "corn bread", "cornbread", "flatbread", "naan", "pita", "shortbread"})
_PLURAL = {"bananas": "banana", "apples": "apple", "oranges": "orange"}

# Count and measure words a user may name an amount in. When the words name one
# of these that is not the family's own unit ("a loaf", "a cup of bread"), the
# family is not applied.
_UNIT_WORDS = frozenset({
    "slice", "slices", "piece", "pieces", "loaf", "loaves", "cup", "cups", "c", "tbsp", "tablespoon",
    "tablespoons", "tbs", "tsp", "teaspoon", "teaspoons", "spoon", "spoons", "spoonful", "spoonfuls", "glass",
    "glasses", "bowl", "bowls", "serving", "servings", "ml", "l", "liter", "liters", "litre", "fl", "floz",
    "pint", "pints", "quart", "quarts", "gallon", "can", "cans", "carton", "bottle", "bottles", "stick",
    "sticks", "pat", "pats", "roll", "rolls", "bun", "buns", "half", "quarter",
})
_FRACTION_WORDS = frozenset({"half", "quarter"})
_VOLUME_UNITS = frozenset({"cup", "tbsp", "tsp", "fl_oz", "ml", "l"})
# A row measure with one of these words is not the regular unit.
_OFF_SIZE = frozenset({"small", "large", "thin", "thick", "mini", "jumbo", "extra", "big", "snack", "crumbs",
                       "crumb", "cubes", "cubed", "chopped", "sliced", "diced", "mashed", "pureed", "sections",
                       "slices", "packed", "melted", "whipped", "pat", "stick", "dialysis", "toasted"})


def _words(text: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").casefold().replace("’", "'").replace("'", ""))


_COMBINATION = re.compile(r"(?:\bwith\b|\bw/|\bon\b|\btopped\b|\band\b|&|\+|,)")


def _name(text: str) -> str:
    """Lowercase words of the food name, without anything in parentheses. Empty for a
    combination ("peanut butter and jelly", "toast with butter"): that is a dish, not one food."""
    text = re.sub(r"\([^)]*\)", " ", str(text or "").casefold())
    if _COMBINATION.search(text):
        return ""
    return " ".join(_words(text))


def for_food(name: str) -> ServingUnit | None:
    """The unit a food is counted in, or ``None`` when no family applies."""
    text = _name(name)
    if not text:
        return None
    if text in _BY_NAME:
        return _BY_NAME[text]
    words = text.split()
    if words[-1] in _PLURAL and " ".join([*words[:-1], _PLURAL[words[-1]]]) in _BY_NAME:
        return _BY_NAME[" ".join([*words[:-1], _PLURAL[words[-1]]])]
    if any(phrase in text for phrase in _NOT_SLICED):
        return None
    if words[-1] in _BREAD_HEADS or (words[-1] in ("slice", "slices") and set(words) & _BREAD_HEADS):
        if not set(words) <= _BREAD_WORDS | _BREAD_HEADS:
            return None  # "avocado toast", "cheesy bread": more than bread
        return WHEAT_SLICE if "wheat" in words else SLICE
    if words[-1] in ("milk", "buttermilk", "kefir") and set(words) <= _MILK_WORDS | {"buttermilk", "kefir"}:
        return CUP_OF_MILK
    return None


def _named_units(text: str) -> set[str]:
    words = set(_words(text))
    if "fl" in words or "floz" in words:
        words |= {"fl"}
    return words & _UNIT_WORDS


def applies(family: ServingUnit, text: str, kind: str, unit: str | None = None) -> bool:
    """Whether the user's amount is counted in this family's unit.

    Yes for a plain count or no amount ("2", "a slice", "a piece of toast", ""), and for
    a volume family's own volume ("1 cup", "8 fl oz" or a bare "8 oz" of milk). No for a
    weight, or for words that name some other unit ("a loaf", "a cup of bread").
    """
    if kind == "mass":
        # A bare "8 oz" of a drink is fluid ounces, as portion math reads it against a volume basis.
        return family.drink and unit == "oz"
    if kind == "volume":
        return family.volume and unit in _VOLUME_UNITS
    named = _named_units(text) - _FRACTION_WORDS
    own = {family.unit, family.plural, *family.synonyms}
    if family.unit == "tbsp":
        own |= {"tablespoon", "tablespoons", "tbs"}
    return named <= own


def row_measure(measures: Iterable[Mapping[str, Any]] | None, family: ServingUnit) -> tuple[str, float] | None:
    """The row's own measure for one unit: a single plain "1 slice", else the one marked medium
    or regular. Several different weights for the same wording is no answer."""
    plain: list[tuple[str, float]] = []
    regular: list[tuple[str, float]] = []
    for measure in measures or []:
        if not isinstance(measure, Mapping):
            continue
        text = str(measure.get("disseminationText") or "").strip()
        grams = measure.get("gramWeight")
        if not isinstance(grams, (int, float)) or isinstance(grams, bool) or not grams > 0 or grams > 2000:
            continue
        if not re.match(r"^1(?:\.0+)?\s", text):
            continue
        words = set(_words(re.sub(r"\([^)]*\)", " ", text))) - {"1", "0"}
        sized = {"medium", "regular"} & words
        if family.measure not in words and not (family.measure == "medium" and sized):
            continue
        if words & _OFF_SIZE or ("fl" in words and family.unit == "cup"):
            continue
        (regular if sized else plain).append((text, float(grams)))
    for group in (plain, regular):
        if group and len({grams for _, grams in group}) == 1:
            return min(group, key=lambda item: item[0].casefold())
    return None


def serving_text(family: ServingUnit, grams: float) -> str:
    """The basis text for one unit, in the shape ``parse_serving_text`` reads."""
    if family.drink:
        return f"1 {family.unit}"
    return f"1 {family.unit} ({grams:g} g)"


def amount_text(servings: float, family_unit: str, plural: str) -> str:
    """"1 slice", "2 slices", "1/2 cup", "1.5 cups"."""
    value = round(float(servings), 2)
    if value == 0.5:
        return f"1/2 {family_unit}"
    if value == 0.25:
        return f"1/4 {family_unit}"
    return f"{value:g} {family_unit if value == 1 else plural}"


def attribution(family: ServingUnit, grams: float, from_row: str | None) -> dict[str, Any]:
    """What the food carries so the label and the receipt can say where the unit came from.
    A drink's weight stays out of ``serving_grams`` so its basis is volume only."""
    # Fruit reads "(1 medium)", not "Banana (1 banana)".
    shown = ("medium", "medium") if family.measure == "medium" else (family.unit, family.plural)
    out: dict[str, Any] = {"serving_unit": shown[0], "serving_unit_plural": shown[1],
                           "serving_unit_grams": grams}
    if not family.drink:
        out["serving_grams"] = grams
    if from_row:
        out["serving_unit_source"] = f"row measure: {from_row}"
    else:
        out["serving_unit_source"] = f"typical: {family.source}"
        out["serving_unit_assumed"] = True
    return out


def choices_for(name: str | None) -> tuple[str, ...] | None:
    """Quick replies for a "how much?" question, in the food's own unit."""
    family = for_food(name or "")
    return family.choices if family is not None and family.choices else None


__all__ = ["ServingUnit", "for_food", "applies", "row_measure", "serving_text", "amount_text",
           "attribution", "choices_for", "SLICE", "CUP_OF_MILK"]
