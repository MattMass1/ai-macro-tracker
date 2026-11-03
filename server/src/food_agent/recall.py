"""``recall_meal``: find a meal in the user's OWN saved history by its food words.

Real failure 1: "I had an ahi tuna bowl again, the one I had last time" logged
the newest unrelated entry (two Burger King wraps). Here:

* Only the current user's own ``nutrition_entries`` are searched, through
  ``Store.search_own_entries`` (owner-scoped by ``current_user_id()``). The
  recaller never takes a user id.
* Every food word of the query must match the entry's name tokens (or, for
  food-agent rows, an item's identity in ``component_metadata``). Filler such
  as "again", "the one I had", "last time" is dropped first. Matching is case
  and punctuation insensitive, folds plurals, and tolerates one small typo in
  longer words. There is NO fallback to the most recent entry: no match means
  ``not_found`` with an honest question.
* One distinct food (possibly on several dates) is ``found``: the newest
  matching row (inside the ``when_hint`` day window when one is given) becomes
  draft items whose panel is the saved row's nutrients for one logged portion.
  Several distinct foods is ``needs_detail`` with ONE plain question naming at
  most three of them inline.

Days follow the app's logging day (``domain.effective_date``: local time, the
day rolls at 4am), resolved with the injected clock.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import domain
from food_agent.contracts import (NUTRIENT_KEYS, DraftItemState, FoodIdentity, NutrientPanel, Portion,
                                  ResolvedFood, ServingBasis)
from food_agent.ports import RecallResult

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 120
SEARCH_LIMIT = 200
MAX_OPTIONS = 3

# Words that say "which time" or "how it was said", never "what food".
FILLER = frozenset("""
a an the one ones some any i im ive id me my mine we our you your it its that this those these there
had have has having ate eat eaten eating got get getting grabbed grab ordered order made make did do
again last time times same as like usual previous previously before earlier recently recent other another
just also too please log logged add track record put enter was were is are be been from at on in for to of
and with or but day days ago week weekend month morning afternoon evening night tonight today yesterday
kind sort thing stuff meal food item thanks thank lol ok okay yeah yes
""".split())
SOFT = frozenset({"breakfast", "brunch", "lunch", "dinner", "supper", "snack", "dessert"})

WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5,
            "sat": 5, "sunday": 6, "sun": 6}
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
          "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_WORDS = frozenset({"january", "february", "march", "april", "june", "july", "august", "september",
                          "october", "november", "december", *MONTHS})
_DATE_WORDS = frozenset(WEEKDAYS) | _MONTH_WORDS | {"st", "nd", "rd", "th"}

Window = tuple[date, date]


# ---------------------------------------------------------------- words


def _ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def raw_words(text: str) -> list[str]:
    """Lowercase words; apostrophes vanish ("McDonald's" -> "mcdonalds"), other punctuation splits."""
    text = _ascii(str(text or "")).casefold().replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9]+", text)


def fold(word: str) -> str:
    """Fold a plural to its singular form, the same way for query and entry."""
    if len(word) <= 3 or word.isdigit():
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "sses", "zes", "oes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def food_words(query: str) -> list[str]:
    """The food words of a recall query, folded, in order, without filler, dates or numbers."""
    out: list[str] = []
    for word in raw_words(query):
        if word in FILLER or word in SOFT or word in _DATE_WORDS or word.isdigit():
            continue
        if re.fullmatch(r"\d+(st|nd|rd|th|am|pm)", word):
            continue
        folded = fold(word)
        if folded not in out and folded not in FILLER:
            out.append(folded)
    return out


def _phrase(query: str) -> str:
    """The user's food words as they said them (unfolded), for honest replies."""
    kept = [w for w in raw_words(query)
            if w not in FILLER and w not in SOFT and w not in _DATE_WORDS and not w.isdigit()]
    return " ".join(dict.fromkeys(kept))


def _osa(a: str, b: str) -> int:
    """Optimal string alignment distance (Levenshtein plus adjacent transposition)."""
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def _close(query_word: str, token: str) -> bool:
    """A small typo, never a different food word. The first letter must match.
    4-5 letters: only two adjacent letters swapped ("tuan"/"tuna"), so "mocha" never
    reaches "mochi" nor "jerky" "jerk". 6-7 letters: one edit; 8+: two edits.
    Shorter words must be exact. A typo match only ever leads to a question."""
    size = len(query_word)
    if size < 4 or not token or token[0] != query_word[0] or abs(size - len(token)) > (2 if size >= 8 else 1):
        return False
    distance = _osa(query_word, token)
    if size <= 5:
        return len(token) == size and distance == 1 and sorted(query_word) == sorted(token)
    return distance <= (2 if size >= 8 else 1)


