"""FastMCP service: `/mcp` for Poke and `/api/*` for the PWA, from one process.

Both front doors call the same helpers below, so the day-boundary rule, the
macro totals, and the validation exist exactly once.
"""

from __future__ import annotations

import functools
import hmac
import json
import os
import sys
import tempfile
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402

from fastmcp import FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

import domain  # noqa: E402
from config import get_config  # noqa: E402
from domain import MacroError  # noqa: E402
from store import Store, StoreError  # noqa: E402

_client: Store | None = None


def store_client() -> Store:
    global _client
    if _client is None:
        _client = Store(CONFIG.database_url)
    return _client


@asynccontextmanager
async def lifespan(_app: Any):
    try:
        yield
    finally:
        if _client is not None:
            await _client.aclose()


CONFIG = get_config()
mcp = FastMCP("macro-tracker", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Shared business helpers — used by both the MCP tools and the REST routes
# --------------------------------------------------------------------------- #


async def fetch_meals(day: _date) -> list[dict[str, Any]]:
    """Every `Nutrition Entries` row for one logging day, newest first."""
    return await store_client().fetch_meals(day)


async def fetch_meals_in_range(start: _date, end: _date) -> list[dict[str, Any]]:
    return await store_client().fetch_meals(start, end)


async def fetch_targets(day: _date) -> dict[str, float]:
    """Targets from the latest `Effective Date` that is on or before `day`."""
    row = await store_client().fetch_targets(day)
    if not row:
        return dict(domain.DEFAULT_TARGETS)
    return {key: row[key] for key in domain.MACRO_KEYS}


async def fetch_presets() -> list[dict[str, Any]]:
    """Active presets ordered by `Sort Order`."""
    presets = await store_client().fetch_presets()
    presets.sort(key=lambda preset: (preset["sort_order"], preset["name"].lower()))
    return presets


async def fetch_workouts(day: _date) -> list[dict[str, Any]]:
    """Every `Fitness Tracker` row for one day, newest first."""
    return await store_client().fetch_workouts(day, day)


async def fetch_workouts_in_range(start: _date, end: _date) -> list[dict[str, Any]]:
    return await store_client().fetch_workouts(start, end)


async def fetch_last_workout(exercise: str) -> dict[str, Any] | None:
    """Most recent Fitness Tracker row matching an exercise exactly."""
    clean_exercise = domain.validate_name(exercise, "exercise")
    pages = await store_client().fetch_workouts(exercise=clean_exercise)
    if not pages:
        return None
    return pages[0]


async def fetch_prs() -> list[dict[str, Any]]:
    pages = await store_client().fetch_prs()
    prs = [{"exercise": p["exercise"], "max_weight": p["max_weight"], "date": p.get("date_achieved") or ""} for p in pages]
    return [pr for pr in prs if pr["exercise"]][:5]


async def fetch_known_exercises() -> list[dict[str, Any]]:
    """Every exercise with its workout type, from the PR log (Max Reps).

    The PWA uses this to offer an exercise picker with the correct
    Push/Pull/Legs tag instead of a free-text field.
    """
    pages = await store_client().fetch_prs()
    return [
        {
            "name": page["exercise"].strip(),
            "workout_type": page["workout_type"],
        }
        for page in pages
        if page["exercise"].strip()
    ]


def get_default_exercises_for_type(workout_type: str) -> list[str]:
    """Exercises that should always be available for a workout type."""
    defaults = {
        "Push": [
            "High-to-Low Cable Fly (cable/rope)",
            "Cable Decline Press (cable/rope)",
            "Dips (forward lean, bodyweight)",
            "Decline DB Bench Press",
        ],
        "Legs": [
            "Barbell Squat",
            "Hack Squats",
            "Leg Press",
            "Leg Curls",
            "Back Extension",
            "Smith Machine Squats",
        ],
        "Abs": [
            "Crunches",
            "Hanging Leg Raises",
            "Planks",
            "Cable Crunches",
            "Russian Twists",
            "Ab Wheel",
        ],
        "Cardio": [
            "Treadmill",
            "Bike",
            "Stairmaster",
            "Rowing Machine",
        ],
    }
    return defaults.get(workout_type, [])


async def last_workout_type(before: str | None = None) -> str | None:
    """The workout type of the most recent split-advancing entry.

    Uses `Date (user input)` as the primary sort; rows without a date fall
    back to `created_time`. Old rows only carry Muscle Group, so the type is
    derived via the shared mapping when the Workout type tag is empty.
    """
    pages = await store_client().fetch_workouts()

    def sort_key(page: Mapping[str, Any]) -> str:
        return (
            page.get("date") or str(page.get("created_time") or "")[:10]
            or ""
        )

    pages.sort(key=sort_key, reverse=True)
    for page in pages:
        if before is not None and sort_key(page) >= before:
            continue
        types = page.get("workout_type") or []
        if types:
            workout_type = next(
                (t for t in domain.WORKOUT_ROTATION if t in types),
                types[0],
            )
        else:
            muscles = page.get("muscle_group") or []
            workout_type = domain.workout_type_from_muscle(muscles)
        if workout_type in domain.WORKOUT_ROTATION:
            return workout_type
    return None


async def workout_plan_payload() -> dict[str, Any]:
    """The upcoming Push → Pull → Legs rotation with exercises per day.

    The plan derives from the most recent logged workout; Abs/Cardio days do
    not advance the split. Exercises come from the PR log (Max Reps) tagged
    with the planned type, plus an Abs/core list that can be done any day.
    `upcoming` lists the next `window` rotation days in order so the user can
    read ahead and never look up exercise names.

    Smart rotation adjustment: when the user logs a workout type that differs
    from what was scheduled, the rotation anchor shifts so tomorrow continues
    from after the logged type instead of from the stale anchor. This prevents
    duplicate days (e.g. two Legs days in a row) and keeps the rotation
    aligned with what the user actually trained.
    """
    today_date = domain.effective_date()
    today = today_date.isoformat()
    todays_workouts, rotation_anchor, known = await asyncio.gather(
        fetch_workouts(today_date),
        last_workout_type(before=today),
        fetch_known_exercises(),
    )

    # ── Extract today's logged workout type ──────────────────────────────
    todays_logged_type: str | None = None
    for w in todays_workouts:
        types = w.get("workout_type") or []
        for t in types:
            if t in domain.WORKOUT_ROTATION:
                todays_logged_type = t
                break
        if todays_logged_type is None:
            muscles = w.get("muscle_group") or []
            derived = domain.workout_type_from_muscle(muscles)
            if derived and derived in domain.WORKOUT_ROTATION:
                todays_logged_type = derived
        if todays_logged_type:
            break

    # Also resolve the global last workout for backward-compat `last_workout`
    todays_last: str | None = todays_logged_type
    if todays_last is None:
        todays_last = await last_workout_type()

    def exercises_for(workout_type: str) -> list[dict[str, Any]]:
        names = get_default_exercises_for_type(workout_type)
        seen = set(names)
        for ex in known:
            if workout_type in ex["workout_type"] and ex["name"] not in seen:
                names.append(ex["name"])
                seen.add(ex["name"])
        return [{"name": name} for name in names]

    window = 5  # a 5-day training week at most cycles the split twice
    upcoming: list[dict[str, Any]] = []

    # ── Smart rotation adjustment ────────────────────────────────────────
    scheduled = domain.next_workout_type(rotation_anchor)
    if todays_logged_type and todays_logged_type in domain.WORKOUT_ROTATION:
        # Today has a rotation-type workout — always show it as the first day.
        upcoming.append(
            {
                "type": todays_logged_type,
                "exercises": exercises_for(todays_logged_type),
            }
        )
        if todays_logged_type != scheduled:
            # User logged a different type than scheduled — shift the anchor
            # so tomorrow starts after what was actually trained today.
            try:
                idx = domain.WORKOUT_ROTATION.index(todays_logged_type)
                day_type = domain.WORKOUT_ROTATION[
                    (idx + 1) % len(domain.WORKOUT_ROTATION)
                ]
            except ValueError:
                day_type = domain.next_workout_type(todays_logged_type)
        else:
            # Logged matches scheduled — advance normally from logged type.
            day_type = domain.next_workout_type(todays_logged_type)
        for _ in range(window - 1):
            upcoming.append(
                {
                    "type": day_type,
                    "exercises": exercises_for(day_type),
                }
            )
            day_type = domain.next_workout_type(day_type)
    else:
        # No rotation-type workout logged today — fall back to the anchor.
        day_type = scheduled
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
        "last_workout": todays_last,
        "upcoming": upcoming,
        "core": exercises_for("Abs"),
    }


