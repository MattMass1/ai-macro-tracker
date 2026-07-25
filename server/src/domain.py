"""Pure business logic: the day boundary, validation, totals, and the Atwater check.

Nothing in this module touches the network. It is the single source of truth for
every rule the MCP tools and the REST API share — duplicating any of it in
TypeScript would let the two clients drift.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "America/New_York"))
DAY_ROLLOVER_HOUR = int(os.environ.get("DAY_ROLLOVER_HOUR", "4"))

MEALS = ("Breakfast", "Lunch", "Dinner", "Snack")

MACRO_KEYS = ("calories", "protein", "carbs", "fat")

MAX_CALORIES = 10_000.0
MAX_MACRO = 1_000.0

#: Values that mean "I made this up" rather than "here is where the numbers came from".
PLACEHOLDER_SOURCES = {
    "estimate",
    "estimated",
    "estimation",
    "approx",
    "approximate",
    "approximately",
    "guess",
    "guessed",
    "guesstimate",
    "unknown",
    "n/a",
    "na",
    "none",
    "null",
    "tbd",
    "idk",
    "made up",
    "rough",
    "rough estimate",
    "eyeball",
    "eyeballed",
    "best guess",
    "-",
    "?",
}

DEFAULT_TARGETS = {"calories": 2400.0, "protein": 215.0, "carbs": 200.0, "fat": 70.0}


class MacroError(ValueError):
    """A user-fixable problem. The message is relayed verbatim to the user by Poke."""


# --------------------------------------------------------------------------- #
# Day boundary
# --------------------------------------------------------------------------- #


def effective_date(now: datetime | None = None) -> date:
    """The logging day for a given instant. The day rolls at 4am local."""
    now = now or datetime.now(LOCAL_TZ)
    return (now.astimezone(LOCAL_TZ) - timedelta(hours=DAY_ROLLOVER_HOUR)).date()


def day_label(day: date) -> str:
    """Human label for a logging day, e.g. ``Saturday, July 25``.

    The PWA shows this instead of "Today" because between midnight and 4am the
    logging day and the calendar date disagree.
    """
    return f"{day.strftime('%A')}, {day.strftime('%B')} {day.day}"


def parse_date(value: str, field: str = "date") -> date:
    """Parse a ``YYYY-MM-DD`` string or raise an actionable error."""
    if not isinstance(value, str) or not value.strip():
        raise MacroError(f"{field} must be a YYYY-MM-DD string — got an empty value")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise MacroError(
            f"{field} must be formatted as YYYY-MM-DD — got {value!r}"
        ) from None


def resolve_date(value: str | None, field: str = "date") -> date:
    """`value` when given, otherwise the current effective day."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return effective_date()
    return parse_date(value, field)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def normalize_meal(meal: str | None, default: str = "Snack") -> str:
    """Case-insensitively map `meal` onto one of the four allowed select options."""
    if meal is None or not str(meal).strip():
        return default
    candidate = str(meal).strip().lower()
    for allowed in MEALS:
        if allowed.lower() == candidate:
            return allowed
    raise MacroError(
        f"meal must be one of {', '.join(MEALS)} — got {str(meal).strip()!r}"
    )


def validate_macro_source(macro_source: str) -> str:
    """Reject empty or placeholder sourcing. Macros are never estimated."""
    text = (macro_source or "").strip()
    if not text:
        raise MacroError(
            "macro_source is required — look up the product label or the FDA "
            "FoodData Central entry first, then pass where the numbers came from "
            '(e.g. "FDA FoodData Central: chicken breast, roasted").'
        )
    if text.lower().strip(" .!") in PLACEHOLDER_SOURCES:
        raise MacroError(
            f"macro_source {text!r} is a placeholder, not a source. Never estimate "
            "macros: look up the brand's nutrition label or the FDA FoodData Central "
            "entry and cite it (e.g. \"Fairlife Core Power label\")."
        )
    return text


def _validate_number(value: Any, field: str, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MacroError(f"{field} must be a number — got {value!r}") from None
    if math.isnan(number) or math.isinf(number):
        raise MacroError(f"{field} must be a finite number — got {value!r}")
    if number < 0:
        raise MacroError(f"{field} must be zero or greater — got {number:g}")
    if number > maximum:
        raise MacroError(
            f"{field} must be {maximum:g} or less for a single entry — got {number:g}. "
            "That looks like a typo; split the entry if it is genuinely that large."
        )
    return round(number, 2)


def validate_macros(
    calories: Any, protein: Any, carbs: Any, fat: Any
) -> dict[str, float]:
    """Validate the four macro numbers, returning them rounded and coerced to float."""
    return {
        "calories": _validate_number(calories, "calories", MAX_CALORIES),
        "protein": _validate_number(protein, "protein", MAX_MACRO),
        "carbs": _validate_number(carbs, "carbs", MAX_MACRO),
        "fat": _validate_number(fat, "fat", MAX_MACRO),
    }


def validate_name(name: str, field: str = "name") -> str:
    text = (name or "").strip()
    if not text:
        raise MacroError(f"{field} is required — describe the food in a few words")
    if len(text) > 200:
        raise MacroError(f"{field} must be 200 characters or fewer")
    return text


# --------------------------------------------------------------------------- #
# Atwater cross-check
# --------------------------------------------------------------------------- #


def atwater_warning(
    calories: float, protein: float, carbs: float, fat: float
) -> str | None:
    """Flag calories that disagree with 4/4/9 by more than max(50, 20%).

    Returns a warning string, or ``None`` when the numbers are consistent. This
    never blocks a write — alcohol and fiber legitimately break the identity.
    """
    computed = 4.0 * protein + 4.0 * carbs + 9.0 * fat
    delta = abs(computed - calories)
    tolerance = max(50.0, 0.20 * calories)
    if delta <= tolerance:
        return None
    return (
        f"Atwater check: {protein:g}g protein + {carbs:g}g carbs + {fat:g}g fat "
        f"works out to about {computed:g} kcal, but {calories:g} kcal was logged "
        f"(off by {delta:g}, tolerance {tolerance:g}). The entry was saved — "
        "double-check the label in case a number was transcribed wrong."
    )


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def _num(value: Any) -> float:
    """Notion number properties can be null. Never let a None reach arithmetic."""
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number


def zero_totals() -> dict[str, float]:
    return {key: 0.0 for key in MACRO_KEYS}


def sum_macros(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Total the four macros across meal dicts, tolerating missing/None values."""
    totals = zero_totals()
    for row in rows:
        for key in MACRO_KEYS:
            totals[key] += _num(row.get(key))
    return {key: round(value, 2) for key, value in totals.items()}


def remaining(
    totals: Mapping[str, Any], targets: Mapping[str, Any]
) -> dict[str, float]:
    """Target minus consumed. May be negative — overage is shown, never clamped."""
    return {
        key: round(_num(targets.get(key)) - _num(totals.get(key)), 2)
        for key in MACRO_KEYS
    }


def average_totals(days: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Mean macros across a list of per-day total dicts."""
    days = list(days)
    if not days:
        return zero_totals()
    summed = sum_macros(days)
    return {key: round(value / len(days), 2) for key, value in summed.items()}