def match(query: Sequence[str], tokens: Sequence[str]) -> str | None:
    """``"exact"`` or ``"fuzzy"`` when EVERY query word matches an entry token, else ``None``.

    Two adjacent words on either side may also join ("cheese burger" ~ "cheeseburger").
    """
    if not query or not tokens:
        return None
    exact_set = set(tokens)
    joined = {fold(a + b) for a, b in zip(tokens, tokens[1:])}
    quality = "exact"
    index = 0
    while index < len(query):
        word = query[index]
        if word in exact_set or word in joined:
            index += 1
            continue
        if index + 1 < len(query) and fold(word + query[index + 1]) in exact_set:
            index += 2
            continue
        if any(_close(word, token) for token in exact_set | joined):
            quality = "fuzzy"
            index += 1
            continue
        return None
    return quality


def tokens_of(*texts: Any) -> list[str]:
    out: list[str] = []
    for text in texts:
        if text:
            out.extend(fold(w) for w in raw_words(str(text)))
    return out


def _search_terms(words: Sequence[str]) -> list[str]:
    """Coarse SQL prefilter terms: a 3-letter prefix of each food word (OR-ed by the store),
    so plural forms and late typos still reach the exact matching above."""
    return list(dict.fromkeys(word[:3] for word in words if len(word) >= 3))[:12] or list(words)[:12]


# ---------------------------------------------------------------- when hints


def _latest(month: int, day: int, today: date, year: int | None = None) -> date | None:
    try:
        if year is not None:
            return date(year if year > 99 else 2000 + year, month, day)
        found = date(today.year, month, day)
        return found if found <= today else date(today.year - 1, month, day)
    except ValueError:
        return None


def resolve_when(hint: str | None, today: date) -> list[Window]:
    """Day windows (inclusive) a hint names, best first. ``[]`` means "any recent day".

    ``today`` is the user's logging day. "last Thursday" is the most recent
    Thursday before today (then the one before that); a plain "Thursday" may
    be today. Vague hints ("last time", "the other day") do not narrow.
    """
    text = _ascii(str(hint or "")).casefold().strip()
    if not text:
        return []
    one = lambda d: [(d, d)]  # noqa: E731
    if re.search(r"\bday before yesterday\b", text):
        return one(today - timedelta(days=2))
    if re.search(r"\b(yesterday|last night)\b", text):
        return one(today - timedelta(days=1))
    if re.search(r"\b(today|tonight|this morning|this afternoon|this evening)\b", text):
        return one(today)
    ago = re.search(r"\b(\d{1,3}|a|one|two|three|four|five|six)\s+days?\s+ago\b", text)
    if ago:
        words = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
        count = words.get(ago[1]) or int(ago[1])
        return one(today - timedelta(days=count))
    if re.search(r"\b(a|one) week ago\b", text):
        return one(today - timedelta(days=7))
    if re.search(r"\blast week\b", text):
        start = today - timedelta(days=today.weekday() + 7)
        return [(start, start + timedelta(days=6))]
    if re.search(r"\bthis week\b", text):
        return [(today - timedelta(days=today.weekday()), today)]
    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        found = _latest(int(iso[2]), int(iso[3]), today, int(iso[1]))
        return one(found) if found else []
    slash = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if slash:
        found = _latest(int(slash[1]), int(slash[2]), today, int(slash[3]) if slash[3] else None)
        return one(found) if found else []
    month_re = "|".join(sorted(MONTHS, key=len, reverse=True))
    named = re.search(rf"\b({month_re})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text) \
        or re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_re})[a-z]*\b", text)
    if named:
        month_text, day_text = (named[1], named[2]) if named[1].isalpha() else (named[2], named[1])
        found = _latest(MONTHS[month_text], int(day_text), today)
        return one(found) if found else []
    for word in re.findall(r"[a-z]+", text):
        if word in WEEKDAYS:
            back = (today.weekday() - WEEKDAYS[word]) % 7
            if re.search(rf"\blast\s+{word}\b", text):
                back = back or 7
                return [(today - timedelta(days=back),) * 2, (today - timedelta(days=back + 7),) * 2]
            return one(today - timedelta(days=back))
    return []


def _day_label(day: date) -> str:
    return f"{day:%A}, {day:%b} {day.day}"


def _window_label(window: Window) -> str:
    low, high = window
    return _day_label(low) if low == high else f"{low:%b} {low.day} to {high:%b} {high.day}"


# ---------------------------------------------------------------- candidates