async def workout_stats_payload() -> dict[str, Any]:
    """One compact read containing every Workout dashboard metric."""
    today = domain.effective_date()
    week_start = today - timedelta(days=today.weekday())
    rows, prs, plan = await asyncio.gather(
        # Keep enough history to calculate a useful qualifying-week streak.
        fetch_workouts_in_range(week_start - timedelta(days=52 * 7), today),
        fetch_prs(),
        workout_plan_payload(),
    )
    aggregates = domain.workout_week_stats(rows, today)
    today_rows = [row for row in rows if row.get("date") == today.isoformat()]
    exercises: dict[str, dict[str, Any]] = {}
    for row in today_rows:
        name = row.get("exercise") or "Exercise"
        item = exercises.setdefault(name, {"name": name, "sets": 0, "weight": "0", "reps": "0"})
        sets = row.get("sets") or []
        item["sets"] += len(sets)
        if sets:
            best = max(sets, key=lambda value: float(value.get("weight") or 0))
            item["weight"] = f'{float(best.get("weight") or 0):g}'
            item["reps"] = f'{float(best.get("reps") or 0):g}'

    upcoming = plan.get("upcoming") or []
    compact_plan = [
        {"type": item["type"], "exercises": [ex["name"] for ex in item["exercises"]]}
        for item in upcoming
    ]
    return {
        "today": {
            "date": today.isoformat(),
            "entries": len(today_rows),
            "exercises": list(exercises.values()),
        },
        **aggregates,
        "prs": prs,
        "plan": {
            "today": compact_plan[0] if compact_plan else {"type": "Push", "exercises": []},
            "next": compact_plan[1:],
        },
    }


