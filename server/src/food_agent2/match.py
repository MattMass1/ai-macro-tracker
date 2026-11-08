"""Comparing what the user named with what a source returned.

Two questions, kept apart:

* :func:`conflict`: is this a *different* food? Species, lean ratio, a stated
  flavor, or a stated variant (diet, double). These are hard guards: such a
  candidate is refused, however close the words look.
* :func:`closeness`: how near is it otherwise? Anything that passes the guards
  and clears :data:`MIN_CLOSENESS` is used and disclosed as "matched to X". The
  user named a product, so the closest real one wins and costs at most one
  correction. The agent never asks which one.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

import food_words
from food_agent.contracts import FoodIdentity, Portion

MIN_CLOSENESS = 0.5
_EXTRA_WORD_PENALTY = 0.02
_TYPO_RATIO = 0.8

_CONNECTIVES = frozenset({"and", "with", "w", "of", "a", "an", "the", "on", "in", "plus", "style"})
# Words that say what kind of thing it is, not which one.
_CATEGORY = frozenset({"sandwich", "meal", "combo", "item", "food", "product", "dish", "entree"})
_SPECIES = {"chicken": "chicken", "turkey": "turkey", "beef": "beef", "steak": "beef", "pork": "pork",
            "ham": "pork", "bacon": "pork", "lamb": "lamb", "tuna": "tuna", "salmon": "salmon", "shrimp": "shrimp",
            "prawn": "shrimp", "cod": "cod", "tilapia": "tilapia", "halibut": "halibut", "trout": "trout",
            "tofu": "tofu", "venison": "venison", "bison": "bison", "duck": "duck"}
_FLAVORS = frozenset({"chocolate", "vanilla", "strawberry", "banana", "caramel", "mocha", "mint", "lemon", "orange",
                      "cherry", "blueberry", "raspberry", "coconut", "cinnamon", "peach", "mango", "pumpkin"})
# Stated by the user, these make a different product if the candidate lacks them.
_VARIANTS = frozenset({"diet", "zero", "sugarfree", "unsweetened", "decaf", "double", "triple", "jr", "junior",
                       "skim", "nonfat", "keto", "vegan", "impossible", "beyond"})
_COOKED = frozenset({"cooked", "grilled", "fried", "baked", "roasted", "boiled", "steamed", "sauteed", "smoked",
                     "broiled", "poached", "scrambled", "seared", "braised", "stewed", "rotisserie", "barbecued",
                     "bbq", "panfried", "airfried"})
_RAW = frozenset({"raw", "uncooked"})
# Foods whose weight changes with cooking: a bare weight needs raw or cooked.
_COOKABLE = frozenset({
    "chicken", "beef", "steak", "pork", "turkey", "lamb", "veal", "bison", "venison", "duck", "salmon", "tuna", "fish",
    "cod", "tilapia", "halibut", "trout", "shrimp", "prawn", "mince", "ground", "rice", "pasta", "spaghetti", "penne",
    "macaroni", "noodle", "quinoa", "oat", "oatmeal", "couscous", "lentil", "bean", "chickpea", "potato", "barley",
    "bulgur", "farro",
})
# Preparation words that describe a counted food without changing which food it is.
_PREPARATION_PHRASES = re.compile(
    r"\b(?:sunny[\s-]+side[\s-]+up|over[\s-]+(?:easy|medium|hard)|soft[\s-]+boiled|hard[\s-]+boiled|poached|scrambled|"
    r"fried|toasted|grilled|baked|roasted|boiled|steamed|homemade|home[\s-]+made|plain)\b", re.IGNORECASE)
_RATIO = re.compile(r"\b(\d{2,3})\s*(?:/|-)\s*(\d{1,2})\b|\b(\d{2,3})\s*%")
_STAPLES = frozenset({"bagel", "bread", "toast", "pizza", "rice", "tortilla", "muffin", "onion", "potato", "yogurt",
                      "cheese", "milk", "pasta", "cereal", "oatmeal", "apple", "banana", "orange", "sauce", "cracker",
                      "chip", "cookie", "pancake", "waffle", "biscuit", "butter", "jam", "honey"})


def _plain(text: Any) -> str:
    """Folded text in the shared food wording ("personal" is small, `10"` is 10 inch)."""
    text = food_words.normalize(str(text or ""))
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"['`]", "", folded).replace("&", " and ")


def _stem(word: str) -> str:
    if word.isdigit():
        return word
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes", "oes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us")):
        return word[:-1]
    return word


def tokens(text: Any) -> set[str]:
    """Stemmed words that identify a food."""
    return {_stem(word) for word in re.findall(r"[a-z0-9]+", _plain(text)) if word not in _CONNECTIVES}


def _same_word(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) < 4 or left[0] != right[0]:
        return False
    return SequenceMatcher(None, left, right).ratio() >= _TYPO_RATIO


def head_present(asked: Any, candidate: Any) -> bool:
    """The food's head word survives: "blueberry bagel" never becomes "blueberry muffin"."""
    words = [_stem(word) for word in re.findall(r"[a-z0-9]+", _plain(asked)) if word not in _CONNECTIVES]
    return bool(words) and any(_same_word(words[-1], other) for other in tokens(candidate))