@dataclass(frozen=True)
class _Candidate:
    row: Mapping[str, Any]
    day: date
    name: str
    key: frozenset[str]
    quality: str
    meta_items: tuple[Mapping[str, Any], ...] | None  # food-agent items to recall, or None
    whole_entry: bool


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _meta_items(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    meta = row.get("component_metadata")
    if not isinstance(meta, Mapping) or meta.get("origin") != "food_agent":
        return []
    items = meta.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping) and isinstance(item.get("identity"), Mapping)]


def _item_tokens(item: Mapping[str, Any]) -> list[str]:
    ident = item["identity"]
    return tokens_of(ident.get("brand"), ident.get("name"), ident.get("variant"), item.get("display_name"))


def _key(name: str) -> frozenset[str]:
    return frozenset(token for token in tokens_of(name) if not token.isdigit())


def _candidates(rows: Sequence[Mapping[str, Any]], words: Sequence[str]) -> list[_Candidate]:
    out: list[_Candidate] = []
    for row in rows:
        day = _as_date(row.get("day"))
        name = str(row.get("name") or "").strip()
        if day is None or not name or not row.get("id"):
            continue
        items = _meta_items(row)
        matched = [(item, q) for item in items if (q := match(words, _item_tokens(item)))]
        if matched:
            chosen = tuple(item for item, _ in matched)
            whole = len(chosen) == len(items)
            label = name if whole else " + ".join(
                str(item.get("display_name") or item["identity"].get("name")) for item in chosen)
            quality = "exact" if all(q == "exact" for _, q in matched) else "fuzzy"
            out.append(_Candidate(row, day, label, _key(label), quality, chosen, whole))
            continue
        quality = match(words, tokens_of(name))
        if quality:
            out.append(_Candidate(row, day, name, _key(name), quality, tuple(items) or None, True))
    out.sort(key=lambda c: (c.day, str(c.row.get("created_at") or "")), reverse=True)
    return out


# ---------------------------------------------------------------- draft items


