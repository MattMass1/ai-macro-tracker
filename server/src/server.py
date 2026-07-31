"""FastMCP service: `/mcp` for Poke and `/api/*` for the PWA, from one process.

Both front doors call the same helpers below, so the day-boundary rule, the
macro totals, and the validation exist exactly once.
"""

from __future__ import annotations

import functools
import hmac
import sys
from datetime import date as _date
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402

from fastmcp import FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

import domain  # noqa: E402
import notion as notion_api  # noqa: E402
from config import get_config  # noqa: E402
from domain import MacroError  # noqa: E402
from notion import NotionClient, NotionError  # noqa: E402

CONFIG = get_config()

mcp = FastMCP("macro-tracker")

_client: NotionClient | None = None


def notion_client() -> NotionClient:
    global _client
    if _client is None:
        _client = NotionClient(CONFIG.notion_token)
    return _client


# --------------------------------------------------------------------------- #
# Shared business helpers — used by both the MCP tools and the REST routes
# --------------------------------------------------------------------------- #


async def fetch_meals(day: _date) -> list[dict[str, Any]]:
    """Every `Nutrition Entries` row for one logging day, newest first."""
    pages = await notion_client().query_data_source(
        CONFIG.nutrition_ds_id,
        filter=notion_api.date_equals_filter(day),
        sorts=[{"timestamp": "created_time", "direction": "descending"}],
    )
    return [notion_api.meal_from_page(page) for page in pages]


async def fetch_meals_in_range(start: _date, end: _date) -> list[dict[str, Any]]:
    pages = await notion_client().query_data_source(
        CONFIG.nutrition_ds_id,
        filter=notion_api.date_range_filter(start, end),
        sorts=[{"property": notion_api.P_DATE, "direction": "ascending"}],
    )
    return [notion_api.meal_from_page(page) for page in pages]


async def fetch_targets(day: _date) -> dict[str, float]:
    """Targets from the latest `Effective Date` that is on or before `day`."""
    pages = await notion_client().query_data_source(
        CONFIG.targets_ds_id,
        filter={
            "property": notion_api.P_EFFECTIVE_DATE,
            "date": {"on_or_before": day.isoformat()},
        },
        sorts=[{"property": notion_api.P_EFFECTIVE_DATE, "direction": "descending"}],
    )
    if not pages:
        return dict(domain.DEFAULT_TARGETS)
    row = notion_api.target_from_page(pages[0])
    return {key: row[key] for key in domain.MACRO_KEYS}


async def fetch_presets() -> list[dict[str, Any]]:
    """Active presets ordered by `Sort Order`."""
    pages = await notion_client().query_data_source(
        CONFIG.presets_ds_id,
        filter={"property": notion_api.P_ACTIVE, "checkbox": {"equals": True}},
        sorts=[{"property": notion_api.P_SORT_ORDER, "direction": "ascending"}],
    )
    presets = [notion_api.preset_from_page(page) for page in pages]
    presets.sort(key=lambda preset: (preset["sort_order"], preset["name"].lower()))
    return presets


async def fetch_workouts(day: _date) -> list[dict[str, Any]]:
    """Every `Fitness Tracker` row for one day, newest first."""
    pages = await notion_client().query_data_source(
        CONFIG.fitness_ds_id,
        filter={"property": notion_api.P_DATE_INPUT, "date": {"equals": day.isoformat()}},
        sorts=[{"timestamp": "created_time", "direction": "descending"}],
    )
    return [notion_api.workout_from_page(page) for page in pages]


async def fetch_known_exercises() -> list[dict[str, Any]]:
    """Every exercise with its workout type, from the PR log (Max Reps).

    The PWA uses this to offer an exercise picker with the correct
    Push/Pull/Legs tag instead of a free-text field.
    """
    pages = await notion_client().query_data_source(
        CONFIG.maxreps_ds_id,
        sorts=[{"property": notion_api.P_EXERCISE, "direction": "ascending"}],
    )
    return [
        {
            "name": notion_api.read_title(page, notion_api.P_EXERCISE).strip(),
            "workout_type": notion_api.read_multi_select(page, notion_api.P_WORKOUT_TYPE),
        }
        for page in pages
        if notion_api.read_title(page, notion_api.P_EXERCISE).strip()
    ]