async def write_workout(
    exercise: str,
    sets: Any,
    workout_type: str | None,
    day_value: str | None,
) -> dict[str, Any]:
    """Validate, write one Fitness Tracker row, and return it."""
    clean_exercise = domain.validate_name(exercise, "exercise")
    is_rest = str(workout_type or "").strip().lower() == "rest"
    if is_rest:
        if sets != []:
            raise domain.MacroError("rest days require sets=[]")
        cleaned_sets: list[dict[str, float]] = []
        type_name = "Rest"
        muscle_group: list[str] = []
    else:
        cleaned_sets = domain.validate_sets(sets)
        type_name = domain.normalize_workout_type(workout_type)
        muscle_group = domain.normalize_muscle_group([type_name])
    day = domain.resolve_date(day_value, "date")

    page = await store_client().insert_workout(exercise=clean_exercise,
        workout_type=[type_name], muscle_group=muscle_group, sets=cleaned_sets, day=day)
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
    ensure: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
    exclude: str | None = None,
) -> dict[str, Any]:
    """The canonical day view every client renders.

    `ensure` and `exclude` reconcile the response with a write that just
    happened. The reconciliation also keeps write responses deterministic.
    """
    meals = await fetch_meals(day)
    if exclude:
        meals = [meal for meal in meals if meal["id"] != exclude]
    ensured = ensure if isinstance(ensure, list) else ([ensure] if ensure else [])
    known_ids = {meal["id"] for meal in meals}
    meals = [dict(meal) for meal in ensured if meal["id"] not in known_ids] + meals
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
                "fiber": preset["fiber"],
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
    allow_estimate: bool = False,
    fiber: Any = 0,
) -> dict[str, Any]:
    """Validate, write one row, and return the day's corrected numbers."""
    clean_name = domain.validate_name(name)
    macros = domain.validate_macros(calories, protein, carbs, fat, fiber)
    source = (macro_source or "").strip() if allow_estimate else domain.validate_macro_source(macro_source)
    if not source:
        source = "Chat & Log"
    meal_name = domain.normalize_meal(meal)
    day = domain.resolve_date(day_value)

    page = await store_client().insert_meal(name=clean_name, meal=meal_name,
        day=day, macro_source=source, **macros)

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


