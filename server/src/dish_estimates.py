"""Typical-serving estimates for everyday dishes described in plain words.

"I'm having a salad with bacon and cheese" or "a large breakfast burrito with
eggs, sausage and cheese" has no label and no weights. Instead of a web guess
at the whole dish name (or "not counted"), the dish is built from a typical
recipe: a base (tortilla, greens, bread) plus the contents the user listed,
each at a typical in-dish amount, scaled for small / medium / large.

Every number is server arithmetic over pinned USDA FoodData Central SR Legacy
per-100 g rows (public domain); the model never sends numbers. The result is
always an estimate (marked "est., typical <size>") and the receipt lists what
it was built from, so the user can correct it. A listed content with no row
is named as not counted, never guessed.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

USDA = "USDA FDC"
SIZE_FACTORS = {"small": 0.7, "medium": 1.0, "large": 1.4}
_MASS_GRAMS = {"g": 1.0, "gram": 1.0, "grams": 1.0, "oz": 28.35, "ounce": 28.35, "ounces": 28.35,
               "lb": 453.6, "lbs": 453.6, "pound": 453.6, "pounds": 453.6}
# Units that just mean "one of the dish".
_DISH_UNITS = frozenset({"serving", "servings", "each", "piece", "pieces", "plate", "plates",
                         "bowl", "bowls", "order", "orders", "whole"})

# name: (label, FDC id, (kcal, protein, carbs, fat, fiber) per 100 g,
#        grams inside a medium dish, standalone serving grams, serving label,
#        grams per counted piece or None, aliases)
_I = {
    "egg": ("scrambled egg", 172187, (149, 9.99, 1.61, 10.98, 0), 90, 122, "2 eggs", 61,
            ("egg", "eggs", "scrambled egg", "scrambled eggs", "egg whites")),
    "boiled egg": ("hard-boiled egg", 173424, (155, 12.58, 1.12, 10.61, 0), 50, 100, "2 eggs", 50,
                   ("boiled egg", "boiled eggs", "hard boiled egg", "hard boiled eggs")),
    "sausage": ("pork sausage", 174578, (325, 18.53, 1.42, 27.25, 0), 45, 46, "2 links", 23,
                ("sausage", "sausages", "pork sausage", "breakfast sausage", "sausage links",
                 "sausage patty", "sausage patties")),
    "bacon": ("bacon", 167712, (541, 37.04, 1.43, 41.78, 0), 16, 24, "3 slices", 8,
              ("bacon", "bacon bits", "crispy bacon", "bacon strips", "bacon slices")),
    "ham": ("sliced ham", 173864, (164, 16.6, 3.63, 8.8, 1.3), 40, 56, "2 slices", 28, ("ham",)),
    "turkey": ("sliced turkey breast", 172941, (106, 14.81, 2.2, 3.77, 0), 56, 56, "2 oz", 16,
               ("turkey", "deli turkey", "turkey breast", "sliced turkey")),
    "chicken": ("grilled chicken breast", 171534, (151, 30.54, 0, 3.17, 0), 85, 85, "3 oz cooked", None,
                ("chicken", "grilled chicken", "chicken breast", "grilled chicken breast")),
    "steak": ("sirloin steak", 168634, (183, 30.55, 0, 5.79, 0), 85, 85, "3 oz cooked", None,
              ("steak", "sirloin", "carne asada")),
    "ground beef": ("ground beef 85/15", 174034, (256, 27.73, 0, 15.3, 0), 70, 85, "3 oz cooked",
                    None, ("ground beef", "beef", "taco meat", "hamburger meat")),
    "tuna": ("canned light tuna", 171986, (116, 25.51, 0, 0.82, 0), 85, 85, "3 oz", None, ("tuna",)),
    "shrimp": ("cooked shrimp", 171971, (119, 22.78, 1.52, 1.7, 0), 85, 85, "3 oz cooked", None,
               ("shrimp", "prawns")),
    "cheddar": ("cheddar cheese", 173414, (403, 22.87, 3.37, 33.31, 0), 28, 28, "1 oz", None,
                ("cheese", "cheddar", "cheddar cheese", "shredded cheese", "mexican cheese")),
    "american cheese": ("American cheese", 171290, (371, 18.13, 3.7, 31.79, 0), 21, 21, "1 slice", 21,
                        ("american cheese", "american")),
    "mozzarella": ("mozzarella", 170845, (299, 22.17, 2.4, 22.14, 0), 28, 28, "1 oz", None,
                   ("mozzarella", "mozzarella cheese")),
    "swiss": ("Swiss cheese", 171251, (393, 26.96, 1.44, 30.99, 0), 21, 28, "1 oz", 21,
              ("swiss", "swiss cheese")),
    "pepper jack": ("Monterey Jack cheese", 170844, (373, 24.48, 0.68, 30.28, 0), 28, 28, "1 oz", None,
                    ("pepper jack", "pepper jack cheese", "monterey jack", "jack cheese")),
    "feta": ("feta", 173420, (265, 14.21, 3.88, 21.49, 0), 28, 28, "1 oz", None, ("feta", "feta cheese")),
    "parmesan": ("grated parmesan", 171247, (420, 28.42, 13.91, 27.84, 0), 10, 10, "2 tbsp", None,
                 ("parmesan", "parmesan cheese", "parm")),
    "blue cheese": ("blue cheese", 172175, (353, 21.4, 2.34, 28.74, 0), 15, 28, "1 oz", None,
                    ("blue cheese", "gorgonzola")),
    "potatoes": ("hash browns", 170044, (219, 2.65, 28.51, 11.59, 3.2), 60, 75, "1/2 cup", None,
                 ("potato", "potatoes", "hash brown", "hash browns", "hashbrowns", "home fries",
                  "breakfast potatoes", "tater tots")),
    "rice": ("white rice", 168878, (130, 2.69, 28.17, 0.28, 0.4), 80, 158, "1 cup", None,
             ("rice", "white rice", "mexican rice", "cilantro lime rice")),
    "black beans": ("black beans", 173735, (132, 8.86, 23.71, 0.54, 8.7), 60, 86, "1/2 cup", None,
                    ("beans", "black beans", "pinto beans")),
    "refried beans": ("refried beans", 172438, (90, 4.98, 13.55, 2.01, 3.7), 60, 119, "1/2 cup", None,
                      ("refried beans",)),
    "lettuce": ("romaine lettuce", 169247, (17, 1.23, 3.29, 0.3, 2.1), 15, 47, "1 cup", None,
                ("lettuce", "romaine", "romaine lettuce", "greens", "mixed greens", "spring mix",
                 "iceberg", "iceberg lettuce", "leafy greens")),
    "spinach": ("spinach", 168462, (23, 2.86, 3.63, 0.39, 2.2), 30, 30, "1 cup", None,
                ("spinach", "baby spinach")),
    "tomato": ("tomato", 170457, (18, 0.88, 3.89, 0.2, 1.2), 30, 62, "1 tomato", 62,
               ("tomato", "tomatoes", "cherry tomatoes", "pico", "pico de gallo")),
    "cucumber": ("cucumber", 168409, (15, 0.65, 3.63, 0.11, 0.5), 30, 52, "1/2 cup", None,
                 ("cucumber", "cucumbers")),
    "onion": ("onion", 170000, (40, 1.1, 9.34, 0.1, 1.7), 15, 40, "1/4 cup", None,
              ("onion", "onions", "red onion", "red onions")),
    "peppers": ("bell pepper", 170427, (20, 0.86, 4.64, 0.17, 1.7), 25, 75, "1/2 cup", None,
                ("pepper", "peppers", "bell pepper", "bell peppers", "green peppers")),
    "jalapeno": ("jalapeno", 168576, (29, 0.91, 6.5, 0.37, 2.8), 10, 14, "1 pepper", 14,
                 ("jalapeno", "jalapenos")),
    "carrot": ("carrot", 170393, (41, 0.93, 9.58, 0.24, 2.8), 20, 61, "1 carrot", 61, ("carrot", "carrots")),
    "mushrooms": ("mushrooms", 169251, (22, 3.09, 3.26, 0.34, 1.0), 30, 70, "1 cup", None,
                  ("mushroom", "mushrooms")),
    "corn": ("sweet corn", 169214, (67, 2.29, 14.34, 1.22, 2.0), 40, 82, "1/2 cup", None, ("corn",)),
    "olives": ("black olives", 169094, (116, 0.84, 6.04, 10.9, 1.6), 15, 15, "1 tbsp", None,
               ("olive", "olives", "black olives")),
    "avocado": ("avocado", 171706, (167, 1.96, 8.64, 15.41, 6.8), 50, 50, "1/3 avocado", None,
                ("avocado", "guacamole", "guac")),
    "sour cream": ("sour cream", 171257, (198, 2.44, 4.63, 19.35, 0), 24, 24, "2 tbsp", None,
                   ("sour cream",)),
    "salsa": ("salsa", 174524, (29, 1.52, 6.64, 0.17, 1.9), 36, 36, "2 tbsp", None, ("salsa",)),
    "croutons": ("croutons", 172752, (465, 10.8, 63.5, 18.3, 5.0), 10, 10, "1/4 cup", None,
                 ("crouton", "croutons")),
    "mayo": ("mayonnaise", 171009, (680, 0.96, 0.57, 74.85, 0), 14, 14, "1 tbsp", None,
             ("mayo", "mayonnaise")),
    "ranch": ("ranch dressing", 173592, (430, 1.32, 5.9, 44.54, 0), 30, 30, "2 tbsp", None,
              ("ranch", "ranch dressing")),
    "caesar dressing": ("caesar dressing", 169055, (542, 2.17, 3.3, 57.85, 0.5), 30, 30, "2 tbsp", None,
                        ("caesar dressing", "caesar")),
    "italian dressing": ("Italian dressing", 171019, (240, 0.41, 12.12, 21.12, 0), 30, 30, "2 tbsp", None,
                         ("italian dressing", "vinaigrette", "dressing", "salad dressing")),
    "flour tortilla": ("flour tortilla", 173242, (325, 8.7, 55.6, 7.1, 3.3), 72, 72, "1 tortilla (10 in)",
                       72, ("tortilla", "flour tortilla", "tortillas", "flour tortillas", "wrap")),
    "white bread": ("white bread", 174924, (266, 8.85, 49.42, 3.33, 2.7), 58, 29, "1 slice", 29,
                    ("bread", "white bread")),
    "wheat bread": ("whole-wheat bread", 172688, (252, 12.45, 42.71, 3.5, 6.0), 64, 32, "1 slice", 32,
                    ("wheat bread", "whole wheat bread", "wheat")),
}
DRESSINGS = frozenset({"ranch", "caesar dressing", "italian dressing", "blue cheese"})

# Dish: base parts always counted (ingredient, medium grams, per-size grams
# override) and the typical contents used only when the user listed none.
_D: dict[str, dict[str, Any]] = {
    "breakfast burrito": {"base": [("flour tortilla", 72, {"small": 48, "large": 117})],
                          "typical": ["egg", "cheddar", "potatoes", "sausage"]},
    "burrito bowl": {"base": [], "typical": ["rice", "black beans", "chicken", "cheddar", "salsa", "lettuce"]},
    "burrito": {"base": [("flour tortilla", 72, {"small": 48, "large": 117})],
                "typical": ["rice", "black beans", "chicken", "cheddar", "salsa"]},
    "quesadilla": {"base": [("flour tortilla", 72, {"small": 48, "large": 117}), ("cheddar", 56, None)],
                   "typical": []},
    "wrap": {"base": [("flour tortilla", 72, {"small": 48, "large": 117})],
             "typical": ["chicken", "lettuce", "tomato", "cheddar"]},
    "caesar salad": {"base": [("lettuce", 94, None), ("caesar dressing", 30, None), ("parmesan", 10, None),
                              ("croutons", 10, None)], "typical": []},
    "salad": {"base": [("lettuce", 94, None)], "typical": ["tomato", "cucumber", "carrot"]},
    "omelette": {"base": [("egg", 122, {"small": 61, "large": 183})], "typical": ["cheddar"]},
    "omelet": {"alias_of": "omelette"},
    "sandwich": {"base": [("white bread", 58, None)],
                 "typical": ["turkey", "american cheese", "lettuce", "tomato"]},
}
_INGREDIENT_ALIASES = {alias: key for key, row in _I.items() for alias in row[7]}
_ALIAS_PATTERN = re.compile(r"\b(" + "|".join(sorted((re.escape(a) for a in _INGREDIENT_ALIASES),
                                                       key=len, reverse=True)) + r")\b")
_DISH_PATTERN = re.compile(r"\b(" + "|".join(sorted((re.escape(d) for d in _D), key=len, reverse=True))
                           + r")(?:es|s)?\b")
_SPLIT = re.compile(r"\s*(?:,|&|\+|/|\bw\b|\bwith\b|\band\b|\bplus\b|\btopped\b|\bon\b|\bin\b|\bof\b|\bno\b"
                    r"|\bwithout\b)\s*")
_FILLER = re.compile(r"\b(?:a|an|the|some|my|homemade|fresh|little|bit|side|small|medium|large|big|regular|"
                     r"loaded|plain|simple|basic|typical|normal|kind|style|type|top|it|inside|also|stuff|them|everything|"
                     r"breakfast|lunch|dinner|meal)\b")
_EXTRA = re.compile(r"\b(?:extra|double|lots|lot|loaded|heavy)\b")
_LIGHT = re.compile(r"\b(?:light|lite|little|bit|sprinkle|touch|splash)\b")
_NEGATED = re.compile(r"\b(?:no|without|hold the)\s+(.+?)(?=\s*(?:,|&|\+|\bwith\b|\band\b|\bplus\b|$))")


def _norm(text: str) -> str:
    text = str(text or "").casefold().replace("’", "'")
    text = re.sub(r"[^a-z0-9/&+,' ]+", " ", text).replace("'", "")
    return " ".join(text.split())


def _row(key: str) -> tuple:
    return _I[key]


def _part(key: str, grams: float) -> dict[str, Any]:
    label, fdc, per100, *_ = _row(key)
    macros = dict(zip(("calories", "protein", "carbs", "fat", "fiber"), per100))
    return {"key": key, "label": label, "fdc": fdc, "grams": round(grams, 1),
            **{k: v * grams / 100.0 for k, v in macros.items()}}


def _size_of(text: str, size: str | None) -> tuple[str, bool]:
    if size in SIZE_FACTORS:
        return size, True
    for word, name in (("small", "small"), ("medium", "medium"), ("large", "large"), ("big", "large")):
        if re.search(rf"\b{word}\b", text):
            return name, True
    return "medium", False


def _contents(text: str) -> tuple[list[tuple[str, float, float | None]], list[str], set[str]]:
    """(ingredient key, amount factor, counted grams) triples, unknown chunks
    and negated keys. A counted piece ("3 eggs", "2 slices bacon") fixes the
    grams at that many pieces, whatever the dish size."""
    negated: set[str] = set()
    for match in _NEGATED.finditer(text):
        for alias in _ALIAS_PATTERN.findall(match[1]):
            negated.add(_INGREDIENT_ALIASES[alias])
    text = _NEGATED.sub(" , ", text)
    found: list[tuple[str, float, float | None]] = []
    unknown: list[str] = []
    for chunk in _SPLIT.split(text):
        factor = 1.5 if _EXTRA.search(chunk) else 0.5 if _LIGHT.search(chunk) else 1.0
        chunk = " ".join(_FILLER.sub(" ", _EXTRA.sub(" ", _LIGHT.sub(" ", chunk))).split())
        if not chunk:
            continue
        hits = _ALIAS_PATTERN.findall(chunk)
        count = re.match(r"^(\d+(?:\.\d+)?)\b", chunk)
        rest = " ".join(_ALIAS_PATTERN.sub(" ", chunk).split())
        for alias in hits:
            key = _INGREDIENT_ALIASES[alias]
            piece = _row(key)[6]
            grams = (float(count[1]) * piece if count and piece and len(hits) == 1
                     and 0 < float(count[1]) <= 12 else None)
            if key not in {k for k, *_ in found}:
                found.append((key, factor, grams))
        rest = re.sub(r"\b(?:\d+(?:\.\d+)?|slices?|strips?|links?|pieces?)\b", " ", rest).strip()
        if rest and not re.fullmatch(r"(?:grilled|crispy|fried|scrambled|shredded|sliced|diced|chopped|"
                                     r"cooked|roasted|sauteed|fresh|real|cheesy|\s)+", rest):
            unknown.append(rest)
    return found, unknown, negated


def _amount(quantity: Any, unit: str | None, total_grams: float) -> tuple[float, str] | None:
    """Multiplier for the whole dish from the stated quantity/unit, or None
    when the unit can't apply to a dish (cups of burrito)."""
    count = float(quantity) if quantity is not None else 1.0
    if unit in _MASS_GRAMS and total_grams > 0:
        grams = count * _MASS_GRAMS[unit]
        return (grams / total_grams, f"scaled to {grams:g} g") if 0 < grams <= 3000 else None
    if not 0 < count <= 20:
        return None
    if unit in (None, "") or unit in _DISH_UNITS:
        return count, ""
    return None