def closeness(asked: Any, candidate: Any, brand: Any = "", *, extra_penalty: float = _EXTRA_WORD_PENALTY) -> float:
    """1.0 means every asked word is there. Extra candidate words cost a little.

    Brand words are not the food, except the food's own head word: "pizza" in
    "Domino's Pizza" never erases the pizza the user asked for.
    """
    asked_words = [_stem(word) for word in re.findall(r"[a-z0-9]+", _plain(asked)) if word not in _CONNECTIVES]
    brand_tokens = tokens(brand) - set(asked_words[-1:])
    wanted = tokens(asked) - brand_tokens
    wanted = (wanted - _CATEGORY) or wanted
    offered = tokens(candidate) - brand_tokens - _CATEGORY
    if not wanted or not offered:
        return 0.0
    found = {word for word in wanted if any(_same_word(word, other) for other in offered)}
    extras = [other for other in offered if not any(_same_word(word, other) for word in wanted)]
    return max(0.0, len(found) / len(wanted) - extra_penalty * len(extras)) if found else 0.0


def _ratios(text: Any) -> set[tuple[str, ...]]:
    return {tuple(part for part in found if part) for found in _RATIO.findall(str(text or ""))}


def conflict(asked: Any, candidate: Any) -> str | None:
    """Why ``candidate`` is a different food, or ``None``. A stated size or diameter that the
    candidate states differently ("large" vs "small", 10 vs 14 inch) is a different food."""
    wanted, offered = tokens(asked), tokens(candidate)
    wanted_species = {_SPECIES[word] for word in wanted if word in _SPECIES}
    offered_species = {_SPECIES[word] for word in offered if word in _SPECIES}
    if wanted_species and offered_species and not wanted_species & offered_species:
        return "species"
    wanted_ratio, offered_ratio = _ratios(asked), _ratios(candidate)
    if wanted_ratio and offered_ratio and not wanted_ratio & offered_ratio:
        return "ratio"
    wanted_flavor, offered_flavor = wanted & _FLAVORS, offered & _FLAVORS
    if wanted_flavor and offered_flavor and not wanted_flavor & offered_flavor:
        return "flavor"
    if (wanted & _VARIANTS) - offered:
        return "variant"
    wanted_size, offered_size = wanted & food_words.SIZES, offered & food_words.SIZES
    if wanted_size and offered_size and not wanted_size & offered_size:
        return "size"
    wanted_inches, offered_inches = food_words.inches(_plain(asked)), food_words.inches(_plain(candidate))
    if wanted_inches and offered_inches and not wanted_inches & offered_inches:
        return "size"
    return None


def _cook_state(words: set[str]) -> str | None:
    if words & _RAW:
        return "raw"
    return "cooked" if words & _COOKED else None


def cook_conflict(asked: Any, candidate: Any) -> bool:
    """A raw panel for a food stated cooked, or the reverse."""
    wanted, offered = _cook_state(tokens(asked)), _cook_state(tokens(candidate))
    return bool(wanted and offered and wanted != offered)


def asked_text(identity: FoodIdentity) -> str:
    return " ".join(part for part in (identity.name, identity.variant, identity.preparation) if part)


def without_preparation(query: str) -> str:
    """"sunny side up eggs" -> "eggs". Used only for foods that are counted, never weighed."""
    return " ".join(_PREPARATION_PHRASES.sub(" ", query).split()) or query


def staple_head(name: str) -> str | None:
    """The last word when it is a common food a database row can stand in for."""
    words = re.findall(r"[a-z]+", _plain(name))
    return words[-1] if words and _stem(words[-1]) in _STAPLES else None


def needs_raw_or_cooked(identity: FoodIdentity, portion: Portion) -> bool:
    """Only a weighed, unbranded food whose weight changes with cooking."""
    if portion.kind != "mass" or identity.brand or identity.kind in ("restaurant", "packaged"):
        return False
    words = tokens(asked_text(identity))
    return _cook_state(words) is None and bool(words & _COOKABLE)


def differs(asked: Any, shown: Any) -> bool:
    """Whether the receipt should say which food it matched."""
    return tokens(asked) - _CATEGORY != tokens(shown) - _CATEGORY


__all__ = ["MIN_CLOSENESS", "tokens", "closeness", "head_present", "conflict", "cook_conflict", "asked_text",
           "without_preparation", "staple_head", "needs_raw_or_cooked", "differs"]