async def last_workout_type() -> str | None:
    """The workout type of the most recent Fitness Tracker entry.

    Uses `Date (user input)` as the primary sort; rows without a date fall
    back to `created_time`. Old rows only carry Muscle Group, so the type is
    derived via the shared mapping when the Workout type tag is empty.
    """
    pages = await notion_client().query_data_source(CONFIG.fitness_ds_id)

    def sort_key(page: Mapping[str, Any]) -> str:
        return (
            notion_api.read_date(page, notion_api.P_DATE_INPUT)
            or (page.get("created_time") or "")[:10]
            or ""
        )

    if not pages:
        return None
    pages.sort(key=sort_key, reverse=True)
    newest = pages[0]
    types = notion_api.read_multi_select(newest, notion_api.P_WORKOUT_TYPE)
    if types:
        return next(
            (t for t in domain.WORKOUT_ROTATION if t in types),
            types[0],
        )
    muscles = notion_api.read_multi_select(newest, notion_api.P_MUSCLE_GROUP)
    return domain.workout_type_from_muscle(muscles)


async def workout_plan_payload() -> dict[str, Any]:
    """The upcoming Push → Pull → Legs rotation with exercises per day.

    The plan derives from the most recent logged workout; Abs/Cardio days do
    not advance the split. Exercises come from the PR log (Max Reps) tagged
    with the planned type, plus an Abs/core list that can be done any day.
    `upcoming` lists the next `window` rotation days in order so the user can
    read ahead and never look up exercise names.
    """
    last_type = await last_workout_type()
    known = await fetch_known_exercises()

    def exercises_for(workout_type: str) -> list[dict[str, Any]]:
        return [
            {"name": ex["name"]}
            for ex in known
            if workout_type in ex["workout_type"]
        ]

    window = 5  # a 5-day training week at most cycles the split twice
    upcoming: list[dict[str, Any]] = []
    day_type = domain.next_workout_type(last_type)
    for _ in range(window):
        upcoming.append(
            {
                "type": day_type,
                "exercises": exercises_for(day_type),
            }
        )
        day_type = domain.next_workout_type(day_type)

    return {
        "rotation": list(domain.WORKOUT_ROTATION),
        "last_workout": last_type,
        "upcoming": upcoming,
        "core": [
            {"name": ex["name"]}
            for ex in known
            if "Abs" in ex["workout_type"]
        ],
    }


async def write_workout(
    exercise: str,
    sets: Any,
    workout_type: str | None,
    day_value: str | None,
) -> dict[str, Any]:
    """Validate, write one Fitness Tracker row, and return it."""
    clean_exercise = domain.validate_name(exercise, "exercise")
    cleaned_sets = domain.validate_sets(sets)
    type_name = domain.normalize_workout_type(workout_type)
    muscle_group = domain.normalize_muscle_group([type_name])
    day = domain.resolve_date(day_value, "date")

    page = await notion_client().create_page(
        CONFIG.fitness_ds_id,
        notion_api.workout_properties(
            clean_exercise,
            type_name,
            muscle_group,
            cleaned_sets,
            day,
        ),
    )
    return {
        "id": page.get("id", ""),
        "exercise": clean_exercise,
        "workout_type": [type_name],
        "muscle_group": muscle_group,
        "sets": cleaned_sets,
        "date": day.isoformat(),
        "created_time": page.get("created_time", ""),
    }


async def day_payload(
    day: _date,
    include_presets: bool = False,
    ensure: Mapping[str, Any] | None = None,
    exclude: str | None = None,
) -> dict[str, Any]:
    """The canonical day view every client renders.

    `ensure` and `exclude` reconcile the response with a write that just
    happened. Notion's query index is eventually consistent: a row created
    milliseconds ago is often missing from the next query, and an archived row
    often still shows up. Without this, logging a meal would return the totals
    from *before* it — Poke would then report the wrong number over text.
    """
    meals = await fetch_meals(day)
    if exclude:
        meals = [meal for meal in meals if meal["id"] != exclude]
    if ensure and not any(meal["id"] == ensure["id"] for meal in meals):
        meals = [dict(ensure), *meals]
    targets = await fetch_targets(day)
    totals = domain.sum_macros(meals)
    payload: dict[str, Any] = {
        "date": day.isoformat(),
        "day_label": domain.day_label(day),
        "totals": totals,
        "targets": targets,
        "remaining": domain.remaining(totals, targets),
        "meals": meals,
    }
    if include_presets:
        payload["presets"] = [
            {
                "name": preset["name"],
                "emoji": preset["emoji"],
                "calories": preset["calories"],
                "protein": preset["protein"],
                "carbs": preset["carbs"],
                "fat": preset["fat"],
                "meal": preset["meal"],
                "sort_order": preset["sort_order"],
            }
            for preset in await fetch_presets()
        ]
    return payload


