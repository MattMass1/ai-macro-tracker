"""Pure business logic: the day boundary, validation, totals, and the Atwater check.

Nothing in this module touches the network. It is the single source of truth for
every rule the MCP tools and the REST API share — duplicating any of it in
TypeScript would let the two clients drift.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "America/New_York"))
DAY_ROLLOVER_HOUR = int(os.environ.get("DAY_ROLLOVER_HOUR", "4"))

MEALS = ("Breakfast", "Lunch", "Dinner", "Snack")

MACRO_KEYS = ("calories", "protein", "carbs", "fat", "fiber")

WORKOUT_TYPES = ("Push", "Pull", "Legs", "Abs", "Cardio", "Full Body")

MUSCLE_GROUPS = ("Quads", "Hams", "Push", "Pull", "Abs")

#: The user's 3-day split. Workout days cycle in this order.
WORKOUT_ROTATION = ("Push", "Pull", "Legs")

#: Version 1 of ``workout_plans.plan`` is a per-user JSON object:
#: ``{"version": 1, "rotation": ["Push", "Pull", "Legs"],
#: "days_per_week": 4, "days": {"Push": {"label": "Push Day",
#: "exercises": [{"name": "Bench Press", "sets": 3, "reps": "8-10",
#: "rest_sec": 150, "swaps": ["Incline DB Press"], "optional": False}]}},
#: "notes": ""}``. ``swaps`` and ``optional`` may be omitted. When swaps are
#: omitted, clients look them up by exercise name in ``workout_library``.
WORKOUT_PLAN_VERSION = 1

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

DEFAULT_TARGETS = {"calories": 2400.0, "protein": 215.0, "carbs": 200.0, "fat": 70.0, "fiber": 0.0}

DEFAULT_EXERCISES = {
    "Push": [
        "High-to-Low Cable Fly (cable/rope)",
        "Cable Decline Press (cable/rope)",
        "Dips (forward lean, bodyweight)",
        "Decline DB Bench Press",
    ],
    "Legs": [
        "Barbell Squat", "Hack Squats", "Leg Press", "Leg Curls",
        "Back Extension", "Smith Machine Squats",
    ],
    "Abs": [
        "Crunches", "Hanging Leg Raises", "Planks", "Cable Crunches",
        "Russian Twists", "Ab Wheel",
    ],
    "Cardio": ["Treadmill", "Bike", "Stairmaster", "Rowing Machine"],
}


def get_default_exercises_for_type(workout_type: str) -> list[str]:
    """Return a copy of Matt's legacy exercise list for one workout type."""
    return list(DEFAULT_EXERCISES.get(workout_type, ()))


class MacroError(ValueError):
    """A user-fixable problem. The message is relayed verbatim to the user by Poke."""


# --------------------------------------------------------------------------- #
# Day boundary
# --------------------------------------------------------------------------- #


def effective_date(now: datetime | None = None) -> date:
    """The logging day for a given instant. The day rolls at 4am local."""
    now = now or datetime.now(LOCAL_TZ)
    return (now.astimezone(LOCAL_TZ) - timedelta(hours=DAY_ROLLOVER_HOUR)).date()