def _result(name: str, parts: list[dict[str, Any]], multiplier: float, assumption: str,
            not_counted: list[str], size: str | None, listed: bool = True) -> dict[str, Any]:
    totals = {k: round(sum(p[k] for p in parts) * multiplier, 2)
              for k in ("calories", "protein", "carbs", "fat", "fiber")}
    # Kept short: a meal's sources are joined into one Composite source that
    # the validator caps at 1000 characters. The grams live in built_from.
    ids = ", ".join(dict.fromkeys(str(p["fdc"]) for p in parts))
    source = f"BOUNDED ESTIMATE, typical {name}; USDA SR Legacy FDC {ids}"[:300]
    result = {"name": name, "macro_source": source, **totals,
              "estimate_provenance": "typical_serving_estimate", "assumption": assumption,
              "typical_size": size, "dish": size is not None, "listed": listed,
              "built_from": [{"label": p["label"], "grams": round(p["grams"] * multiplier, 1),
                              "fdc": p["fdc"]} for p in parts],
              "attribution": {"provider": "USDA SR Legacy", "verification_state": "typical_serving_estimate",
                              "cache_allowed": True}}
    if not_counted:
        result["not_counted_parts"] = not_counted
    return result


def estimate(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """A typical-serving estimate for one meal item, or None.

    Dishes ("breakfast burrito", "salad", "wrap"...) are built from their base
    plus the listed contents (or typical contents when none are listed).
    A single plain ingredient ("cheese", "sausage", "hash browns") gets its
    standard serving, or a count of pieces. Anything else returns None.
    """
    text = _norm(item.get("description") or "")
    if not text:
        return None
    unit = (str(item.get("unit")).casefold() if item.get("unit") else None)
    dish_match = _DISH_PATTERN.search(text)
    if dish_match is None:
        return _single(text, item, unit)
    dish_key = dish_match[1]
    dish = _D[dish_key]
    if dish.get("alias_of"):
        dish = _D[dish["alias_of"]]
    size, size_stated = _size_of(text, item.get("size"))
    factor = SIZE_FACTORS[size]
    before, after = text[:dish_match.start()], text[dish_match.end():]
    if dish_key == "salad" and re.search(r"\b(?:egg|tuna|chicken|potato|pasta|fruit|macaroni|taco|"
                                         r"seafood|crab|shrimp|ham)\s*$", before):
        return None  # "egg salad" / "tuna salad" are their own (mayo) dishes, not greens
    rest = (before + " , " + after).strip(" ,")
    listed, unknown, negated = _contents(rest)
    # Contents said after the dish ("salad with bacon") describe this one;
    # a name before it ("chicken caesar wrap") may be a real menu item, so
    # a source lookup goes first.
    said_after = bool(_contents(after)[0]) or bool(_NEGATED.search(after))
    base_keys = {key for key, *_ in dish["base"]}
    stated = {key: (extra, grams) for key, extra, grams in listed}
    parts = []
    for key, grams, by_size in dish["base"]:
        if key in negated:
            continue
        extra, counted = stated.get(key, (1.0, None))
        parts.append(_part(key, counted or ((by_size or {}).get(size) or grams * factor) * extra))
    listed_contents = [(k, f, g) for k, f, g in listed if k not in base_keys]
    typical = not listed_contents and not unknown
    contents = ([(k, 1.0, None) for k in dish["typical"] if k not in negated] if typical else listed_contents)
    if dish_key == "breakfast burrito" and not typical and "egg" not in negated \
            and not any(k in {"egg", "boiled egg"} for k, *_ in contents):
        contents.insert(0, ("egg", 1.0, None))  # a breakfast burrito has egg unless told otherwise
    for key, extra, counted in contents:
        parts.append(_part(key, counted or _row(key)[3] * factor * extra))
    if not parts:
        return None
    total = sum(p["grams"] for p in parts)
    amount = _amount(item.get("quantity"), unit, total)
    if amount is None:
        return None
    multiplier, scaled = amount
    name = f"{size} {dish_key}" if dish_key != "omelet" else f"{size} omelette"
    what = ", ".join(p["label"] for p in parts)
    notes = [f"{'typical recipe' if typical else 'built from'}: {what}"]
    if not size_stated:
        notes.insert(0, "assumed medium")
    if scaled:
        notes.append(scaled)
    if dish_key in {"salad", "burrito bowl"} and not any(p["key"] in DRESSINGS for p in parts):
        notes.append("no dressing counted")
    return _result(name, parts, multiplier, "; ".join(notes), unknown, size, listed=said_after)


def _single(text: str, item: Mapping[str, Any], unit: str | None) -> dict[str, Any] | None:
    """A plain ingredient named on its own ("cheese", "2 sausage links")."""
    words = " ".join(_FILLER.sub(" ", text).split())
    key = _INGREDIENT_ALIASES.get(words)
    if key is None:
        return None
    label, _, _, _, serving_g, serving_label, count_g, _ = _row(key)
    quantity = item.get("quantity")
    if unit in _MASS_GRAMS:
        grams = float(quantity or 1) * _MASS_GRAMS[unit]
        note = f"{grams:g} g"
    elif unit in (None, "") or unit in _DISH_UNITS or unit in {"slice", "slices", "link", "links", "strip",
                                                                 "strips", "patty", "patties"}:
        if count_g and unit not in {"serving", "servings"}:
            # A count food is counted in pieces; no number at all is one piece
            # (a bare plural already got its typical count from typical_count).
            pieces = float(quantity) if quantity is not None else 1.0
            grams, note = pieces * count_g, f"{pieces:g} x {count_g:g} g"
        else:
            count = float(quantity) if quantity is not None else 1.0
            grams = count * serving_g
            note = f"standard serving {serving_label}" + (f" x{count:g}" if count != 1 else "")
    else:
        return None
    if not 0 < grams <= 3000:
        return None
    part = _part(key, grams)
    return _result(label, [part], 1.0, note, [], None)


# A count food named with no amount at all ("eggs", "bacon"): a typical
# serving is this many pieces. A singular ("an egg") is one, as stated.
_TYPICAL_COUNTS = {"eggs": 2, "scrambled eggs": 2, "boiled eggs": 2, "hard boiled eggs": 2,
                   "bacon": 3, "crispy bacon": 3, "sausage": 2, "sausages": 2, "sausage links": 2,
                   "breakfast sausage": 2}


def typical_count(description: str) -> int | None:
    """Pieces in a typical serving of a bare count food, else None."""
    return _TYPICAL_COUNTS.get(" ".join(_FILLER.sub(" ", _norm(description)).split()))


def dish_name(description: str) -> str | None:
    """The dish word(s) alone ("breakfast burrito") from a described dish, or None."""
    match = _DISH_PATTERN.search(_norm(description))
    return match[1] if match else None
