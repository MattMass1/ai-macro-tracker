"""FastMCP service: `/mcp` for Poke and `/api/*` for the PWA, from one process.

Both front doors call the same helpers below, so the day-boundary rule, the
macro totals, and the validation exist exactly once.
"""

from __future__ import annotations

import functools
import hmac
import json
import logging
import os
import sys
import tempfile
import asyncio
import re
import time
from collections import defaultdict, deque
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
from store import ChatQuotaExceeded, InviteAlreadyClaimed, InviteNotFound  # noqa: E402
from auth import bind_user, current_user_id, reset_user  # noqa: E402
from coach import CoachProviderError, run_agent  # noqa: E402
import food_lookup  # noqa: E402

logger = logging.getLogger(__name__)

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
# Security: gate the raw MCP protocol endpoint behind the same device-token
# auth as /api/*. The MCP tools duplicate the REST API and must NOT be
# reachable anonymously (Fable security sweep, C1).
# --------------------------------------------------------------------------- #
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request as _Request  # noqa: E402
from starlette.responses import JSONResponse as _JSONResponse  # noqa: E402


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated requests to the MCP protocol endpoint.

    /health stays public; /api/* routes do their own auth (they resolve the
    user from the bearer token). Only the raw MCP tool protocol needs the
    blanket guard, because FastMCP's HTTP transport has no per-request auth.
    """

    async def dispatch(self, request: _Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path == "/health":
            return await call_next(request)
        if path.startswith("/mcp"):
            user_id = await _authenticated_user(request)
            if user_id is None:
                return _JSONResponse({"error": "Missing or invalid bearer token"}, status_code=401)
            request.state.mcp_user_id = user_id
        return await call_next(request)


# --------------------------------------------------------------------------- #
# Shared business helpers — used by both the MCP tools and the REST routes
# --------------------------------------------------------------------------- #


async def fetch_meals(day: _date) -> list[dict[str, Any]]:
    """Every `Nutrition Entries` row for one logging day, newest first."""
    return await store_client().fetch_meals(day)


async def fetch_meals_in_range(start: _date, end: _date) -> list[dict[str, Any]]:
    return await store_client().fetch_meals(start, end)


async def fetch_day_rollups(
    start: _date | None = None, end: _date | None = None
) -> list[dict[str, Any]]:
    return await store_client().fetch_day_rollups(start, end)


async def fetch_meal_rollups(day: _date) -> list[dict[str, Any]]:
    return await store_client().fetch_meal_rollups(day)


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
    pages, library = await asyncio.gather(
        store_client().fetch_prs(), store_client().fetch_workout_library()
    )
    merged = {
        item["name"].casefold(): {
            **item,
            # Preserve the existing /api/exercises array-valued type contract.
            "workout_type": [item["workout_type"]],
        }
        for item in library
    }
    for page in pages:
        name = page["exercise"].strip()
        if not name:
            continue
        existing = merged.get(name.casefold())
        if existing is None:
            merged[name.casefold()] = {
                "name": name,
                "workout_type": page["workout_type"],
                "muscle_group": [],
                "equipment": None,
                "difficulty": None,
                "swaps": [],
            }
        elif page["workout_type"] and not existing.get("workout_type"):
            existing["workout_type"] = page["workout_type"]
    return sorted(merged.values(), key=lambda item: item["name"].casefold())


def get_default_exercises_for_type(workout_type: str) -> list[str]:
    """Exercises that should always be available for a workout type."""
    return domain.get_default_exercises_for_type(workout_type)


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


def _as_day(value: Any) -> _date | None:
    """Coerce a stored timestamp/date into a plain date (None-safe)."""
    if value is None:
        return None
    return value.date() if hasattr(value, "date") else value


async def _resolve_today_session() -> tuple[int, str, list[dict[str, Any]], bool]:
    """Resolve today's scheduled rotation session without writing.

    Returns (rotation_index, today_type, exercises, done). The index walks the
    plan rotation past the last logged split-advancing workout, overlaid by the
    per-user day-state: a completion recorded today pins today's type and marks
    it done; a completion from an earlier day advances the rotation. Raises
    MacroError when no plan exists yet (the coach should onboard first).
    """
    today = domain.effective_date()
    plan = await store_client().fetch_workout_plan()
    if not plan:
        raise MacroError("No workout plan yet — build one first.")
    rotation = [str(t) for t in (plan.get("rotation") or []) if str(t).strip()]
    if not rotation:
        rotation = list(domain.WORKOUT_ROTATION)
    days_raw = plan.get("days")
    if isinstance(days_raw, dict):
        days = days_raw
    elif isinstance(days_raw, list):
        # Legacy plans store days as [{type, exercises}] rows, the same shape
        # workout_plan_payload already reads.
        days = {str(item["type"]): item
                for item in days_raw if isinstance(item, dict) and item.get("type")}
    else:
        days = {}
    state = await store_client().fetch_session_day_state()
    index: int | None = None
    done = False
    if state is not None:
        stored_index = state.get("rotation_index")
        done_date = _as_day(state.get("done_date"))
        if stored_index is not None:
            if done_date == today:
                index, done = int(stored_index), True
            elif done_date is not None and done_date < today:
                index = (int(stored_index) + 1) % len(rotation)
            else:
                index = int(stored_index)
    if index is None:
        last = await last_workout_type(before=today.isoformat())
        if last is not None:
            match = next((t for t in rotation if t.casefold() == last.casefold()), None)
            index = (rotation.index(match) + 1) % len(rotation) if match is not None else 0
        else:
            index = 0
    index = min(index, len(rotation) - 1)
    today_type = rotation[index]
    day = days.get(today_type) if isinstance(days.get(today_type), dict) else {}
    exercises = [ex for ex in day.get("exercises", []) if isinstance(ex, dict)]
    return index, today_type, exercises, done


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
    todays_workouts, rotation_anchor, known, stored_plan, day_state = await asyncio.gather(
        fetch_workouts(today_date),
        last_workout_type(before=today),
        fetch_known_exercises(),
        store_client().fetch_workout_plan(),
        store_client().fetch_session_day_state(),
    )

    planned_exercises = {
        str(item.get("type")): [
            str(exercise.get("name"))
            for exercise in item.get("exercises", [])
            if isinstance(exercise, dict) and exercise.get("name")
        ]
        for item in (stored_plan or {}).get("days", [])
        if isinstance(item, dict) and item.get("type")
    }
    if isinstance((stored_plan or {}).get("days"), dict):
        planned_exercises = {
            workout_type: [
                str(exercise.get("name"))
                for exercise in day.get("exercises", [])
                if isinstance(exercise, dict) and exercise.get("name")
            ]
            for workout_type, day in stored_plan["days"].items()
            if isinstance(day, dict)
        }

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
        # A missing plan intentionally starts empty. Matt's legacy defaults are
        # data seeded by the migration, never a default leaked to new users.
        # ONLY the plan's stored exercises for this day — never append the
        # user's full PR-log history (that's what made sessions balloon).
        names = list(planned_exercises.get(workout_type, ()))
        return [{"name": name} for name in names[:8]]  # hard cap 8

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

    # The app's Today view reads this payload: expose the per-user day-state
    # completion flag so a session the coach marked done shows as done here.
    if upcoming:
        upcoming[0]["done"] = _as_day((day_state or {}).get("done_date")) == today_date

    return {
        "rotation": list(domain.WORKOUT_ROTATION),
        "last_workout": todays_last,
        "upcoming": upcoming,
        "core": exercises_for("Abs"),
        "has_plan": bool(stored_plan),
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
    plan_today = compact_plan[0] if compact_plan else {"type": "Push", "exercises": []}
    if compact_plan and upcoming[0].get("done") is not None:
        plan_today["done"] = upcoming[0]["done"]
    return {
        "today": {
            "date": today.isoformat(),
            "entries": len(today_rows),
            "exercises": list(exercises.values()),
        },
        **aggregates,
        "prs": prs,
        "plan": {
            "today": plan_today,
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
    meals, meal_rollups, day_rollups = await asyncio.gather(
        fetch_meals(day),
        fetch_meal_rollups(day),
        fetch_day_rollups(day, day),
    )
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
        # `meals` remains the legacy food-entry array consumed by the PWA.
        # These additive keys expose the normalized hierarchy without changing
        # any existing response field.
        "day_rollup": day_rollups[0] if day_rollups else None,
        "meal_rollups": meal_rollups,
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
        "macro_source": source,
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
Banana 105/1/27/0
Rice Krispies Treat 90/1/16/3
TJ olive oil butter 80/0/0/9
TJ artisan roll 200/8/38/2
93/7 ground beef about 42.5 kcal/oz
sweet potato about 86 kcal/100g
3 large eggs 216/19/1/14
McNuggets 10pc 410/24/25/25
Michelob Ultra 95 kcal, 0g protein, 2.6g carbs, 0g fat"""

KNOWN_CHAT_FOOD_NAMES = (
    "Moe's cookie",
    "Fairlife 30g shake",
    "Barebells",
    "Banana",
    "Rice Krispies Treat",
    "TJ olive oil butter",
    "TJ artisan roll",
    "93/7 ground beef",
    "sweet potato",
    "3 large eggs",
    "McNuggets 10pc",
    "Michelob Ultra",
)


def _normalized_food_name(name: str) -> str:
    """Normalize only case and whitespace for exact food-name matching."""
    return " ".join(name.split()).casefold()


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


async def parse_chat_message(
    message: str,
) -> tuple[list[Any], str | None, list[dict[str, Any]]]:
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
[{{"name":"...","calories":0,"protein":0,"carbs":0,"fat":0,"fiber":0,"quantity":null,"grams":null,"basis":"per_unit|per_100g|per_serving","sourced_from":"preset|known|estimate|lookup","meal":"Breakfast|Lunch|Dinner|Snack","note":"source string or ESTIMATE"}}]
The macros MUST be the FINAL TOTALS for exactly the portion the user stated. You own all portion arithmetic: "2 bananas" means return the macros for both bananas, and "200g sweet potato" means return the macros for 200g. Never return a per-unit or per-100g value when the user asked for a multiple or weighted portion. Set basis to per_unit for whole countable items, per_100g for foods sized from a weight-based value, or per_serving for a complete stated serving such as a bar, bowl, or saved preset. Set sourced_from to preset only when the name exactly identifies an available preset, known only when it exactly identifies a listed known food, lookup when you used lookup_food, or estimate when the macros are your fallback estimate. Prefer an exact available preset over a known-food match when both have the same name. Presets are per serving and have no serving weight: use them for whole servings only. When the user states grams for a preset, call lookup_food and scale its per-100g result instead; if lookup fails, keep your portion estimate labeled ESTIMATE. Use a matching preset or known-food value when possible. Infer the meal from context and time; default to Snack. Set quantity to the number of that item the user states ("1 bar" -> 1, "two cookies" -> 2, "a banana" -> 1); otherwise leave quantity null. Set grams only when the user states the portion weight ("100g chicken" -> 100, "3 oz venison" -> 85, converting oz/lb to grams); leave grams null when no weight is stated — never guess it. For an ambiguous or unknown food, make a reasonable final macro estimate. Coffee without stated additions is 5 kcal with zero macros. Never add commentary around a food JSON array.
If a food is not an exact available preset or exact known food, call lookup_food with its name BEFORE choosing macros. Use the returned per-100g or per-serving values to calculate final macros for the stated portion, set sourced_from to lookup, and copy its source string exactly into note. Never invent a source or macros for a real food while lookup_food is available. If lookup_food fails, returns not found, or the lookup limit is reached, proceed with a reasonable estimate and set note to exactly ESTIMATE.
If the message is a greeting, question, or otherwise not asking to log food, output [] followed by one short plain-text reply. Do not invent food items."""
    tools = [{
        "type": "function",
        "function": {
            "name": "lookup_food",
            "description": (
                "Look up real food macros using the USDA, OpenFoodFacts, and "
                "Tavily cascade. Call for foods not in presets or known foods."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    }]
    try:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]
        payload = {
            "model": "gpt-4o-mini",
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        token = _openai_access_token()
        lookups_left = 3
        verified_lookups: list[tuple[str, str]] = []
        while True:
            response = await _post_openai_chat(token, payload)
            response.raise_for_status()
            assistant = response.json()["choices"][0]["message"]
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                content = assistant.get("content") or ""
                break
            messages.append(assistant)
            executed_lookup = False
            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                result: dict[str, Any]
                if function.get("name") != "lookup_food":
                    result = {"result": "unsupported tool"}
                elif lookups_left <= 0:
                    result = {"result": "lookup limit reached; use ESTIMATE"}
                else:
                    lookups_left -= 1
                    executed_lookup = True
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                        name = str(arguments.get("name") or "").strip()
                        found = await food_lookup.resolve_food(name) if name else None
                        if found:
                            result = {
                                key: found[key]
                                for key in (
                                    "name", "macros_per_100g", "macros_per_serving",
                                    "serving_size", "source",
                                )
                                if key in found
                            }
                            source = result.get("source")
                            if isinstance(source, str) and source:
                                verified_lookups.append(
                                    (_normalized_food_name(name), source)
                                )
                        else:
                            result = {"result": "not found; use ESTIMATE"}
                    except Exception:
                        logger.exception("Parser food lookup failed; using estimate")
                        result = {"result": "lookup failed; use ESTIMATE"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": json.dumps(result, separators=(",", ":")),
                })
            if lookups_left <= 0 or not executed_lookup:
                payload["tool_choice"] = "none"
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise MacroError("I couldn't parse that right now. Please try again in a moment.") from exc
    items, reply = _extract_chat_items(str(content))
    for item in items:
        if not isinstance(item, dict):
            continue
        note = item.get("note")
        lookup = (_normalized_food_name(str(item.get("name") or "")), note)
        if lookup in verified_lookups:
            verified_lookups.remove(lookup)
        else:
            item["note"] = "ESTIMATE"
    return items, reply, presets


async def _upgrade_estimate(
    name: str, grams: Any, *, whole_item: bool = False
) -> tuple[dict[str, float], str] | None:
    """Try the free database cascade before writing a parser ESTIMATE.

    A user-stated gram portion can size a per-100g hit. An explicitly requested
    whole item can use a source's per-serving panel directly. Nothing infers a
    serving from per-100g data alone.
    """
    try:
        portion = float(grams)
    except (TypeError, ValueError):
        portion = None
    if portion is not None and not (0 < portion <= 5000):
        portion = None
    if portion is None and not whole_item:
        return None
    try:
        if whole_item and portion is None:
            found = await food_lookup.resolve_food(name, whole_item=True)
        else:
            found = await food_lookup.resolve_food(name)
        if not found:
            return None
        if portion is not None:
            return food_lookup.portion_from_grams(found, portion)
        return food_lookup.portion_from_serving(found)
    except Exception:
        logger.exception("Food lookup failed; keeping the estimate")
        return None


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
        "model": "gpt-4o-mini",
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
        context_token = bind_user(CONFIG.matt_user_id)
        try:
            return await fn(*args, **kwargs)
        except MacroError as exc:
            raise ToolError(str(exc)) from exc
        except StoreError as exc:
            raise ToolError(f"Database rejected the request. {exc}") from exc
        finally:
            reset_user(context_token)

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
    macro_source: str,
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
        macro_source: Where the per-serving numbers came from, e.g. "Fairlife
            Core Power label" or "FDA FoodData Central: oats, dry". Placeholders
            like "estimate" or "guess" are rejected.
        meal: Default meal slot — Breakfast, Lunch, Dinner, or Snack.
        emoji: Single emoji shown on the app's quick-add tile.
        fiber: Grams of fiber per serving. When updating an existing preset,
            omitted fiber preserves its current value; new presets must provide it.
    """
    clean_name = domain.validate_name(name, "name")
    source = domain.validate_macro_source(macro_source)
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

    properties = {"name": clean_name, "emoji": glyph, "meal": meal_name,
                  "macro_source": source, **macros}

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
            "macro_source": source,
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


async def _get_range_summary(start: str, end: str) -> dict[str, Any]:
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
async def get_range_summary(start: str, end: str) -> dict[str, Any]:
    """Per-day totals and averages across an inclusive YYYY-MM-DD range."""
    return await _get_range_summary(start, end)


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
    path = CONFIG.briefs_dir / str(current_user_id()) / f"{day.isoformat()}.json"
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
    directory = CONFIG.briefs_dir / str(current_user_id())
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
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-App-Token",
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


async def _authenticated_user(request: Request):
    authorization = request.headers.get("authorization", "")
    scheme, separator, raw_token = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and raw_token.strip():
        return await store_client().resolve_device(raw_token.strip())

    # Temporary deploy bridge for the existing PWA/iOS clients. Remove after
    # both clients ship per-device Bearer auth. The shared token is Matt-only.
    legacy_token = request.headers.get("x-app-token", "")
    if legacy_token and hmac.compare_digest(legacy_token, CONFIG.app_shared_token):
        return CONFIG.matt_user_id
    return None


def api_route(path: str, methods: list[str], *, public: bool = False):
    """Register an authenticated, CORS-enabled JSON route on the same app as /mcp."""

    def decorator(fn: Callable[[Request], Awaitable[Any]]):
        @mcp.custom_route(path, methods=[*methods, "OPTIONS"], name=fn.__name__)
        @functools.wraps(fn)
        async def handler(request: Request) -> Response:
            if request.method == "OPTIONS":
                return _with_cors(Response(status_code=204), request)
            context_token = None
            if not public:
                user_id = await _authenticated_user(request)
                if user_id is None:
                    return _with_cors(JSONResponse(
                        {"error": "Missing or invalid bearer token"}, status_code=401), request)
                request.state.user_id = user_id
                context_token = bind_user(user_id)
            try:
                payload = await fn(request)
            except InviteNotFound:
                return _with_cors(JSONResponse({"error": "Unknown invite code"}, status_code=404), request)
            except InviteAlreadyClaimed:
                return _with_cors(JSONResponse({"error": "Invite code already claimed"}, status_code=409), request)
            except MacroError as exc:
                return _with_cors(
                    JSONResponse({"error": str(exc)}, status_code=400), request
                )
            except StoreError as exc:
                return _with_cors(
                    JSONResponse({"error": str(exc)}, status_code=502), request
                )
            except CoachProviderError as exc:
                return _with_cors(
                    JSONResponse({"error": str(exc)}, status_code=502), request
                )
            except BriefStorageError as exc:
                return _with_cors(
                    JSONResponse({"error": str(exc)}, status_code=500), request
                )
            finally:
                if context_token is not None:
                    reset_user(context_token)
            status = 200
            if isinstance(payload, tuple):
                payload, status = payload
            response = _with_cors(JSONResponse(payload, status_code=status), request)
            return response

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


_INVITE_RATE_WINDOW_SECONDS = 15 * 60
_INVITE_RATE_MAX_ATTEMPTS = 10
_invite_attempts: dict[str, deque[float]] = defaultdict(deque)
_invite_rate_lock = asyncio.Lock()


async def _claim_invite_rate_limited(request: Request) -> bool:
    """Return whether this client IP has exhausted the invite attempt budget."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - _INVITE_RATE_WINDOW_SECONDS
    async with _invite_rate_lock:
        attempts = _invite_attempts[client_ip]
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= _INVITE_RATE_MAX_ATTEMPTS:
            return True
        attempts.append(now)
        return False


@api_route("/api/claim-invite", methods=["POST"], public=True)
async def api_claim_invite(request: Request) -> Any:
    if await _claim_invite_rate_limited(request):
        return {"error": "Too many invite attempts. Try again later."}, 429
    body = await _json_body(request)
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        raise MacroError("code is required")
    label = body.get("label")
    if label is not None and not isinstance(label, str):
        raise MacroError("label must be a string")
    display_name = body.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        raise MacroError("display_name must be a string")
    return await store_client().claim_invite(
        code.strip(),
        label.strip() if label else None,
        display_name.strip() if display_name else None,
    )


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


@api_route("/api/food/barcode", methods=["POST"])
async def api_food_barcode(request: Request) -> Any:
    body = await _json_body(request)
    code = body.get("code")
    if not isinstance(code, str):
        raise MacroError("code must be a string")
    raw_code = code.strip()
    if not re.fullmatch(r"\d{8,14}", raw_code):
        raise MacroError("code must contain 8 to 14 digits")
    normalized_code = raw_code.lstrip("0") or "0"
    found = await food_lookup.resolve_by_barcode(normalized_code)
    if found is None:
        return {"error": "Barcode not found in database"}, 404
    macros = found["macros_per_100g"]
    result = {
        "name": found["name"],
        **macros,
        "source": found["source"],
    }
    if found.get("serving_size"):
        result["serving_size"] = found["serving_size"]
    if found.get("macros_per_serving"):
        result["macros_per_serving"] = found["macros_per_serving"]
    return result


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
    # M3 (Fable): daily cap on the paid vision path — barcodes are unaffected.
    user_id = getattr(request.state, "user_id", None)
    if user_id is not None:
        used = await store_client().count_vision_logs_today(user_id)
        if used >= 20:
            raise MacroError("Daily vision-log limit reached (20 photos/day). Use chat logging or barcodes instead.")
    result = await analyze_food_image(image, meal_hint)
    if user_id is not None:
        await store_client().record_vision_log(user_id)
    return result


_LIBRARY_STOPWORDS = {
    "a", "an", "and", "day", "exercise", "exercises", "for", "friendly",
    "gym", "routine", "the", "to", "workout", "workouts",
}
_EQUIPMENT_ALIASES = {
    "barbell": "barbell", "barbells": "barbell", "bodyweight": "bodyweight",
    "cable": "cable", "cables": "cable", "dumbbell": "dumbbell",
    "dumbbells": "dumbbell", "db": "dumbbell", "kettlebell": "kettlebell",
    "kettlebells": "kettlebell", "machine": "machine", "machines": "machine",
    "treadmill": "treadmill",
}
_MUSCLE_ALIASES = {
    "arm": {"biceps", "triceps", "forearms"}, "arms": {"biceps", "triceps", "forearms"},
    "back": {"back", "lats", "upper back", "lower back"}, "biceps": {"biceps"},
    "calf": {"calves"}, "calves": {"calves"}, "chest": {"chest"},
    "core": {"core", "abs"}, "delt": {"shoulders", "rear delts"},
    "delts": {"shoulders", "rear delts"}, "forearm": {"forearms"},
    "forearms": {"forearms"}, "glute": {"glutes"}, "glutes": {"glutes"},
    "ham": {"hams", "hamstrings"}, "hams": {"hams", "hamstrings"},
    "hamstring": {"hams", "hamstrings"}, "hamstrings": {"hams", "hamstrings"},
    "lat": {"back", "lats"}, "lats": {"back", "lats"},
    "leg": {"quads", "glutes", "hams", "hamstrings", "calves"},
    "legs": {"quads", "glutes", "hams", "hamstrings", "calves"},
    "quad": {"quads"}, "quads": {"quads"},
    "shoulder": {"shoulders", "rear delts"}, "shoulders": {"shoulders", "rear delts"},
    "trap": {"back", "traps"}, "traps": {"back", "traps"}, "triceps": {"triceps"},
}
_WORKOUT_TYPES = {
    "abs": "abs", "cardio": "cardio", "leg": "legs", "legs": "legs",
    "pull": "pull", "push": "push",
}
_DIFFICULTIES = {"beginner", "intermediate", "advanced"}
_KNEE_STRESS_NAME_TERMS = (
    "squat", "lunge", "jump", "box jump", "burpee", "pistol squat",
    "split squat", "bulgarian", "step-up", "step up", "wall sit",
    "leg extension", "knee press", "kettlebell swing", "running", "sprint",
)
_KNEE_STRESS_MACHINE_NAMES = (
    "stairmaster", "stair master", "stair climber", "leg press", "hack squat",
    "sled push", "treadmill",
)
_KNEE_STRESS_EXCLUSIONS = (
    *_KNEE_STRESS_NAME_TERMS,
    *_KNEE_STRESS_MACHINE_NAMES,
)


def _is_knee_stress_exercise(row: Mapping[str, Any]) -> bool:
    """Return whether an exercise name matches the curated knee-stress list."""
    name = " ".join(re.findall(r"[a-z0-9]+", str(row.get("name", "")).casefold()))
    return any(term.replace("-", " ") in name for term in _KNEE_STRESS_EXCLUSIONS)


def search_workout_library(
    rows: list[dict[str, Any]], query: str, limit: int = 30,
) -> dict[str, Any]:
    """Rank exercise rows using the library's structured metadata."""
    normalized = " ".join(re.findall(r"[a-z0-9]+", query.casefold()))
    tokens = {token for token in normalized.split() if token not in _LIBRARY_STOPWORDS}
    equipment = {_EQUIPMENT_ALIASES[token] for token in tokens if token in _EQUIPMENT_ALIASES}
    if "commercial" in tokens:
        equipment.update({"barbell", "machine"})
    muscle_terms = [_MUSCLE_ALIASES[token] for token in tokens if token in _MUSCLE_ALIASES]
    muscles = set().union(*muscle_terms) if muscle_terms else set()
    workout_types = {_WORKOUT_TYPES[token] for token in tokens if token in _WORKOUT_TYPES}
    if "full body" in normalized:
        workout_types.add("full body")
    difficulties = tokens & _DIFFICULTIES
    knee_friendly = any(
        phrase in normalized
        for phrase in ("knee friendly", "no knee", "low impact", "knee pain", "avoid knee")
    )

    requested_fields = sum(bool(values) for values in (equipment, muscles, workout_types, difficulties))
    ranked: list[tuple[int, int, str, dict[str, Any], list[str]]] = []
    eligible_rows = [
        row for row in rows if not knee_friendly or not _is_knee_stress_exercise(row)
    ]
    for row in eligible_rows:
        name = str(row.get("name", ""))
        normalized_name = " ".join(re.findall(r"[a-z0-9]+", name.casefold()))
        reasons: list[str] = []
        score = 0
        matched_fields = 0
        row_equipment = str(row.get("equipment", "")).casefold()
        row_muscles = {str(value).casefold() for value in row.get("muscle_group", [])}
        row_type = str(row.get("workout_type", "")).casefold()
        row_difficulty = str(row.get("difficulty", "")).casefold()
        if normalized and normalized in normalized_name:
            score += 20 if normalized == normalized_name else 10
            reasons.append("name match")
        if equipment and row_equipment in equipment:
            score += 2
            matched_fields += 1
            reasons.append(f"equipment: {row_equipment}")
        if muscles and row_muscles & muscles:
            score += 3
            matched_fields += 1
            reasons.append("muscle group: " + ", ".join(sorted(row_muscles & muscles)))
        if workout_types and row_type in workout_types:
            score += 4
            matched_fields += 1
            reasons.append(f"workout type: {row_type}")
        if difficulties and row_difficulty in difficulties:
            score += 1
            matched_fields += 1
            reasons.append(f"difficulty: {row_difficulty}")
        if score:
            if knee_friendly:
                reasons.append("knee-friendly preference")
            ranked.append((matched_fields, score, name.casefold(), row, reasons))

    if not ranked:
        note = "no filter matched; full library returned"
        if knee_friendly:
            note += "; obvious knee-stress exercises excluded"
        return {
            "exercises": [
                {**row, "matched": "no structured filter matched"}
                for row in eligible_rows
                if not knee_friendly or not _is_knee_stress_exercise(row)
            ],
            "note": note,
        }
    if requested_fields and any(item[0] == requested_fields for item in ranked):
        ranked = [item for item in ranked if item[0] == requested_fields]
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    note = "ranked by structured library fields"
    if knee_friendly:
        note += "; obvious knee-stress exercises excluded"
    return {
        "exercises": [{**row, "matched": "; ".join(reasons)} for _, _, _, row, reasons in ranked[:limit]],
        "note": note,
    }


def exercise_card_widget(
    reply: str, tool_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build one exercise card from successful library lookups in this turn."""
    all_exercises: list[Mapping[str, Any]] = []
    newest_exercises: list[Mapping[str, Any]] | None = None
    for tool_result in reversed(tool_results):
        if tool_result.get("tool") not in {"get_library", "library_tool"} or not tool_result.get("ok"):
            continue
        result = tool_result.get("result")
        exercises = result.get("exercises") if isinstance(result, Mapping) else None
        if not isinstance(exercises, list) or not exercises:
            continue
        valid_exercises = [
            exercise for exercise in exercises if isinstance(exercise, Mapping)
        ]
        if newest_exercises is None:
            newest_exercises = valid_exercises
        all_exercises.extend(valid_exercises)

    reply_text = reply.strip().casefold()
    mentioned = [
        exercise for exercise in all_exercises
        if str(exercise.get("name") or "").strip()
        and str(exercise.get("name") or "").strip().casefold() in reply_text
    ]
    exact = [
        exercise for exercise in mentioned
        if str(exercise.get("name") or "").strip().casefold() == reply_text
    ]
    if exact:
        exercise = exact[0]
    elif mentioned:
        exercise = max(
            mentioned,
            key=lambda item: len(str(item.get("name") or "").strip()),
        )
    else:
        exercise = newest_exercises[0] if newest_exercises else None
        if exercise is None or str(exercise.get("name") or "").strip().casefold() not in reply_text:
            return None

    name = str(exercise.get("name") or "").strip()
    if not name:
        return None
    return {"type": "exercise_card", "exercise": {
            "exercise_name": name,
            "muscle_group": exercise.get("muscle_group"),
            "equipment": exercise.get("equipment"),
            "sets": exercise.get("sets"),
            "reps": exercise.get("reps"),
            "video_url": exercise.get("video_url"),
            "instructions": exercise.get("instructions"),
            "workout_type": exercise.get("workout_type"),
    }}


def reply_requests_metrics(reply: str) -> bool:
    """Recognize a model request that should have used request_metrics_form."""
    text = " ".join(reply.casefold().split())
    return any(re.search(pattern, text) for pattern in (
        r"\bmeasurements?\b",
        r"\bbody (?:metrics|measurements|stats)\b",
        r"\bheight\b",
        r"\b(?:your|current|goal) weight\b",
        r"\bhow much do you weigh\b",
    ))


def _coach_tool_handlers() -> dict[str, Callable[[Mapping[str, Any]], Awaitable[Any]]]:
    """Build tenant-bound coach tools; none accepts a user identifier."""
    async def set_display_name_tool(args):
        return await store_client().put_display_name(
            domain.validate_display_name(args.get("name"))
        )
    async def set_metrics_tool(args):
        return {"metrics": await store_client().put_metrics(domain.validate_metrics(args))}
    async def get_metrics_tool(_args):
        return {"metrics": await store_client().get_metrics() or {}}
    async def request_metrics_form_tool(_args):
        return {"type": "metrics_form", "fields": [
            "height_cm", "weight_kg", "goal_weight_kg", "age", "activity_level"
        ]}
    async def get_today_tool(_args): return await day_payload(domain.effective_date(), include_presets=True)
    async def get_day_tool(args): return await day_payload(domain.parse_date(str(args["date"])))
    async def get_range_tool(args): return await _get_range_summary(str(args["start"]), str(args["end"]))
    async def log_meal_tool(args):
        return await write_meal(
            str(args["name"]), args["calories"], args["protein"], args["carbs"],
            args["fat"], str(args["macro_source"]), str(args["meal_type"]), None,
            fiber=args["fiber"],
        )
    async def log_preset_tool(args):
        return await log_preset_servings(str(args["preset_name"]), args["servings"], str(args["meal"]))
    async def save_preset_tool(args):
        values = dict(args["values"])
        # A preset is a deferred log_meal, so it carries the same provenance
        # requirement — otherwise fabricated macros could be laundered through
        # save_preset and logged later via log_preset.
        macro_source = domain.validate_macro_source(str(args.get("macro_source") or ""))
        clean_name = domain.validate_name(str(values.get("name", "")))
        values.update(name=clean_name, emoji=str(values.get("emoji", "🍽️"))[:4],
                      meal=domain.normalize_meal(values.get("meal"), "Dinner"),
                      macro_source=macro_source)
        values.update(domain.validate_macros(*(values.get(key) for key in domain.MACRO_KEYS)))
        existing = next((row for row in await store_client().fetch_presets(False)
                         if row["name"].casefold() == clean_name.casefold()), None)
        return await store_client().save_preset(values, existing["id"] if existing else None)
    async def undo_tool(_args):
        day = domain.effective_date(); rows = await fetch_meals(day)
        if not rows: raise MacroError("There is nothing to undo today.")
        await store_client().delete("nutrition_entries", rows[0]["id"])
        return {"removed": rows[0], "day": await day_payload(day, exclude=rows[0]["id"])}
    async def get_targets_tool(_args):
        day = domain.effective_date(); return {"date": day.isoformat(), "targets": await fetch_targets(day)}
    async def set_targets_tool(args):
        values = dict(args["values"]); macros = domain.validate_macros(*(values.get(k) for k in domain.MACRO_KEYS))
        return await store_client().insert_targets(domain.effective_date(), macros)
    async def log_workout_tool(args):
        # muscle_group is accepted by the contract; domain derives the canonical group from type.
        return await write_workout(str(args["exercise"]), args["sets"], str(args["workout_type"]), None)
    async def recent_workouts_tool(args):
        return {"workouts": (await store_client().fetch_workouts())[:int(args["n"])]}
    async def get_plan_tool(_args): return {"plan": await store_client().fetch_workout_plan()}
    async def get_today_session_tool(_args):
        try:
            index, today_type, exercises, done = await _resolve_today_session()
            plan = await store_client().fetch_workout_plan()
            session_size = str(plan.get("session_size") or "") if isinstance(plan, dict) else ""
        except MacroError:
            return {"today_type": None, "exercises": [], "done": False, "has_plan": False}
        return {"today_type": today_type, "exercises": exercises, "done": done,
                "has_plan": True, "session_size": session_size or None}
    async def complete_today_session_tool(_args):
        index, today_type, exercises, already_done = await _resolve_today_session()
        await store_client().put_session_day_state(index, domain.effective_date())
        return {"today_type": today_type, "exercises": exercises, "done": True,
                "already_done": bool(already_done),
                "completed_on": domain.effective_date().isoformat()}
    async def set_plan_tool(args):
        raw_plan = args["plan"]
        if not isinstance(raw_plan, dict):
            raise MacroError("plan must be a JSON object")
        # --- Adapter: translate the coach's emitted plan shape to the canonical schema ---
        # The coach varies: rotation as [{name|day, exercises}] or ["Push",...]; the day
        # container may be days / workouts / sessions; labels under name or day.
        rotation_raw = raw_plan.get("rotation", raw_plan.get("sessions", []))
        canonical_rotation: list[str] = []
        sessions_by_name: dict[str, dict] = {}
        for entry in rotation_raw:
            if isinstance(entry, str):
                canonical_rotation.append(entry)
            elif isinstance(entry, dict):
                label = entry.get("name", entry.get("day"))
                if label:
                    canonical_rotation.append(str(label))
                    sessions_by_name[str(label)] = entry
        if not canonical_rotation:
            days_src = raw_plan.get("days") or raw_plan.get("workouts") or raw_plan.get("sessions")
            if isinstance(days_src, dict):
                canonical_rotation = list(days_src.keys())
            elif isinstance(days_src, list):
                for entry in days_src:
                    if isinstance(entry, dict) and entry.get("name", entry.get("day")):
                        canonical_rotation.append(str(entry.get("name", entry.get("day"))))
        days_raw = raw_plan.get("days", raw_plan.get("workouts"))
        if not isinstance(days_raw, dict):
            days_raw = {}
        # sessions as the day container: keyed by day/name
        sessions_raw = raw_plan.get("sessions", [])
        if isinstance(sessions_raw, list):
            for entry in sessions_raw:
                if isinstance(entry, dict):
                    label = entry.get("name", entry.get("day"))
                    if label and str(label) not in days_raw:
                        days_raw[str(label)] = entry
        for name, session in sessions_by_name.items():
            if name not in days_raw:
                days_raw[name] = session
        canonical_days: dict[str, dict] = {}
        for day_name, day in days_raw.items():
            if not isinstance(day, dict):
                continue
            exercises_raw = day.get("exercises", [])
            exercises = []
            for ex in exercises_raw:
                if not isinstance(ex, dict):
                    continue
                exercises.append({
                    "name": ex.get("name"),
                    "sets": ex.get("sets"),
                    "reps": ex.get("reps"),
                    "rest_sec": ex.get("rest_sec", ex.get("rest_seconds")),
                })
            canonical_days[day_name] = {"label": day.get("label", str(day_name)), "exercises": exercises}
        adapted = {
            "version": domain.WORKOUT_PLAN_VERSION,
            "rotation": canonical_rotation,
            "days": canonical_days,
        }
        if raw_plan.get("days_per_week") is not None:
            adapted["days_per_week"] = raw_plan["days_per_week"]
        if raw_plan.get("notes") is not None:
            adapted["notes"] = raw_plan["notes"]
        try: plan = domain.validate_workout_plan(adapted)
        except ValueError as exc: raise MacroError(str(exc)) from None
        library = await store_client().fetch_workout_library()
        by_id = {str(row["id"]): row for row in library if row.get("id") is not None}
        by_name: dict[str, list[Mapping[str, Any]]] = {}
        for row in library:
            by_name.setdefault(str(row["name"]).casefold(), []).append(row)
        normalized = [(re.sub(r"[^a-z0-9]", "", str(row["name"]).casefold()), row)
                      for row in library]

        def match_library_name(exercise_name: str) -> Mapping[str, Any] | None:
            exact_matches = by_name.get(exercise_name.casefold(), [])
            if len(exact_matches) > 1:
                raise MacroError("ambiguous exercise name, use the exact library name")
            if exact_matches:
                return exact_matches[0]
            plan_name = re.sub(r"[^a-z0-9]", "", exercise_name.casefold())
            if not plan_name:
                return None
            candidates = [(len(name), row) for name, row in normalized
                          if name and (plan_name in name or name in plan_name)]
            if not candidates:
                return None
            longest = max(length for length, _row in candidates)
            matches = [row for length, row in candidates if length == longest]
            if len(matches) > 1:
                names = ", ".join(str(row["name"]) for row in matches)
                raise MacroError(
                    f"Ambiguous workout library exercise {exercise_name!r}: {names}. "
                    "Use a clearer exercise name."
                )
            return matches[0]

        unknown = []
        # Match against the ADAPTED (canonical) days — the coach's raw shape was translated above.
        for day_name, day in plan["days"].items():
            for exercise in day["exercises"]:
                # Prefer the library name match; the id path was a failure mode
                # (the coach often omits ids). Name matching is exact → longest → ambiguous-reject.
                match = match_library_name(exercise["name"])
                if match is None:
                    unknown.append(exercise["name"])
                else:
                    exercise["name"] = str(match["name"])
        if unknown: raise MacroError("Plan exercises must come from workout_library: " + ", ".join(unknown))
        return {"plan": await store_client().put_workout_plan(plan)}
    async def library_tool(args):
        rows = await store_client().fetch_workout_library()
        return search_workout_library(rows, str(args["query"]))
    async def lookup_food_tool(args):
        current_user_id()  # fail closed: only run inside an authenticated tenant context
        found = await food_lookup.resolve_food(str(args["query"]))
        if not found:
            return {"result": "not found",
                    "guidance": "No free-database match. Ask for the nutrition "
                                "label or portion; a clearly flagged estimate is "
                                "the last resort."}
        return found
    async def readiness_tool(_args):
        workouts, plan, integration = await asyncio.gather(
            store_client().fetch_workouts(), store_client().fetch_workout_plan(),
            store_client().fetch_integration("whoop"),
        )
        context: dict[str, Any] = {"workouts": workouts[:10], "plan": plan, "source": "rotation"}
        if not integration or not integration.get("access_token"):
            return context
        try:
            headers = {"Authorization": f"Bearer {integration['access_token']}"}
            async with httpx.AsyncClient(timeout=12.0) as http:
                recovery, sleep = await asyncio.gather(
                    http.get("https://api.prod.whoop.com/developer/v2/recovery", headers=headers, params={"limit": 1}),
                    http.get("https://api.prod.whoop.com/developer/v2/activity/sleep", headers=headers, params={"limit": 1}),
                )
            recovery.raise_for_status(); sleep.raise_for_status()
            context.update(source="whoop", recovery=recovery.json(), sleep=sleep.json())
        except (httpx.HTTPError, ValueError):
            context["whoop_status"] = "temporarily_unavailable"
        return context
    return {"set_display_name": set_display_name_tool, "set_metrics": set_metrics_tool,
            "get_metrics": get_metrics_tool,
            "request_metrics_form": request_metrics_form_tool,
            "get_today": get_today_tool, "get_day": get_day_tool,
            "get_range_summary": get_range_tool, "lookup_food": lookup_food_tool,
            "log_meal": log_meal_tool,
            "log_preset": log_preset_tool, "save_preset": save_preset_tool,
            "undo_last_meal": undo_tool, "get_targets": get_targets_tool,
            "set_targets": set_targets_tool, "log_workout": log_workout_tool,
            "get_recent_workouts": recent_workouts_tool, "get_workout_plan": get_plan_tool,
            "get_today_session": get_today_session_tool,
            "complete_today_session": complete_today_session_tool,
            "set_workout_plan": set_plan_tool, "get_library": library_tool,
            "get_readiness": readiness_tool}


@api_route("/api/chat", methods=["POST"])
async def api_chat(request: Request) -> Any:
    body = await _json_body(request)
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise MacroError("message is required")
    if len(message.strip()) > 1000:
        raise MacroError("message must be 1000 characters or fewer")

    client = store_client()
    day_start, _ = domain.effective_day_window()
    history = await client.fetch_chat_messages_since(day_start, 20)
    # Persist the user turn before either model call so in-flight requests already
    # count toward the daily cap and a failed loop still consumes quota. The
    # store enforces the cap atomically (per-user advisory lock around
    # count+insert), so concurrent turns at 49 cannot both land. On failure we
    # append a synthetic assistant reply so history keeps alternating roles;
    # run_agent additionally merges same-role rows as defense in depth.
    text = message.strip()
    structured_metrics = body.get("metrics")
    validated_metrics = None
    agent_message = text
    if structured_metrics is not None:
        if not isinstance(structured_metrics, dict):
            raise MacroError("metrics must be an object")
        validated_metrics = domain.validate_metrics(structured_metrics)
        saved_summary = ", ".join(
            f"{key}={value}" for key, value in validated_metrics.items()
        )
        agent_message = f"{text}\n\nMetrics stored this turn: {saved_summary}. Do not call set_metrics."
    try:
        await client.insert_user_chat_message(agent_message, daily_cap=50)
    except ChatQuotaExceeded:
        return {"error": "You've reached today's coach limit. Ask Matt to raise it."}, 429

    if validated_metrics is not None:
        await client.put_metrics(validated_metrics)

    if structured_metrics is not None:
        raw_items = []
    else:
        try:
            parsed_food = await parse_chat_message(text)
            raw_items, _ = parsed_food[:2]
            presets = parsed_food[2] if len(parsed_food) > 2 else await fetch_presets()
        except Exception:
            logger.exception("Food parser failed; falling back to coach")
            raw_items = []
    if raw_items:
        try:
            requested_day = body.get("date")
            day = domain.parse_date(requested_day) if requested_day is not None else domain.effective_date()
            validated: list[tuple[str, dict[str, float], str, Any, Any, str, str, str]] = []
            for raw in raw_items[:10]:
                if not isinstance(raw, dict):
                    raise MacroError("I couldn't understand one of those food items. Please rephrase it.")
                clean_name = domain.validate_name(str(raw.get("name", "")))
                macros = domain.validate_macros(
                    raw.get("calories"), raw.get("protein"), raw.get("carbs"),
                    raw.get("fat"), raw.get("fiber", 0)
                )
                meal_name = domain.normalize_meal(raw.get("meal"))
                basis = str(raw.get("basis") or "").strip().casefold()
                sourced_from = str(raw.get("sourced_from") or "estimate").strip().casefold()
                note = str(raw.get("note") or "ESTIMATE").strip()
                validated.append(
                    (clean_name, macros, meal_name, raw.get("grams"), raw.get("quantity"),
                     basis, sourced_from, note)
                )

            logged: list[dict[str, Any]] = []
            preset_by_name = {
                _normalized_food_name(str(preset["name"])): preset
                for preset in presets
            }
            known_by_name = {
                _normalized_food_name(name): name for name in KNOWN_CHAT_FOOD_NAMES
            }
            # The parser chooses a route, but only a real exact preset/known
            # record can mint one of those source labels.
            preset_gram_lookups_left = 3
            for clean_name, macros, meal_name, grams, quantity, basis, sourced_from, note in validated:
                normalized_name = _normalized_food_name(clean_name)
                preset = preset_by_name.get(normalized_name)
                known_name = known_by_name.get(normalized_name)
                if preset is not None:
                    preset_macros = domain.validate_macros(
                        *(preset.get(key) for key in domain.MACRO_KEYS)
                    )
                    try:
                        stated_grams = float(grams)
                    except (TypeError, ValueError):
                        stated_grams = None
                    if stated_grams is not None and stated_grams > 0:
                        try:
                            serving_weight = float(preset.get("serving_weight_g"))
                        except (TypeError, ValueError):
                            serving_weight = None
                        if serving_weight is not None and serving_weight > 0:
                            servings = stated_grams / serving_weight
                            macros = domain.validate_macros(
                                *(round(preset_macros[key] * servings, 2)
                                  for key in domain.MACRO_KEYS)
                            )
                            source = f'Meal Preset: {preset["name"]}'
                        else:
                            upgraded = None
                            if preset_gram_lookups_left > 0:
                                preset_gram_lookups_left -= 1
                                upgraded = await _upgrade_estimate(clean_name, stated_grams)
                            if upgraded is not None:
                                macros, source = upgraded
                            else:
                                source = "ESTIMATE"
                    else:
                        try:
                            servings = float(quantity)
                        except (TypeError, ValueError):
                            servings = 1
                        if servings <= 0:
                            servings = 1
                        macros = domain.validate_macros(
                            *(round(preset_macros[key] * servings, 2)
                              for key in domain.MACRO_KEYS)
                        )
                        source = f'Meal Preset: {preset["name"]}'
                elif known_name is not None:
                    # The parser already returned the known food's final macros
                    # for the user's exact portion. The registry only proves the
                    # source label; it does not redo language/portion math.
                    source = f"Known food: {known_name}"
                else:
                    source = note if sourced_from == "lookup" else "ESTIMATE"
                result = await write_meal(
                    clean_name, macros["calories"], macros["protein"], macros["carbs"],
                    macros["fat"], source, meal_name, day.isoformat(), allow_estimate=True,
                    fiber=macros["fiber"],
                )
                logged.append(result["logged"])

            current = await day_payload(day, ensure=logged)
            names = ", ".join(f'{item["name"]} {item["calories"]:g} kcal ✓' for item in logged)
            totals = current["totals"]
            targets = current["targets"]
            reply = f'Logged {len(logged)} item{"s" if len(logged) != 1 else ""} — {names}. Total now {totals["calories"]:g}/{targets["calories"]:g} kcal.'
            await client.insert_chat_message("assistant", reply)
            return {"reply": reply, "logged": logged, "totals": totals, "widget": None}
        except Exception:
            try:
                await client.insert_chat_message(
                    "assistant", "Sorry, I couldn't log that. Try again."
                )
            except Exception:
                pass  # The food-path error is the one worth surfacing.
            raise

    plan, has_targets = await asyncio.gather(
        client.fetch_workout_plan(), client.has_macro_targets()
    )
    onboarding = plan is None or not has_targets
    async def record_usage(usage: Mapping[str, Any]) -> None:
        await client.insert_coach_usage(
            str(usage["model"]), int(usage["input_tokens"]), int(usage["output_tokens"])
        )

    handlers = _coach_tool_handlers()
    if structured_metrics is not None:
        handlers.pop("set_metrics")
    try:
        reply, tool_results = await run_agent(
            history=[{"role": row["role"], "content": row["content"]} for row in history],
            message=agent_message, onboarding=onboarding,
            handlers=handlers, record_usage=record_usage,
            max_rounds=12, max_tool_calls=24,
        )
    except Exception:
        try:
            await client.insert_chat_message(
                "assistant", "Sorry, I couldn't reach the coach. Try again."
            )
        except Exception:
            pass  # The loop's error is the one worth surfacing.
        raise
    await client.insert_chat_message("assistant", reply, tool_results or None)
    current = await day_payload(domain.effective_date())
    widget = ({"type": "metrics_form", "fields": [
        "height_cm", "weight_kg", "goal_weight_kg", "age", "activity_level"
    ]} if any(result.get("tool") == "request_metrics_form" and result.get("ok")
              for result in tool_results) else None)
    if widget is None and onboarding and reply_requests_metrics(reply):
        saved_metrics = await client.get_metrics()
        if not saved_metrics:
            widget = {"type": "metrics_form", "fields": [
                "height_cm", "weight_kg", "goal_weight_kg", "age", "activity_level"
            ]}
    if widget is None:
        widget = exercise_card_widget(reply, tool_results)
    return {"reply": reply, "logged": [], "totals": current["totals"],
            "has_plan": plan is not None or await client.fetch_workout_plan() is not None,
            "has_targets": has_targets or await client.has_macro_targets(),
            "widget": widget}


@api_route("/api/chat/history", methods=["GET"])


async def api_chat_history(request: Request) -> Any:
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        raise MacroError("limit must be an integer") from None
    if not 1 <= limit <= 100:
        raise MacroError("limit must be between 1 and 100")
    return {"messages": await store_client().fetch_chat_messages(limit)}


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


@api_route("/api/plan", methods=["POST"])
async def api_put_plan(request: Request) -> Any:
    body = await _json_body(request)
    try:
        plan = domain.validate_workout_plan(body)
    except ValueError as exc:
        raise MacroError(str(exc)) from None
    library = await store_client().fetch_workout_library()
    known = {item["name"].casefold() for item in library}
    referenced_names = {
        name
        for day in plan["days"].values()
        for exercise in day["exercises"]
        for name in [exercise["name"], *(exercise.get("swaps") or [])]
    }
    unknown = sorted(
        (name for name in referenced_names if name.casefold() not in known),
        key=str.casefold,
    )
    stored = await store_client().put_workout_plan(plan)
    payload: dict[str, Any] = {"plan": stored}
    if unknown:
        payload["warnings"] = [
            "Exercise is not in workout_library: " + name for name in unknown
        ]
    return payload


@api_route("/api/library", methods=["GET"])
async def api_library(request: Request | None = None) -> Any:
    query = ""
    if request is not None:
        query = (request.query_params.get("q") or "").strip()
    rows = await store_client().fetch_workout_library()
    if query:
        # Server-side search — the client only gets matches, not all 1,542 rows.
        return {"exercises": search_workout_library(rows, query)}
    return {"exercises": rows}


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
    # Build the Starlette app with the MCP auth gate (C1 security fix) and
    # serve via uvicorn — the raw /mcp protocol requires a bearer token.
    import uvicorn

    from starlette.middleware import Middleware as _Middleware

    app = mcp.http_app(
        middleware=[_Middleware(MCPAuthMiddleware)],
        transport="http",
    )
    uvicorn.run(app, host="0.0.0.0", port=CONFIG.port)


if __name__ == "__main__":
    main()
