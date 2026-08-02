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

WORKOUT_TYPES = ("Push", "Pull", "Legs", "Abs", "Cardio", "Full Body")

MUSCLE_GROUPS = ("Quads", "Hams", "Push", "Pull", "Abs")

#: The user's 3-day split. Workout days cycle in this order.
WORKOUT_ROTATION = ("Push", "Pull", "Legs")

MAX_SETS = 4
MAX_WORKOUT_WEIGHT = 2000.0
MAX_REPS = 300

#: Muscle Group written for each Workout type when the app logs a workout.
#: Keeps the Fitness Tracker's two taxonomies consistent for the PR sweep.
WORKOUT_TYPE_TO_MUSCLE = {
    "Push": ["Push"],
    "Pull": ["Pull"],
    "Legs": ["Quads"],
    "Abs": ["Abs"],
    "Cardio": ["Cardio"],
    "Full Body": ["Full Body"],
}

#: `Muscle Group` values map onto the `Workout type` taxonomy. Fitness Tracker
#: rows populate Muscle Group; the Max Reps log carries Workout type.
MUSCLE_TO_WORKOUT_TYPE = {
    "Push": "Push",
    "Pull": "Pull",
    "Abs": "Abs",
    "Quads": "Legs",
    "Hams": "Legs",
    "Legs": "Legs",
    "Cardio": "Cardio",
    "Full Body": "Full Body",
}

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


# --------------------------------------------------------------------------- #
# Workout validation
# --------------------------------------------------------------------------- #


def normalize_workout_type(value: str | None) -> str:
    """Case-insensitively map a workout type onto the allowed taxonomy."""
    if value is None or not str(value).strip():
        raise MacroError(
            f"workout_type is required — one of {', '.join(WORKOUT_TYPES)}"
        )
    candidate = str(value).strip().lower()
    for allowed in WORKOUT_TYPES:
        if allowed.lower() == candidate:
            return allowed
    raise MacroError(
        f"workout_type must be one of {', '.join(WORKOUT_TYPES)} — got "
        f"{str(value).strip()!r}"
    )


def next_workout_type(last_type: str | None) -> str:
    """The next day in the Push → Pull → Legs rotation.

    `last_type` is the most recent logged workout's type (or None when there
    is no history). Anything outside the rotation (Abs, Cardio, unknown)
    falls back to Push, since those days do not advance the main split.
    """
    if last_type is None:
        return WORKOUT_ROTATION[0]
    candidate = str(last_type).strip().lower()
    for index, allowed in enumerate(WORKOUT_ROTATION):
        if allowed.lower() == candidate:
            return WORKOUT_ROTATION[(index + 1) % len(WORKOUT_ROTATION)]
    return WORKOUT_ROTATION[0]


def workout_type_from_muscle(muscle_groups: list[str]) -> str | None:
    """Best-effort Workout type for a row tagged only with Muscle Group."""
    for muscle in muscle_groups:
        mapped = MUSCLE_TO_WORKOUT_TYPE.get(muscle)
        if mapped:
            return mapped
    return None


def normalize_muscle_group(value: str | list[str] | None) -> list[str]:
    """Map a workout type onto its default Muscle Group tags."""
    types = value if isinstance(value, list) else ([value] if value else [])
    groups: list[str] = []
    for entry in types:
        if not str(entry).strip():
            continue
        candidate = str(entry).strip().lower()
        for allowed in WORKOUT_TYPES:
            if allowed.lower() == candidate and allowed in WORKOUT_TYPE_TO_MUSCLE:
                for group in WORKOUT_TYPE_TO_MUSCLE[allowed]:
                    if group not in groups:
                        groups.append(group)
    return groups


def validate_sets(sets: Any) -> list[dict[str, float]]:
    """Validate a list of up to MAX_SETS weight/reps pairs.

    Each set is a dict with `weight` and `reps` numbers. Empty weight or reps
    are allowed (bodyweight, burnout sets), but at least one populated set is
    required and every value must be finite and within a sane range.
    """
    if not isinstance(sets, list) or not sets:
        raise MacroError("sets must be a non-empty list of {weight, reps} objects")
    if len(sets) > MAX_SETS:
        raise MacroError(
            f"sets must have at most {MAX_SETS} sets — got {len(sets)}. "
            "Log a second exercise row for anything beyond that."
        )

    cleaned: list[dict[str, float]] = []
    for index, item in enumerate(sets, start=1):
        if not isinstance(item, dict):
            raise MacroError(f"set {index} must be an object with weight and reps")
        weight = item.get("weight")
        reps = item.get("reps")
        weight_f = _validate_number(weight, f"set {index} weight", MAX_WORKOUT_WEIGHT)
        reps_f = _validate_number(reps, f"set {index} reps", MAX_REPS)
        cleaned.append({"weight": weight_f, "reps": reps_f})

    if all(s["weight"] == 0 and s["reps"] == 0 for s in cleaned):
        raise MacroError("at least one set needs a weight or reps value")
    return cleaned


def workout_week_stats(
    rows: Iterable[Mapping[str, Any]], today: date
) -> dict[str, Any]:
    """Aggregate a rolling workout week without coupling business rules to Notion."""
    rows = list(rows)
    logged_dates = {str(row.get("date", ""))[:10] for row in rows if row.get("date")}
    total_sets = sum(len(row.get("sets") or []) for row in rows)
    total_volume = sum(
        _num(item.get("weight")) * _num(item.get("reps"))
        for row in rows
        for item in (row.get("sets") or [])
    )

    streak = 0
    cursor = today
    while cursor.isoformat() in logged_dates:
        streak += 1
        cursor -= timedelta(days=1)

    muscle_days = {group: set() for group in MUSCLE_GROUPS}
    type_days = {workout_type: set() for workout_type in WORKOUT_TYPES}
    for row in rows:
        day = str(row.get("date", ""))[:10]
        if not day:
            continue
        muscles = list(row.get("muscle_group") or [])
        types = list(row.get("workout_type") or [])
        derived_type = workout_type_from_muscle(muscles)
        if not types and derived_type:
            types = [derived_type]
        for muscle in muscles:
            if muscle in muscle_days:
                muscle_days[muscle].add(day)
        for workout_type in types:
            if workout_type in type_days:
                type_days[workout_type].add(day)

    muscle_counts = {key: len(value) for key, value in muscle_days.items()}
    return {
        "week": {
            "days_logged": len(logged_dates),
            "total_sets": total_sets,
            "total_volume": round(total_volume, 2),
            "streak_days": streak,
        },
        "coverage": {
            "muscle_groups": muscle_counts,
            "workout_types": {key: len(value) for key, value in type_days.items()},
            "untouched": [key for key, value in muscle_counts.items() if value == 0],
        },
    }
