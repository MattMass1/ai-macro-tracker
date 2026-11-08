"""How people say a food versus how a source names it, made the same before words are compared.

"Domino's 10 inch cheese pizza, whole pie" and a source's `Cheese Pizza - Small (10")`
are the same food, but compared word by word they are not. Four rewrites, applied to
both sides of every comparison:

* Inches: `10"`, `10in`, `10 in.`, `10-inch` and `10 inches` are all "10 inch".
* Sizes: "personal" and "individual" are the chain's small.
* Amounts said inside the name: "whole pie", "entire pizza", "pizza pie" are "pizza".
  How much was eaten is the portion's job, not the food's identity.
* The everyday default kind goes unsaid, as databases leave it unsaid: "chicken eggs" are
  "eggs", "cow's milk" is "milk" (:func:`default_kind`).

Pure: no I/O.
"""
from __future__ import annotations

import re

_INCH = re.compile(r"(\d+(?:\.\d+)?)\s*(?:\"|”|″|''|-?\s*inch(?:es)?\b|in\b\.?)", re.IGNORECASE)
_SIZE = re.compile(r"\b(?:personal|individual)\b", re.IGNORECASE)
_WHOLE_PIE = re.compile(r"\b(?:(?:a\s+)?(?:whole|entire|full)\s+(?:pizza\s+)?(?:pie|pizza)|pizza\s+pie)\b",
                        re.IGNORECASE)
_LONE_PIE = re.compile(r"\bpies?\b", re.IGNORECASE)
_WHOLE_WORDS = re.compile(r"\b(?:whole|entire|full)\b", re.IGNORECASE)

SIZES = frozenset({"small", "medium", "large", "xl"})


_DEFAULT_KIND = (
    (re.compile(r"\b(?:chicken|hen)(?:'?s)?\s+(eggs?)\b", re.IGNORECASE), r"\1"),
    (re.compile(r"\bcow(?:'?s)?\s+(milk)\b", re.IGNORECASE), r"\1"),
)


def default_kind(text: str) -> str:
    """Leave out the kind everybody means: "chicken eggs" -> "eggs", "cow's milk" -> "milk".
    A database row for the default never says it, so a search that requires it finds nothing."""
    out = str(text or "")
    for pattern, replacement in _DEFAULT_KIND:
        out = pattern.sub(replacement, out)
    return " ".join(out.split())


def normalize(text: str) -> str:
    """The same food in the same words, whoever wrote them."""
    out = _INCH.sub(lambda match: f"{match.group(1)} inch ", str(text or ""))
    out = _SIZE.sub("small", default_kind(out))
    if re.search(r"\bpizzas?\b", out, re.IGNORECASE) or _WHOLE_PIE.search(out):
        out = _WHOLE_PIE.sub("pizza", out)
        out = _LONE_PIE.sub(" ", out)  # "cheese pizza, whole pie": the pie is the pizza
        out = _WHOLE_WORDS.sub(" ", out)
    return " ".join(out.split())


def inches(text: str) -> set[str]:
    """The stated diameters, as numbers: {"10"} for "10 inch cheese pizza"."""
    return set(re.findall(r"(\d+(?:\.\d+)?) inch\b", normalize(text).casefold()))


__all__ = ["normalize", "default_kind", "inches", "SIZES"]