async def write_meal(
    name: str,
    calories: Any,
    protein: Any,
    carbs: Any,
    fat: Any,
    macro_source: str,
    meal: str | None,
    day_value: str | None,
) -> dict[str, Any]:
    """Validate, write one row, and return the day's corrected numbers."""
    clean_name = domain.validate_name(name)
    macros = domain.validate_macros(calories, protein, carbs, fat)
    source = domain.validate_macro_source(macro_source)
    meal_name = domain.normalize_meal(meal)
    day = domain.resolve_date(day_value)

    page = await notion_client().create_page(
        CONFIG.nutrition_ds_id,
        notion_api.meal_properties(
            clean_name,
            meal_name,
            macros["calories"],
            macros["protein"],
            macros["carbs"],
            macros["fat"],
            day,
        ),
        children=[notion_api.paragraph_block(f"Macro source: {source}")],
    )

    logged = {
        "id": page.get("id", ""),
        "name": clean_name,
        "meal": meal_name,
        **macros,
        "date": day.isoformat(),
        "created_time": page.get("created_time", ""),
    }
    payload = await day_payload(day, ensure=logged)
    payload["logged"] = {**logged, "macro_source": source}
    warning = domain.atwater_warning(
        macros["calories"], macros["protein"], macros["carbs"], macros["fat"]
    )
    if warning:
        payload["warning"] = warning
    return payload


async def find_preset(preset_name: str) -> dict[str, Any]:
    presets = await fetch_presets()
    wanted = (preset_name or "").strip().lower()
    if not wanted:
        raise MacroError("preset_name is required")
    for preset in presets:
        if preset["name"].strip().lower() == wanted:
            return preset
    available = ", ".join(preset["name"] for preset in presets) or "(none saved yet)"
    raise MacroError(
        f"No active preset named {preset_name!r}. Available presets: {available}."
    )


async def log_preset_servings(
    preset_name: str,
    servings: Any = 1.0,
    meal: str | None = None,
    day_value: str | None = None,
) -> dict[str, Any]:
    preset = await find_preset(preset_name)
    try:
        count = float(servings)
    except (TypeError, ValueError):
        raise MacroError(f"servings must be a number — got {servings!r}") from None
    if count <= 0 or count > 20:
        raise MacroError(f"servings must be greater than 0 and at most 20 — got {count:g}")

    label = preset["name"] if count == 1 else f"{preset['name']} x{count:g}"
    return await write_meal(
        name=label,
        calories=preset["calories"] * count,
        protein=preset["protein"] * count,
        carbs=preset["carbs"] * count,
        fat=preset["fat"] * count,
        macro_source=f"Meal Preset: {preset['name']}",
        meal=meal or preset["meal"],
        day_value=day_value,
    )


# --------------------------------------------------------------------------- #
# MCP tools
# --------------------------------------------------------------------------- #