KNOWN_CHAT_FOODS = """Moe's cookie 170 kcal, 2g protein, 23g carbs, 8g fat
Fairlife 30g shake 150/30/3/2.5
Barebells 200/20/21/7
Rice Krispies Treat 90/1/16/3
TJ olive oil butter 80/0/0/9
TJ artisan roll 200/8/38/2
93/7 ground beef about 42.5 kcal/oz
sweet potato about 86 kcal/100g
3 large eggs 216/19/1/14
McNuggets 10pc 410/24/25/25
Michelob Ultra 95 kcal, 0g protein, 2.6g carbs, 0g fat"""


def _extract_chat_items(content: str) -> tuple[list[Any], str | None]:
    """Accept a bare JSON array (preferred), fenced JSON, or prose for non-food chat."""
    cleaned = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", cleaned, re.DOTALL)
    candidate = fenced.group(1) if fenced else cleaned
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, list):
            return parsed, None
    except (json.JSONDecodeError, TypeError):
        pass
    array = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if array:
        try:
            parsed = json.loads(array.group(0))
            if isinstance(parsed, list):
                reply = (cleaned[: array.start()] + cleaned[array.end() :]).strip()
                return parsed, reply or None
        except json.JSONDecodeError:
            pass
    return [], cleaned or None


def _extract_vision_result(content: str) -> dict[str, Any]:
    """Extract the single JSON object requested from the vision response."""
    cleaned = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else [cleaned]
    embedded = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if embedded and embedded.group(0) not in candidates:
        candidates.append(embedded.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue
    raise MacroError("I couldn't analyze that photo. Please try another one.")


def _openai_access_token() -> str:
    token = os.environ.get("OPENAI_ACCESS_TOKEN", "").strip()
    if not token:
        raise MacroError(
            "Chat is not configured yet. Add OPENAI_ACCESS_TOKEN to the server environment."
        )
    return token


async def _post_openai_chat(token: str, payload: dict[str, Any]) -> httpx.Response:
    for retry_delay in (0.5, 1.5, None):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TransportError:
            if retry_delay is None:
                raise
            await asyncio.sleep(retry_delay)
    raise RuntimeError("unreachable")


async def parse_chat_message(message: str) -> tuple[list[Any], str | None]:
    day = domain.effective_date()
    meals, targets, presets = await asyncio.gather(
        fetch_meals(day), fetch_targets(day), fetch_presets()
    )
    totals = domain.sum_macros(meals)
    preset_context = [
        {key: preset[key] for key in ("name", "calories", "protein", "carbs", "fat", "fiber", "meal")}
        for preset in presets
    ]
    system = f"""You parse food messages for Matthew's macro tracker.
Current totals: {json.dumps(totals)}. Targets: {json.dumps(targets)}.
Available presets: {json.dumps(preset_context)}.
Known foods (values are calories/protein/carbs/fat unless labeled):
{KNOWN_CHAT_FOODS}

For a food-related message, output ONLY a JSON array with one object per item:
[{{"name":"...","calories":0,"protein":0,"carbs":0,"fat":0,"fiber":0,"meal":"Breakfast|Lunch|Dinner|Snack","note":"source"}}]
Use a matching preset or known-food value when possible. Infer the meal from context and time; default to Snack. For an ambiguous or unknown food, make a reasonable macro estimate and set note exactly to ESTIMATE. Coffee without stated additions is 5 kcal with zero macros. Never add commentary around a food JSON array.
If the message is a greeting, question, or otherwise not asking to log food, output [] followed by one short plain-text reply. Do not invent food items."""
    try:
        payload = {
            "model": "gpt-5.6-luna",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ],
        }
        token = _openai_access_token()
        response = await _post_openai_chat(token, payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise MacroError("I couldn't parse that right now. Please try again in a moment.") from exc
    return _extract_chat_items(str(content))


async def analyze_food_image(image: str, meal_hint: str | None = None) -> dict[str, Any]:
    """Ask OpenAI's vision-capable chat model for one conservative macro estimate."""
    if not image.startswith("data:image/") or ";base64," not in image:
        raise MacroError("Please provide a valid food photo.")
    if len(image.encode("utf-8")) > 2_800_000:
        raise MacroError("That photo is too large. Please choose a smaller image.")

    meal = domain.normalize_meal(meal_hint) if meal_hint else None
    system = """You are a food logging assistant. Look at this food image and identify what food or meal is shown. Return ONLY a JSON object with: {"name":"...","calories":0,"protein":0,"carbs":0,"fat":0,"fiber":0,"note":"ESTIMATE — vision model"}. Estimate macros conservatively — when unsure, estimate low on protein and high on calories. If you cannot identify the food, return {"error":"Could not identify food"}."""
    prompt = "Identify this food and estimate its macros."
    if meal:
        prompt += f" The user says this is for {meal}."
    payload = {
        "model": "gpt-5.6-luna",
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            },
        ],
    }
    try:
        token = _openai_access_token()
        response = await _post_openai_chat(token, payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result = _extract_vision_result(str(content))
    except MacroError:
        raise
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise MacroError(
            "I couldn't analyze that photo right now. Please try again in a moment."
        ) from exc

    if result.get("error"):
        raise MacroError("I couldn't identify food in that photo. Try a clearer angle.")
    clean_name = domain.validate_name(str(result.get("name", "")))
    macros = domain.validate_macros(
        result.get("calories"), result.get("protein"), result.get("carbs"), result.get("fat"), result.get("fiber", 0)
    )
    return {
        "name": clean_name,
        **macros,
        "note": "ESTIMATE — vision model",
        **({"meal": meal} if meal else {}),
    }


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
        fiber=preset["fiber"] * count,
        macro_source=f"Meal Preset: {preset['name']}",
        meal=meal or preset["meal"],
        day_value=day_value,
    )


