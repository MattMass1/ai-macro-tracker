"""Where a food came from decides where its numbers come from.

Every meal item is routed before any lookup, into exactly one lane:

* ``restaurant`` - ordered at a restaurant, fast-food chain or takeout
  ("Big Mac", "Chipotle bowl"). Menus publish nutrition, so the item is looked
  up online under the place's name and the receipt says what it matched.
* ``packaged`` - a store-bought product with a brand ("Kodiak waffles", "Chobani
  yogurt", "Lean Cuisine"). Looked up as that product (label databases first).
* ``homemade`` - cooked at home or a generic dish with no place ("I made a
  breakfast burrito", "salad with bacon and cheese"). A dish name means
  nothing online for someone's own cooking, so a known dish is built from a
  typical recipe; a plain ingredient uses the generic food databases.

The model sets the lane from the conversation (it knows "I'm at Chipotle" was
said two messages ago). When it doesn't, the server infers it from the words:
a known chain name means restaurant, "homemade"/"I made" means homemade, and
anything else stays unrouted (the previous behavior). The user is never asked
where a food came from.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

KINDS = frozenset({"restaurant", "packaged", "homemade"})

# Display name -> words that name it. Only names that are clearly a place.
CHAINS: dict[str, tuple[str, ...]] = {
    "McDonald's": ("mcdonalds", "mcdonald", "mickey ds", "mcd"),
    "Chick-fil-A": ("chick fil a", "chickfila", "chik fil a", "cfa"),
    "Chipotle": ("chipotle mexican grill", "chipotle"),
    "Taco Bell": ("taco bell",),
    "Wendy's": ("wendys",),
    "Burger King": ("burger king",),
    "Subway": ("subway",),
    "Starbucks": ("starbucks",),
    "Dunkin'": ("dunkin donuts", "dunkin"),
    "Panera": ("panera bread", "panera"),
    "Raising Cane's": ("raising canes", "raising cane", "canes"),
    "Chili's": ("chilis",),
    "Applebee's": ("applebees",),
    "Olive Garden": ("olive garden",),
    "Panda Express": ("panda express",),
    "Five Guys": ("five guys",),
    "In-N-Out": ("in n out", "in and out", "innout"),
    "Shake Shack": ("shake shack",),
    "Popeyes": ("popeyes",),
    "KFC": ("kfc", "kentucky fried chicken"),
    "Wingstop": ("wingstop",),
    "Domino's": ("dominos",),
    "Pizza Hut": ("pizza hut",),
    "Papa John's": ("papa johns",),
    "Little Caesars": ("little caesars", "little caesar"),
    "Jersey Mike's": ("jersey mikes",),
    "Jimmy John's": ("jimmy johns",),
    "Firehouse Subs": ("firehouse subs", "firehouse"),
    "Qdoba": ("qdoba",),
    "Moe's": ("moes southwest grill", "moes"),
    "Cava": ("cava",),
    "Sweetgreen": ("sweetgreen",),
    "Zaxby's": ("zaxbys",),
    "Culver's": ("culvers",),
    "Sonic": ("sonic drive in", "sonic"),
    "Arby's": ("arbys",),
    "Jack in the Box": ("jack in the box",),
    "Whataburger": ("whataburger",),
    "Cracker Barrel": ("cracker barrel",),
    "IHOP": ("ihop",),
    "Denny's": ("dennys",),
    "Waffle House": ("waffle house",),
    "Buffalo Wild Wings": ("buffalo wild wings", "bdubs", "bww"),
    "Texas Roadhouse": ("texas roadhouse",),
    "Outback": ("outback steakhouse", "outback"),
    "Cheesecake Factory": ("cheesecake factory",),
    "Tropical Smoothie Cafe": ("tropical smoothie cafe", "tropical smoothie"),
    "Smoothie King": ("smoothie king",),
    "Publix": ("publix deli", "pub sub", "publix"),
    "Wawa": ("wawa",),
    "Sheetz": ("sheetz",),
    "Costco": ("costco food court", "costco"),
}
_CHAIN_WORDS = {alias: name for name, aliases in CHAINS.items() for alias in aliases}
_CHAIN_PATTERN = re.compile(r"\b(?:from|at|of)?\s*\b(" + "|".join(
    sorted((re.escape(a) for a in _CHAIN_WORDS), key=len, reverse=True)) + r")\b")
_HOMEMADE = re.compile(r"\b(?:home ?made|home[- ]cooked|home cooking|i made|i cooked|made it myself|"
                       r"my own|from scratch|leftovers?)\b")


def _norm(text: Any) -> str:
    text = str(text or "").casefold().replace("’", "'").replace("&", " and ")
    text = re.sub(r"'s\b", "s", text).replace("'", "")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def clean_place(value: Any) -> str | None:
    """A place/brand name as the user would read it, or None."""
    text = " ".join(str(value or "").split()).strip(" .,")
    if not text:
        return None
    if not 1 <= len(text) <= 60 or any(ord(c) < 32 for c in text):
        raise ValueError("place must be a short restaurant or brand name")
    return _CHAIN_WORDS.get(_norm(text), text)


def chain_in(description: str) -> str | None:
    match = _CHAIN_PATTERN.search(_norm(description))
    return _CHAIN_WORDS[match[1]] if match else None


def route(description: str, kind: str | None, place: str | None) -> tuple[str | None, str | None, str | None]:
    """(lane, place, how it was decided: 'model' | 'inferred' | None)."""
    if kind in KINDS:
        if kind == "homemade":
            return "homemade", None, "model"
        return kind, place or (chain_in(description) if kind == "restaurant" else None), "model"
    chain = chain_in(description)
    if chain:
        return "restaurant", chain, "inferred"
    if _HOMEMADE.search(_norm(description)):
        return "homemade", None, "inferred"
    return None, None, None


def names_place(description: str, place: str | None) -> bool:
    """Whether the description already says the place/brand."""
    if not place:
        return True
    words = _norm(place)
    return bool(words) and (words in _norm(description) or chain_in(description) == place)


def lookup_description(item: Mapping[str, Any]) -> str:
    """What to search for: the place's name plus the item ("McDonald's Big Mac")."""
    origins = item.get("field_origins") or {}
    place = origins.get("place")
    if origins.get("source_kind") in {"restaurant", "packaged"} and not names_place(item["description"], place):
        return f"{place} {item['description']}"
    return item["description"]