def _nutrient(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100000 else None


def _panel(source: Mapping[str, Any], unknown: Sequence[str] = ()) -> NutrientPanel:
    return NutrientPanel(**{key: None if key in unknown else _nutrient(source.get(key)) for key in NUTRIENT_KEYS})


def _source(*options: Any, fallback: str) -> str:
    for option in options:
        try:
            if option:
                return domain.validate_macro_source(str(option)[:200])
        except domain.MacroError:
            continue
    return fallback[:200]


def _identity(raw: Any, name: str) -> FoodIdentity:
    if isinstance(raw, Mapping):
        try:
            return FoodIdentity.model_validate(dict(raw), strict=False)
        except ValueError:
            pass
    return FoodIdentity(name=name[:120].strip() or "meal", kind="history")


def _basis(day: date, meta_item: Mapping[str, Any] | None) -> ServingBasis:
    """One logged portion. Grams are kept when the saved item knew them, so a later
    "6 oz of it" can still be scaled; otherwise mass needs a question."""
    grams = None
    if meta_item is not None:
        basis = meta_item.get("serving_basis") if isinstance(meta_item.get("serving_basis"), Mapping) else {}
        per = _nutrient(basis.get("grams"))
        mult = _nutrient(meta_item.get("multiplier"))
        if per and mult and 0 < per * mult <= 10000:
            grams = round(per * mult, 1)
    return ServingBasis(text=f"one portion as logged on {_day_label(day)}", basis="per_serving",
                        amount=1.0, unit="count", grams=grams)


def _items(chosen: _Candidate) -> tuple[DraftItemState, ...]:
    row, day = chosen.row, chosen.day
    entry_id = str(row["id"])
    row_name = str(row["name"]).strip()
    fallback = f"Your log: {row_name}, {day.isoformat()}"
    one = Portion(kind="serving_fraction", amount=1.0, text="")
    meta = row.get("component_metadata") if isinstance(row.get("component_metadata"), Mapping) else {}
    row_unknown = [k for k in (meta.get("unknown_nutrients") or []) if k in NUTRIENT_KEYS]
    items = chosen.meta_items
    if not items:
        food = ResolvedFood(food_id=f"history:{entry_id}"[:200], provider="history", external_id=entry_id,
                            display_name=row_name[:200], macro_source=_source(row.get("macro_source"),
                                                                             fallback=fallback),
                            serving_basis=_basis(day, None), panel=_panel(row, row_unknown))
        return (DraftItemState(item_ref="r1", identity=_identity(None, row_name), portion=one,
                               resolved=food, multiplier=1.0),)
    single = chosen.whole_entry and len(items) == 1
    out = []
    for index, item in enumerate(items, start=1):
        display = str(item.get("display_name") or item["identity"].get("name") or row_name).strip()
        if single:  # the saved row is the truth; metadata only says which values were unknown
            panel = _panel(row, row_unknown)
            food_id = f"history:{entry_id}"
        else:
            nutrients = item.get("nutrients") if isinstance(item.get("nutrients"), Mapping) else {}
            unknown = [k for k in (item.get("unknown_nutrients") or []) if k in NUTRIENT_KEYS]
            panel = _panel(nutrients, unknown)
            food_id = f"history:{entry_id}:{item.get('item_ref') or index}"
        food = ResolvedFood(food_id=food_id[:200], provider="history", external_id=entry_id,
                            display_name=display[:200],
                            macro_source=_source(item.get("macro_source"), row.get("macro_source"),
                                                 fallback=fallback),
                            serving_basis=_basis(day, item), panel=panel)
        out.append(DraftItemState(item_ref=f"r{index}", identity=_identity(item.get("identity"), display),
                                  portion=one, resolved=food, multiplier=1.0))
    return tuple(out)


# ---------------------------------------------------------------- the recaller


def _with_article(phrase: str) -> str:
    if not phrase:
        return "that"
    last = phrase.split()[-1]
    if last.endswith("s") and not last.endswith("ss"):
        return phrase
    return ("an " if phrase[0] in "aeiou" else "a ") + phrase


def _options(names: Sequence[str]) -> str:
    shown = [f"the {name}" for name in names[:MAX_OPTIONS]] if len(names) <= MAX_OPTIONS else \
        [f"the {name}" for name in names[:MAX_OPTIONS - 1]] + ["another one"]
    return ", ".join(shown[:-1]) + " or " + shown[-1]


class HistoryRecaller:
    """``HistoryRecaller(store, clock=None)``; ``store`` has ``search_own_entries``.

    Implements :class:`food_agent.ports.MealRecaller`. Read-only: it never logs.
    """

    def __init__(self, store: Any, clock: Callable[[], datetime] | None = None, *,
                 lookback_days: int = LOOKBACK_DAYS, limit: int = SEARCH_LIMIT):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lookback_days = lookback_days
        self.limit = limit

    async def recall(self, query: str, when_hint: str | None) -> RecallResult:
        words = food_words(query)
        if not words:
            return RecallResult(status="needs_detail",
                                question="Which food was it, so I can find it in your log?")
        today = domain.effective_date(self.clock())
        windows = [w for w in (resolve_when(when_hint, today) or resolve_when(query, today)) if w[0] <= today]
        since = min([today - timedelta(days=self.lookback_days)] + [low for low, _ in windows])
        rows = await self.store.search_own_entries(_search_terms(words), since=since, until=today,
                                                   limit=self.limit)
        found = _candidates(rows or [], words)
        if any(c.quality == "exact" for c in found):
            found = [c for c in found if c.quality == "exact"]  # a typo match never beats a real one

        missed: Window | None = None
        if windows and found:
            for low, high in windows:
                inside = [c for c in found if low <= c.day <= high]
                if inside:
                    found = inside
                    break
            else:
                missed = windows[0]  # the food exists, just not on the day they named

        if not found:
            phrase = _with_article(_phrase(query))
            months = max(1, round(self.lookback_days / 30))
            question = (f"I couldn't find {phrase} in your log from the last {months} months. "
                        "Where was it from, or what was in it?")
            return RecallResult(status="not_found", note=question.split(". ")[0], question=question)

        foods: dict[frozenset[str], _Candidate] = {}
        for candidate in found:  # newest first: the first of each food is its latest
            foods.setdefault(candidate.key, candidate)
        if len(foods) > 1:
            exact = [c for key, c in foods.items() if key == frozenset(words)]
            if len(exact) == 1:
                foods = {exact[0].key: exact[0]}
        if len(foods) > 1:
            names = list(dict.fromkeys(c.name for c in foods.values()))
            question = f"Which one was it, {_options(names)}?"
            return RecallResult(status="needs_detail", question=question)

        chosen = next(iter(foods.values()))
        if chosen.quality != "exact":  # a typo match is a guess: ask, never preview it
            return RecallResult(status="needs_detail",
                                question=f"Did you mean the {chosen.name} from {_day_label(chosen.day)}?")
        if missed is not None:
            note = (f"I didn't see it on {_window_label(missed)}, so this is your {chosen.name} "
                    f"from {_day_label(chosen.day)}")
        else:
            note = f"same as your {chosen.name} on {_day_label(chosen.day)}"
        return RecallResult(status="found", items=_items(chosen), source_entry_id=str(chosen.row["id"]),
                            note=note)


__all__ = ["HistoryRecaller", "resolve_when", "food_words", "match", "fold", "LOOKBACK_DAYS"]