def effective_day_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The [start, end) timestamp window of the current logging day.

    Anything that counts "today" against a quota must use this window, not the
    calendar date, or the 4am rollover hands out a fresh quota at midnight.
    """
    start = datetime.combine(
        effective_date(now), time(hour=DAY_ROLLOVER_HOUR), tzinfo=LOCAL_TZ
    )
    return start, start + timedelta(days=1)


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
    calories: Any, protein: Any, carbs: Any, fat: Any, fiber: Any = 0
) -> dict[str, float]:
    """Validate the four macro numbers, returning them rounded and coerced to float."""
    return {
        "calories": _validate_number(calories, "calories", MAX_CALORIES),
        "protein": _validate_number(protein, "protein", MAX_MACRO),
        "carbs": _validate_number(carbs, "carbs", MAX_MACRO),
        "fat": _validate_number(fat, "fat", MAX_MACRO),
        "fiber": _validate_number(fiber, "fiber", MAX_MACRO),
    }


def validate_name(name: str, field: str = "name") -> str:
    text = (name or "").strip()
    if not text:
        raise MacroError(f"{field} is required — describe the food in a few words")
    if len(text) > 200:
        raise MacroError(f"{field} must be 200 characters or fewer")
    return text


def validate_display_name(value: Any) -> str:
    """Validate the short name used by the coach for the current user."""
    if not isinstance(value, str):
        raise MacroError("name must be a string")
    name = value.strip()
    if not name:
        raise MacroError("name is required")
    if len(name) > 40:
        raise MacroError("name must be 40 characters or fewer")
    return name


def validate_metrics(values: Mapping[str, Any]) -> dict[str, Any]:
    """Validate onboarding measurements and return normalized values."""
    def bounded_number(field: str, minimum: float, maximum: float) -> float:
        try:
            number = float(values.get(field))
        except (TypeError, ValueError):
            raise MacroError(f"{field} must be a number") from None
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise MacroError(f"{field} must be between {minimum:g} and {maximum:g}")
        return round(number, 2)

    result: dict[str, Any] = {
        "height_cm": bounded_number("height_cm", 100, 250),
        "weight_kg": bounded_number("weight_kg", 30, 300),
        "goal_weight_kg": bounded_number("goal_weight_kg", 30, 300),
    }
    age = values.get("age")
    if age is not None:
        if isinstance(age, bool):
            raise MacroError("age must be a whole number between 13 and 120")
        try:
            clean_age = int(age)
        except (TypeError, ValueError):
            raise MacroError("age must be a whole number between 13 and 120") from None
        if clean_age != float(age) or not 13 <= clean_age <= 120:
            raise MacroError("age must be a whole number between 13 and 120")
        result["age"] = clean_age
    activity_level = values.get("activity_level")
    if activity_level is not None:
        if not isinstance(activity_level, str):
            raise MacroError("activity_level must be a string")
        clean_level = activity_level.strip()
        if not clean_level or len(clean_level) > 40:
            raise MacroError("activity_level must be 1 to 40 characters")
        result["activity_level"] = clean_level
    return result


def validate_workout_plan(plan: Any) -> dict[str, Any]:
    """Validate and return a version-1 per-user workout plan.

    Exercise membership in the shared library is deliberately checked by the
    caller, where database access is available. Unknown names are warnings, not
    validation errors, so a coach may introduce a legitimate future exercise.
    """
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    if plan.get("version") != WORKOUT_PLAN_VERSION:
        raise ValueError(f"plan.version must be {WORKOUT_PLAN_VERSION}")
    rotation = plan.get("rotation")
    if not isinstance(rotation, list) or not rotation:
        raise ValueError("plan.rotation must be a non-empty array")
    # Normalize workout types case-insensitively to the canonical set WHEN they match;
    # otherwise accept the coach's session label as-is (the app renders labels).
    type_lookup = {t.casefold(): t for t in WORKOUT_TYPES}
    for index, workout_type in enumerate(rotation):
        if not isinstance(workout_type, str) or not workout_type.strip():
            raise ValueError(f"plan.rotation[{index}] must be a non-empty string")
        canonical = type_lookup.get(workout_type.casefold())
        if canonical is not None:
            rotation[index] = canonical
    if len(set(rotation)) != len(rotation):
        raise ValueError("plan.rotation must not contain duplicate workout types")
    clean_plan: dict[str, Any] = {
        "version": WORKOUT_PLAN_VERSION,
        "rotation": list(rotation),
    }
    days_per_week = plan.get("days_per_week")
    if days_per_week is None:
        days_per_week = len(rotation)  # default: one day per rotation entry
    if isinstance(days_per_week, bool) or not isinstance(days_per_week, int):
        raise ValueError("plan.days_per_week must be an integer")
    if not 1 <= days_per_week <= 7:
        raise ValueError("plan.days_per_week must be between 1 and 7")
    clean_plan["days_per_week"] = days_per_week
    days = plan.get("days")
    if not isinstance(days, dict):
        raise ValueError("plan.days must be an object keyed by workout type")
    missing_days = [workout_type for workout_type in rotation if workout_type not in days]
    if missing_days:
        raise ValueError(
            "plan.days is missing rotation workout types: " + ", ".join(missing_days)
        )
    clean_days: dict[str, Any] = {}
    for day_name, day in days.items():
        prefix = f"plan.days[{day_name!r}]"
        if not isinstance(day_name, str) or not day_name.strip():
            raise ValueError(f"{prefix} uses an invalid workout type")
        canonical_day = type_lookup.get(str(day_name).casefold())
        if canonical_day is not None:
            day_name = canonical_day
        if not isinstance(day, dict):
            raise ValueError(f"{prefix} must be an object")
        if not isinstance(day.get("label"), str) or not day["label"].strip():
            raise ValueError(f"{prefix}.label must be a non-empty string")
        exercises = day.get("exercises")
        if not isinstance(exercises, list):
            raise ValueError(f"{prefix}.exercises must be an array")
        if len(exercises) > 30:
            raise ValueError(f"{prefix}.exercises must contain at most 30 exercises")
        if len(exercises) > 8:
            raise ValueError(f"{prefix}.exercises must contain at most 8 exercises (keep sessions focused — the user can add more from the library)")
        # Honor the user's chosen session size range (e.g. "3-4", "5-7", "7-8")
        session_size = plan.get("session_size")
        if isinstance(session_size, str) and "-" in session_size:
            try:
                upper = int(session_size.split("-")[-1].strip())
                if upper >= 1 and len(exercises) > upper:
                    raise ValueError(
                        f"{prefix}.exercises exceeds the user's chosen session size ({session_size}) — keep it to {upper} exercises"
                    )
            except ValueError:
                pass  # malformed session_size — ignore, rely on the 8 cap
        clean_exercises: list[dict[str, Any]] = []
        for index, exercise in enumerate(exercises):
            exercise_prefix = f"{prefix}.exercises[{index}]"
            if not isinstance(exercise, dict):
                raise ValueError(f"{exercise_prefix} must be an object")
            if not isinstance(exercise.get("name"), str) or not exercise["name"].strip():
                raise ValueError(f"{exercise_prefix}.name must be a non-empty string")
            sets = exercise.get("sets")
            if sets is None:
                sets = 3  # default: 3 sets
            if (
                isinstance(sets, bool)
                or not isinstance(sets, int)
                or not 1 <= sets <= 10
            ):
                raise ValueError(f"{exercise_prefix}.sets must be an integer between 1 and 10")
            reps = exercise.get("reps")
            if not isinstance(reps, str) or not reps.strip():
                if isinstance(reps, (int, float)):
                    reps = str(int(reps))  # coerce 12 -> "12"
                else:
                    raise ValueError(f"{exercise_prefix}.reps must be a non-empty string")
            rest_sec = exercise.get("rest_sec")
            if (
                isinstance(rest_sec, bool)
                or not isinstance(rest_sec, int)
                or not 0 <= rest_sec <= 3600
            ):
                raise ValueError(
                    f"{exercise_prefix}.rest_sec must be an integer between 0 and 3600"
                )
            swaps = exercise.get("swaps")
            if swaps is not None and (
                not isinstance(swaps, list)
                or any(not isinstance(swap, str) or not swap.strip() for swap in swaps)
            ):
                raise ValueError(f"{exercise_prefix}.swaps must be an array of non-empty strings")
            optional = exercise.get("optional")
            if optional is not None and not isinstance(optional, bool):
                raise ValueError(f"{exercise_prefix}.optional must be a boolean")
            clean_exercise = {
                "name": exercise["name"].strip(),
                "sets": sets,
                "reps": reps.strip(),
                "rest_sec": rest_sec,
            }
            if swaps is not None:
                clean_exercise["swaps"] = [swap.strip() for swap in swaps]
            if optional is not None:
                clean_exercise["optional"] = optional
            clean_exercises.append(clean_exercise)
        clean_days[day_name] = {
            "label": day["label"].strip(),
            "exercises": clean_exercises,
        }
    clean_plan["days"] = clean_days
    notes = plan.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("plan.notes must be a string")
    if notes is not None:
        clean_plan["notes"] = notes
    session_size = plan.get("session_size")
    if session_size is not None and not isinstance(session_size, str):
        raise ValueError("plan.session_size must be a string")
    if session_size is not None:
        clean_plan["session_size"] = session_size
    return clean_plan


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
    """Aggregate the current ISO week and its completed qualifying-week streak.

    Weeks run Monday through Sunday. A qualifying week has workouts logged on
    at least three distinct days. The current week is always in progress and
    is therefore excluded from the streak until the following Monday.
    """
    rows = list(rows)
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    dated_rows: list[tuple[date, Mapping[str, Any]]] = []
    for row in rows:
        raw_date = str(row.get("date", ""))[:10]
        if not raw_date:
            continue
        try:
            dated_rows.append((date.fromisoformat(raw_date), row))
        except ValueError:
            continue

    current_rows = [
        row for row_date, row in dated_rows if week_start <= row_date <= week_end
    ]
    logged_dates = {
        row_date.isoformat()
        for row_date, _row in dated_rows
        if week_start <= row_date <= week_end
    }
    total_sets = sum(len(row.get("sets") or []) for row in current_rows)
    total_volume = sum(
        _num(item.get("weight")) * _num(item.get("reps"))
        for row in current_rows
        for item in (row.get("sets") or [])
    )

    weekly_days: dict[date, set[date]] = {}
    for row_date, _row in dated_rows:
        row_week = row_date - timedelta(days=row_date.weekday())
        weekly_days.setdefault(row_week, set()).add(row_date)
    streak = 0
    # The current Monday-Sunday window has not fully elapsed yet, including on
    # Sunday. Only begin with the immediately preceding completed week.
    cursor = week_start - timedelta(days=7)
    while len(weekly_days.get(cursor, set())) >= 3:
        streak += 1
        cursor -= timedelta(days=7)

    muscle_days = {group: set() for group in MUSCLE_GROUPS}
    type_days = {workout_type: set() for workout_type in WORKOUT_TYPES}
    for row_date, row in dated_rows:
        if not week_start <= row_date <= week_end:
            continue
        day = row_date.isoformat()
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
            "week_start": week_start.isoformat(),
            "week_label": f"Week of {week_start.strftime('%b')} {week_start.day}",
            "days_logged": len(logged_dates),
            "total_sets": total_sets,
            "total_volume": round(total_volume, 2),
            "streak_weeks": streak,
        },
        "coverage": {
            "muscle_groups": muscle_counts,
            "workout_types": {key: len(value) for key, value in type_days.items()},
            "untouched": [key for key, value in muscle_counts.items() if value == 0],
        },
    }