# --------------------------------------------------------------------------- #
# MCP tools
# --------------------------------------------------------------------------- #


def tool_errors(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Surface domain and database messages to Poke instead of a masked error."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except MacroError as exc:
            raise ToolError(str(exc)) from exc
        except StoreError as exc:
            raise ToolError(f"Database rejected the request. {exc}") from exc

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
    fiber: float = 0,
) -> dict[str, Any]:
    """Log one food or meal to the nutrition log and return the day's totals.

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
        name, calories, protein, carbs, fat, macro_source, meal, date, fiber=fiber
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
                    "fiber": preset["fiber"],
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
    fiber: float | None = None,
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
        fiber: Grams of fiber per serving. When updating an existing preset,
            omitted fiber preserves its current value; new presets must provide it.
    """
    clean_name = domain.validate_name(name, "name")
    meal_name = domain.normalize_meal(meal, default="Dinner")
    glyph = (emoji or "🍽️").strip()[:4] or "🍽️"

    client = store_client()
    existing_pages = await client.fetch_presets(active_only=False)
    existing = next(
        (
            page
            for page in existing_pages
            if page["name"].strip().lower() == clean_name.lower()
        ),
        None,
    )
    if fiber is None:
        if existing is None:
            raise domain.MacroError(
                "fiber is required when creating a new preset; pass the explicit fiber value"
            )
        existing_fiber = existing.get("fiber")
        if existing_fiber is None:
            raise domain.MacroError(
                "fiber is required because the existing preset has no stored fiber value"
            )
        fiber = float(existing_fiber)
    macros = domain.validate_macros(calories, protein, carbs, fat, fiber)

    properties = {"name": clean_name, "emoji": glyph, "meal": meal_name, **macros}

    if existing is None:
        order = 0.0
        for page in existing_pages:
            order = max(order, float(page["sort_order"]))
        properties["sort_order"] = order + 1
        page = await client.save_preset(properties)
        action = "created"
    else:
        page = await client.save_preset(properties, existing["id"])
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
    await store_client().delete("nutrition_entries", newest["id"])
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
    fiber: float | None = None,
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
        fiber: Daily fiber target. When omitted, preserves the target currently
            in effect; if none exists, it must be provided explicitly.
    """
    day = domain.resolve_date(effective_date, "effective_date")
    if fiber is None:
        existing_target = await store_client().fetch_targets(day)
        if not existing_target:
            raise domain.MacroError(
                "fiber is required when no existing target is in effect; pass the explicit fiber value"
            )
        existing_fiber = existing_target.get("fiber")
        if existing_fiber is None:
            raise domain.MacroError(
                "fiber is required because the existing target has no stored fiber value"
            )
        fiber = float(existing_fiber)
    macros = domain.validate_macros(calories, protein, carbs, fat, fiber)
    page = await store_client().insert_targets(day, macros)
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


class BriefStorageError(RuntimeError):
    """A clean, user-facing failure while reading or writing daily briefs."""


def read_brief(day: _date) -> dict[str, Any]:
    path = CONFIG.briefs_dir / f"{day.isoformat()}.json"
    try:
        if not path.exists():
            return {"text": None, "date": day.isoformat()}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BriefStorageError(
            f"Could not read the brief for {day.isoformat()}"
        ) from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise BriefStorageError(f"The brief for {day.isoformat()} is invalid")
    return {"text": text, "date": day.isoformat()}


def write_brief(text: str, day: _date) -> dict[str, Any]:
    directory = CONFIG.briefs_dir
    temp_path: str | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{day.isoformat()}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump({"text": text, "date": day.isoformat()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, directory / f"{day.isoformat()}.json")
    except OSError as exc:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise BriefStorageError(
            f"Could not store the brief for {day.isoformat()}"
        ) from exc
    return {"text": text, "date": day.isoformat()}


async def write_brief_to_notion(text: str, day: _date) -> None:
    await store_client().put_brief(day, text)


async def read_brief_from_notion(day: _date) -> dict[str, Any]:
    return {"text": await store_client().get_brief(day), "date": day.isoformat()}

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
            except StoreError as exc:
                return _with_cors(
                    JSONResponse({"error": str(exc)}, status_code=502), request
                )
            except BriefStorageError as exc:
                return _with_cors(
                    JSONResponse({"error": str(exc)}, status_code=500), request
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
        fiber=body.get("fiber", 0),
        macro_source=str(body.get("macro_source", "")),
        meal=body.get("meal"),
        day_value=body.get("date"),
    )


@api_route("/api/vision-log", methods=["POST"])
@api_route("/api/macro/vision-log", methods=["POST"])
async def api_vision_log(request: Request) -> Any:
    body = await _json_body(request)
    image = body.get("image")
    if not isinstance(image, str) or not image:
        raise MacroError("image is required")
    meal_hint = body.get("meal")
    if meal_hint is not None and not isinstance(meal_hint, str):
        raise MacroError("meal must be a string")
    return await analyze_food_image(image, meal_hint)


@api_route("/api/chat", methods=["POST"])
async def api_chat(request: Request) -> Any:
    body = await _json_body(request)
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise MacroError("message is required")
    if len(message.strip()) > 1000:
        raise MacroError("message must be 1000 characters or fewer")

    raw_items, conversational_reply = await parse_chat_message(message.strip())
    if not raw_items:
        current = await day_payload(domain.effective_date())
        return {
            "reply": conversational_reply or "Tell me what you ate and I'll log it.",
            "logged": [],
            "totals": current["totals"],
        }

    day = domain.effective_date()
    validated: list[tuple[str, dict[str, float], str, str]] = []
    for raw in raw_items[:10]:
        if not isinstance(raw, dict):
            raise MacroError("I couldn't understand one of those food items. Please rephrase it.")
        clean_name = domain.validate_name(str(raw.get("name", "")))
        macros = domain.validate_macros(
            raw.get("calories"), raw.get("protein"), raw.get("carbs"), raw.get("fat"), raw.get("fiber", 0)
        )
        meal_name = domain.normalize_meal(raw.get("meal"))
        validated.append(
            (clean_name, macros, str(raw.get("note") or "Chat & Log"), meal_name)
        )

    logged: list[dict[str, Any]] = []
    for clean_name, macros, note, meal_name in validated:
        result = await write_meal(
            clean_name,
            macros["calories"], macros["protein"], macros["carbs"], macros["fat"],
            note, meal_name, day.isoformat(),
            allow_estimate=True,
            fiber=macros["fiber"],
        )
        logged.append(result["logged"])

    current = await day_payload(day, ensure=logged)
    names = ", ".join(f'{item["name"]} {item["calories"]:g} kcal ✓' for item in logged)
    totals = current["totals"]
    targets = current["targets"]
    return {
        "reply": f'Logged {len(logged)} item{"s" if len(logged) != 1 else ""} — {names}. Total now {totals["calories"]:g}/{targets["calories"]:g} kcal.',
        "logged": logged,
        "totals": totals,
    }


@api_route("/api/brief", methods=["GET", "POST"])
async def api_brief(request: Request) -> Any:
    if request.method == "GET":
        date_value = request.query_params.get("date")
        day = domain.resolve_date(date_value, "date")
        local = read_brief(day)
        if local["text"] is not None:
            return local
        try:
            return await read_brief_from_notion(day)
        except StoreError:
            return local

    body = await _json_body(request)
    text = body.get("text")
    if not isinstance(text, str):
        raise MacroError("text must be a string")
    day = domain.resolve_date(body.get("date"), "date")
    result = write_brief(text, day)
    try:
        await write_brief_to_notion(text, day)
    except StoreError:
        pass
    return result


@api_route("/api/meal/{page_id}", methods=["DELETE"])
async def api_delete_meal(request: Request) -> Any:
    page_id = request.path_params["page_id"].strip()
    if not page_id:
        raise MacroError("A meal id is required to delete an entry")
    await store_client().delete("nutrition_entries", page_id)
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


@api_route("/api/workout-stats", methods=["GET"])
async def api_workout_stats(request: Request) -> Any:
    return await workout_stats_payload()


@api_route("/api/workout", methods=["POST"])
async def api_log_workout(request: Request) -> Any:
    body = await _json_body(request)
    return await write_workout(
        exercise=str(body.get("exercise", "")),
        sets=body.get("sets"),
        workout_type=body.get("workout_type"),
        day_value=body.get("date"),
    )


@api_route("/api/workouts/last", methods=["GET"])
async def api_last_workout(request: Request) -> Any:
    row = await fetch_last_workout(request.query_params.get("exercise", ""))
    if row is None:
        return {"sets": []}
    return {
        "exercise": row["exercise"],
        "sets": row["sets"][:4],
        "date": row["date"],
        "workout_type": row["workout_type"],
    }


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
    await store_client().delete("fitness_tracker", page_id)
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
