"""Code-side detection of the user's OWN unambiguous yes or no.

This is the only authority for a commit. The model's ``confirm`` action is a
hint and never enough. A message is a plain yes only when every word in it
belongs to a short affirmation vocabulary ("yes", "yep", "ok", "log it",
"yes please" ...). Anything else, an amount, a food word, "but", a question,
makes it ``"other"``: "yes but 6 oz" edits the meal, it does not confirm it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

Decision = Literal["yes", "no", "other"]

MAX_CONFIRM_CHARS = 60

# Multi-word phrases first (longest match wins), then single words.
_YES_PHRASES: tuple[tuple[str, ...], ...] = tuple(sorted((
    ("yes",), ("yep",), ("yup",), ("yeah",), ("yea",), ("ya",), ("y",), ("sure",), ("ok",), ("okay",),
    ("k",), ("kk",), ("okey",), ("correct",), ("right",), ("confirm",), ("confirmed",), ("perfect",),
    ("great",), ("good",), ("yes",), ("absolutely",), ("definitely",), ("affirmative",), ("👍",),
    ("✅",), ("log", "it"), ("log", "that"), ("log", "this"), ("save", "it"), ("save", "that"),
    ("add", "it"), ("do", "it"), ("go", "ahead"), ("go", "for", "it"), ("sounds", "good"),
    ("looks", "good"), ("looks", "right"), ("that's", "right"), ("thats", "right"), ("that", "works"),
    ("all", "good"), ("of", "course"),
), key=len, reverse=True))
_NO_PHRASES: tuple[tuple[str, ...], ...] = tuple(sorted((
    ("no",), ("nope",), ("nah",), ("n",), ("cancel",), ("cancel", "it"), ("cancel", "that"),
    ("don't", "log", "it"), ("dont", "log", "it"), ("don't", "log", "that"), ("dont", "log", "that"),
    ("do", "not", "log", "it"), ("never", "mind"), ("nevermind",), ("forget", "it"), ("scrap", "it"),
    ("scrap", "that"), ("stop",), ("skip", "it"), ("don't",), ("dont",),
), key=len, reverse=True))
# Courtesy words that never change the meaning of a yes or a no.
_FILLER = frozenset({"please", "pls", "plz", "thanks", "thank", "you", "thx", "ty", "then", "and"})

_TOKEN = re.compile(r"[a-z']+|[\U0001F44D✅]")


def _tokens(text: str) -> list[str] | None:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    if not normalized.strip() or len(normalized) > MAX_CONFIRM_CHARS:
        return None
    if re.search(r"\d", normalized):
        return None  # any number is an amount: an edit, not a confirmation
    # Only letters, apostrophes, spaces, basic punctuation and the two emoji may appear.
    if re.search(r"[^a-z'\s.,!\U0001F44D✅️-]", normalized):
        return None
    return _TOKEN.findall(normalized)


def _covers(tokens: list[str], phrases: tuple[tuple[str, ...], ...]) -> bool:
    """True when ``tokens`` is a sequence of ``phrases`` and filler words, with at least one phrase."""
    i, matched = 0, False
    while i < len(tokens):
        for phrase in phrases:
            if tuple(tokens[i:i + len(phrase)]) == phrase:
                i += len(phrase)
                matched = True
                break
        else:
            if tokens[i] in _FILLER:
                i += 1
                continue
            return False
    return matched


def classify(message: str | None) -> Decision:
    """``"yes"`` / ``"no"`` only for an unambiguous short reply, else ``"other"``."""
    tokens = _tokens(message or "")
    if not tokens:
        return "other"
    if _covers(tokens, _YES_PHRASES):
        return "yes"
    if _covers(tokens, _NO_PHRASES):
        return "no"
    return "other"


def is_plain_yes(message: str | None) -> bool:
    return classify(message) == "yes"


__all__ = ["classify", "is_plain_yes", "Decision", "MAX_CONFIRM_CHARS"]
