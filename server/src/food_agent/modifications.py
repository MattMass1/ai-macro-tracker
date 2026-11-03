"""How the agent shows item changes ("no tomato", "extra patty").

Nutrition sources describe the standard recipe, and code never invents numbers
for an adjustment. A changed item is therefore counted as the standard recipe
and the preview says so in plain words, instead of blocking the meal.
Taking off a garnish (lettuce, tomato, pickles ...) barely moves the numbers,
so it gets the shortest note. Anything the user adds on top is its own item
(see the interpreter rules), so it is looked up rather than guessed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from food_agent.contracts import DraftItemState, Modification

_GARNISH = frozenset({
    "lettuce", "tomato", "tomatoes", "pickle", "pickles", "onion", "onions", "red onion", "red onions",
    "jalapeno", "jalapenos", "pepper", "peppers", "green peppers", "banana peppers", "spinach",
    "cucumber", "cucumbers", "sprouts", "cilantro", "herbs", "arugula", "mustard", "hot sauce", "salt",
})
_LEADING = ("no ", "extra ", "add ", "light ", "swap ", "the ", "a ", "some ", "any ")
_VERB = {"remove": "no", "extra": "extra", "add": "add", "light": "light", "swap": "swap"}


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z,&/ ]+", " ", folded).strip()


def _subject(text: str) -> str:
    """The thing changed, without a repeated verb ("no tomato" with op remove -> "tomato")."""
    subject = " ".join(text.split())
    changed = True
    while changed:
        changed = False
        for lead in _LEADING:
            if subject.casefold().startswith(lead):
                subject, changed = subject[len(lead):].strip(), True
    return subject or text.strip()


def mod_text(mod: Modification) -> str:
    """Short words for one change: "no tomato", "extra beef patty"."""
    return f"{_VERB.get(mod.op, mod.op)} {_subject(mod.text)}"


def is_minor(mod: Modification) -> bool:
    """True when every part named is a garnish, so the standard numbers stay fair."""
    parts = [_subject(part.strip()) for part in re.split(r",|&|/|\band\b|\bor\b", _plain(mod.text))]
    parts = [part for part in parts if part]
    return bool(parts) and all(part in _GARNISH for part in parts)


def adjustment_note(items: Sequence[DraftItemState]) -> str | None:
    """One plain sentence for the preview, or None when nothing was changed."""
    mods = [mod for item in items for mod in item.modifications]
    if not mods:
        return None
    major = list(dict.fromkeys(mod_text(mod) for mod in mods if not is_minor(mod)))
    if not major:
        return "counted as the standard recipe"
    joined = major[0] if len(major) == 1 else ", ".join(major[:-1]) + " or " + major[-1]
    return f"counted as the standard recipe. I can't adjust for {joined} yet"


__all__ = ["adjustment_note", "is_minor", "mod_text"]