def tool_errors(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Surface domain and Notion messages to Poke instead of a masked error."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except MacroError as exc:
            raise ToolError(str(exc)) from exc
        except NotionError as exc:
            raise ToolError(f"Notion rejected the request. {exc}") from exc

    return wrapper


@mcp.tool
@tool_errors
async def log_meal(
    name: str,
    calories: float,
    protein: float,
    carbs: float,
    fat: float,
    macro_source: str,
    meal: str = "Snack",
    date: str | None = None,
) -> dict[str, Any]:
    """Log one food or meal to the Notion nutrition log and return the day's totals.

    NEVER estimate or guess macros. Before calling this, look up real numbers:
    the FDA FoodData Central entry for whole foods, or the specific brand's
    nutrition label for packaged products. Do not guess portion sizes or weights
    either — if the amount is unclear, ask the user how much they ate.

    Args:
        name: What was eaten, e.g. "6oz roasted chicken breast".
        calories: Calories for the portion actually eaten.
        protein: Grams of protein.
        carbs: Grams of carbohydrate.
        fat: Grams of fat.
        macro_source: Where these numbers came from, e.g.
            "FDA FoodData Central: chicken breast, roasted", "Fairlife Core Power
            label", "user read the package". Placeholders like "estimate",
            "approx" or "guess" are rejected.
        meal: Breakfast, Lunch, Dinner, or Snack. Defaults to Snack.
        date: YYYY-MM-DD. Defaults to the current logging day, which rolls over
            at 4am Eastern — a 2am snack counts toward the previous day.

    Returns totals, targets, remaining and the day's meals. A `warning` field
    appears when the calories disagree with 4/4/9 math, which usually means a
    number was transcribed wrong.
    """
    return await write_meal(
        name, calories, protein, carbs, fat, macro_source, meal, date
    )


@mcp.tool
@tool_errors
async def log_preset(
    preset_name: str,
    servings: float = 1.0,
    meal: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Log a saved meal preset by name, multiplied by the number of servings.

    Use this when the user names a meal they already have saved ("log my usual
    dinner", "two protein shakes"). Call `list_presets` first if unsure what
    exists. Macros come from the saved preset, so nothing is estimated.

    Args:
        preset_name: Preset name, matched case-insensitively among active presets.
        servings: How many servings, e.g. 2 for a double portion. Defaults to 1.
        meal: Override the preset's meal slot (Breakfast/Lunch/Dinner/Snack).
        date: YYYY-MM-DD. Defaults to the current logging day (rolls at 4am Eastern).
    """
    return await log_preset_servings(preset_name, servings, meal, date)


@mcp.tool
@tool_errors
async def list_presets() -> dict[str, Any]:
    """List every active saved meal preset with its per-serving macros.

    Use this to see what the user can quick-log by name before calling
    `log_preset`, or to answer "what meals do I have saved?".
    """
    presets = await fetch_presets()
    return {
        "count": len(presets),
        "presets": [
            {
                "name": preset["name"],
                "emoji": preset["emoji"],
                "meal": preset["meal"],
                "per_serving": {
                    "calories": preset["calories"],
                    "protein": preset["protein"],
                    "carbs": preset["carbs"],
                    "fat": preset["fat"],
                },
                "sort_order": preset["sort_order"],
            }
            for preset in presets
        ],
    }


@mcp.tool
@tool_errors
async def save_preset(
    name: str,
    calories: float,
    protein: float,
    carbs: float,
    fat: float,
    meal: str = "Dinner",
    emoji: str = "🍽️",
) -> dict[str, Any]:
    """Create or update a reusable meal preset so it can be logged in one tap.

    Use when the user says something like "save that as my usual dinner". Macros
    must be real looked-up values per one serving — never estimates. If a preset
    with this name already exists it is updated in place.

    Args:
        name: Preset name the user will say, e.g. "Usual dinner".
        calories: Calories for one serving.
        protein: Grams of protein per serving.
        carbs: Grams of carbohydrate per serving.
        fat: Grams of fat per serving.
        meal: Default meal slot — Breakfast, Lunch, Dinner, or Snack.
        emoji: Single emoji shown on the app's quick-add tile.
    """
    clean_name = domain.validate_name(name, "name")
    macros = domain.validate_macros(calories, protein, carbs, fat)
    meal_name = domain.normalize_meal(meal, default="Dinner")
    glyph = (emoji or "🍽️").strip()[:4] or "🍽️"

    client = notion_client()
    existing_pages = await client.query_data_source(CONFIG.presets_ds_id)
    existing = next(
        (
            page
            for page in existing_pages
            if notion_api.read_title(page).strip().lower() == clean_name.lower()
        ),
        None,
    )

    properties = {
        notion_api.P_NAME: notion_api.title_prop(clean_name),
        notion_api.P_EMOJI: notion_api.rich_text_prop(glyph),
        notion_api.P_CALORIES: notion_api.number_prop(macros["calories"]),
        notion_api.P_PROTEIN: notion_api.number_prop(macros["protein"]),
        notion_api.P_CARBS: notion_api.number_prop(macros["carbs"]),
        notion_api.P_FAT: notion_api.number_prop(macros["fat"]),
        notion_api.P_MEAL: notion_api.select_prop(meal_name),
        notion_api.P_ACTIVE: notion_api.checkbox_prop(True),
    }

    if existing is None:
        order = 0.0
        for page in existing_pages:
            order = max(order, notion_api.read_number(page, notion_api.P_SORT_ORDER))
        properties[notion_api.P_SORT_ORDER] = notion_api.number_prop(order + 1)
        page = await client.create_page(CONFIG.presets_ds_id, properties)
        action = "created"
    else:
        page = await client.update_page(existing["id"], properties)
        action = "updated"

    return {
        "action": action,
        "preset": {
            "id": page.get("id", ""),
            "name": clean_name,
            "emoji": glyph,
            "meal": meal_name,
            **macros,
        },
    }


@mcp.tool
@tool_errors
async def get_today() -> dict[str, Any]:
    """Today's macro totals, targets, remaining macros, and every meal logged.

    "Today" is the current logging day, which rolls over at 4am Eastern rather
    than midnight. The response includes the resolved date and a `day_label` —
    state which day the numbers are for when reporting them.
    """
    return await day_payload(domain.effective_date(), include_presets=True)


@mcp.tool
@tool_errors
async def get_day(date: str) -> dict[str, Any]:
    """Macro totals, targets, remaining, and meals for one specific day.

    Args:
        date: The day to look up, formatted YYYY-MM-DD.
    """
    return await day_payload(domain.parse_date(date))


@mcp.tool
@tool_errors
async def get_range_summary(start: str, end: str) -> dict[str, Any]:
    """Per-day totals and averages across a date range, inclusive of both ends.

    Use for questions like "how did last week go?" or "what's my average protein
    this month?".

    Args:
        start: First day of the range, YYYY-MM-DD.
        end: Last day of the range, YYYY-MM-DD.
    """
    start_day = domain.parse_date(start, "start")
    end_day = domain.parse_date(end, "end")
    if end_day < start_day:
        raise MacroError(
            f"end ({end}) is before start ({start}) — pass the earlier date first"
        )

    meals = await fetch_meals_in_range(start_day, end_day)
    by_day: dict[str, list[dict[str, Any]]] = {}
    for meal in meals:
        by_day.setdefault(meal["date"], []).append(meal)

    days: list[dict[str, Any]] = []
    cursor = start_day
    while cursor <= end_day:
        iso = cursor.isoformat()
        day_meals = by_day.get(iso, [])
        totals = domain.sum_macros(day_meals)
        targets = await fetch_targets(cursor)
        days.append(
            {
                "date": iso,
                "day_label": domain.day_label(cursor),
                "meal_count": len(day_meals),
                "totals": totals,
                "targets": targets,
                "remaining": domain.remaining(totals, targets),
            }
        )
        cursor = cursor.fromordinal(cursor.toordinal() + 1)

    logged_days = [day for day in days if day["meal_count"] > 0]
    return {
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "days": days,
        "days_with_entries": len(logged_days),
        "average_all_days": domain.average_totals([day["totals"] for day in days]),
        "average_logged_days": domain.average_totals(
            [day["totals"] for day in logged_days]
        ),
    }


@mcp.tool
@tool_errors
async def undo_last_meal() -> dict[str, Any]:
    """Remove the most recently logged entry for the current logging day.

    Use when the user says "undo that", "scratch that", or "I logged that twice".
    Returns what was removed along with the corrected totals for the day.
    """
    day = domain.effective_date()
    meals = await fetch_meals(day)
    if not meals:
        raise MacroError(
            f"Nothing has been logged for {domain.day_label(day)} yet, so there is "
            "nothing to undo."
        )
    newest = meals[0]
    await notion_client().archive_page(newest["id"])
    payload = await day_payload(day, exclude=newest["id"])
    payload["removed"] = newest
    return payload


@mcp.tool
@tool_errors
async def get_targets(date: str | None = None) -> dict[str, Any]:
    """The macro targets in effect for a given day.

    Targets are dated rows; the one with the latest effective date on or before
    the requested day wins, so past days keep the targets they were judged against.

    Args:
        date: YYYY-MM-DD. Defaults to the current logging day.
    """
    day = domain.resolve_date(date)
    targets = await fetch_targets(day)
    return {
        "date": day.isoformat(),
        "day_label": domain.day_label(day),
        "targets": targets,
    }


@mcp.tool
@tool_errors
async def set_targets(
    calories: float,
    protein: float,
    carbs: float,
    fat: float,
    effective_date: str | None = None,
) -> dict[str, Any]:
    """Set new daily macro targets from a given date onward.

    Writes a new dated row rather than editing the old one, so target history
    survives a switch between cut and bulk phases.

    Args:
        calories: Daily calorie target.
        protein: Daily protein target in grams.
        carbs: Daily carbohydrate target in grams.
        fat: Daily fat target in grams.
        effective_date: YYYY-MM-DD the new targets start on. Defaults to the
            current logging day.
    """
    macros = domain.validate_macros(calories, protein, carbs, fat)
    day = domain.resolve_date(effective_date, "effective_date")
    page = await notion_client().create_page(
        CONFIG.targets_ds_id,
        {
            notion_api.P_NAME: notion_api.title_prop(f"Targets from {day.isoformat()}"),
            notion_api.P_EFFECTIVE_DATE: notion_api.date_prop(day),
            notion_api.P_CALORIES: notion_api.number_prop(macros["calories"]),
            notion_api.P_PROTEIN: notion_api.number_prop(macros["protein"]),
            notion_api.P_CARBS: notion_api.number_prop(macros["carbs"]),
            notion_api.P_FAT: notion_api.number_prop(macros["fat"]),
        },
    )
    return {
        "id": page.get("id", ""),
        "effective_date": day.isoformat(),
        "targets": macros,
    }


@mcp.tool
@tool_errors
async def log_workout(
    exercise: str,
    sets: list[dict[str, float]],
    workout_type: str,
    date: str | None = None,
) -> dict[str, Any]:
    """Log one exercise with its sets to the Fitness Tracker.

    Use when the user reports a workout ("3 sets of bench at 225"), one
    exercise at a time. Each entry is a row in the Fitness Tracker database;
    the nightly sweep turns new maxes into PR rows automatically.

    Args:
        exercise: Exercise name, e.g. "Barbell Bench Press".
        sets: Up to 4 sets as [{"weight": 225, "reps": 5}, ...]. Weight in
            pounds (or kg if that is what the user tracks); reps is a count.
            Bodyweight exercises can pass weight: 0.
        workout_type: One of Push, Pull, Legs, Abs, Cardio, Full Body.
        date: YYYY-MM-DD the workout was done. Defaults to the current
            logging day (rolls at 4am Eastern).

    Returns the created entry with its id, exercise, sets, and type.
    """
    return await write_workout(exercise, sets, workout_type, date)


# --------------------------------------------------------------------------- #
# REST API for the PWA
# --------------------------------------------------------------------------- #

CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-App-Token",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
}


def _allowed_origin(request: Request) -> str:
    origin = request.headers.get("origin", "")
    if not CONFIG.allowed_origins or "*" in CONFIG.allowed_origins:
        return origin or "*"
    return origin if origin in CONFIG.allowed_origins else ""


def _with_cors(response: Response, request: Request) -> Response:
    origin = _allowed_origin(request)
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
    for key, value in CORS_HEADERS.items():
        response.headers[key] = value
    return response


def _authorized(request: Request) -> bool:
    supplied = request.headers.get("x-app-token", "")
    return bool(supplied) and hmac.compare_digest(supplied, CONFIG.app_shared_token)


def api_route(path: str, methods: list[str]):
    """Register an authenticated, CORS-enabled JSON route on the same app as /mcp."""

    def decorator(fn: Callable[[Request], Awaitable[Any]]):
        @mcp.custom_route(path, methods=[*methods, "OPTIONS"], name=fn.__name__)
        @functools.wraps(fn)
        async def handler(request: Request) -> Response:
            if request.method == "OPTIONS":
                return _with_cors(Response(status_code=204), request)
            if not _authorized(request):
                return _with_cors(
                    JSONResponse(
                        {"error": "Missing or invalid X-App-Token header"},
                        status_code=401,
                    ),
                    request,
                )
            try:
                payload = await fn(request)
            except MacroError as exc:
                return _with_cors(
                    JSONResponse({"error": str(exc)}, status_code=400), request
                )
            except NotionError as exc:
                return _with_cors(
                    JSONResponse({"error": str(exc)}, status_code=502), request
                )
            status = 200
            if isinstance(payload, tuple):
                payload, status = payload
            return _with_cors(JSONResponse(payload, status_code=status), request)

        return handler

    return decorator


async def _json_body(request: Request) -> Mapping[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise MacroError("Request body must be JSON") from None
    if not isinstance(body, dict):
        raise MacroError("Request body must be a JSON object")
    return body


@api_route("/api/today", methods=["GET"])
async def api_today(request: Request) -> Any:
    return await day_payload(domain.effective_date(), include_presets=True)


@api_route("/api/day/{date}", methods=["GET"])
async def api_day(request: Request) -> Any:
    day = domain.parse_date(request.path_params["date"])
    return await day_payload(day, include_presets=True)


@api_route("/api/presets", methods=["GET"])
async def api_presets(request: Request) -> Any:
    return {"presets": await fetch_presets()}


@api_route("/api/log-preset", methods=["POST"])
async def api_log_preset(request: Request) -> Any:
    body = await _json_body(request)
    return await log_preset_servings(
        preset_name=str(body.get("preset_name", "")),
        servings=body.get("servings", 1.0),
        meal=body.get("meal"),
        day_value=body.get("date"),
    )


@api_route("/api/log", methods=["POST"])
async def api_log(request: Request) -> Any:
    body = await _json_body(request)
    return await write_meal(
        name=str(body.get("name", "")),
        calories=body.get("calories"),
        protein=body.get("protein"),
        carbs=body.get("carbs"),
        fat=body.get("fat"),
        macro_source=str(body.get("macro_source", "")),
        meal=body.get("meal"),
        day_value=body.get("date"),
    )


@api_route("/api/meal/{page_id}", methods=["DELETE"])
async def api_delete_meal(request: Request) -> Any:
    page_id = request.path_params["page_id"].strip()
    if not page_id:
        raise MacroError("A meal id is required to delete an entry")
    await notion_client().archive_page(page_id)
    payload = await day_payload(
        domain.effective_date(), include_presets=True, exclude=page_id
    )
    payload["deleted"] = page_id
    return payload


@api_route("/api/exercises", methods=["GET"])
async def api_exercises(request: Request) -> Any:
    return {"exercises": await fetch_known_exercises()}


@api_route("/api/plan", methods=["GET"])
async def api_plan(request: Request) -> Any:
    return await workout_plan_payload()


@api_route("/api/workout", methods=["POST"])
async def api_log_workout(request: Request) -> Any:
    body = await _json_body(request)
    return await write_workout(
        exercise=str(body.get("exercise", "")),
        sets=body.get("sets"),
        workout_type=body.get("workout_type"),
        day_value=body.get("date"),
    )


@api_route("/api/workouts/{date}", methods=["GET"])
async def api_workouts(request: Request) -> Any:
    day = domain.parse_date(request.path_params["date"])
    return {
        "date": day.isoformat(),
        "day_label": domain.day_label(day),
        "workouts": await fetch_workouts(day),
    }


@api_route("/api/workout/{page_id}", methods=["DELETE"])
async def api_delete_workout(request: Request) -> Any:
    page_id = request.path_params["page_id"].strip()
    if not page_id:
        raise MacroError("A workout id is required to delete an entry")
    await notion_client().archive_page(page_id)
    return {"deleted": page_id}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Unauthenticated liveness probe for Render."""
    day = domain.effective_date()
    return JSONResponse(
        {
            "status": "ok",
            "service": "macro-tracker",
            "effective_date": day.isoformat(),
            "day_label": domain.day_label(day),
        }
    )


def main() -> None:
    mcp.run(transport="http", host="0.0.0.0", port=CONFIG.port)


if __name__ == "__main__":
    main()
