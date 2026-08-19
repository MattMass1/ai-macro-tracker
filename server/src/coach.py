"""Anthropic-backed coach loop with bounded, application-owned tool execution."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx


class CoachProviderError(RuntimeError):
    """The coach provider was unavailable or returned an invalid response."""


ToolHandler = Callable[[Mapping[str, Any]], Awaitable[Any]]
PostMessages = Callable[[str, dict[str, Any]], Awaitable[httpx.Response]]
RecordUsage = Callable[[dict[str, Any]], Awaitable[None]]


def _schema(name: str, description: str, properties: dict[str, Any], required=()) -> dict[str, Any]:
    return {"name": name, "description": description, "input_schema": {
        "type": "object", "properties": properties, "required": list(required),
        "additionalProperties": False,
    }}


N = {"type": "number", "minimum": 0}
S = {"type": "string"}
MACROS = {key: N for key in ("calories", "protein", "carbs", "fat", "fiber")}
TOOLS = [
    _schema("get_today", "Read today's macros and meals.", {}),
    _schema("get_day", "Read a specific day.", {"date": S}, ("date",)),
    _schema("get_range_summary", "Read macro totals over an inclusive range.", {"start": S, "end": S}, ("start", "end")),
    _schema("log_meal", "Log food only with verified macros and a real source. Never estimate.", {"name": S, "meal_type": S, **MACROS, "macro_source": S}, ("name", "meal_type", *MACROS, "macro_source")),
    _schema("log_preset", "Log a saved preset.", {"preset_name": S, "servings": N, "meal": S}, ("preset_name", "servings", "meal")),
    _schema("save_preset", "Save a verified reusable meal preset. macro_source must cite where the macros came from (a nutrition label or FDA FoodData Central); placeholders like 'estimate' are rejected.", {"values": {"type": "object"}, "macro_source": S}, ("values", "macro_source")),
    _schema("undo_last_meal", "Delete the most recent meal entry today.", {}),
    _schema("get_targets", "Read current macro targets.", {}),
    _schema("set_targets", "Write macro targets.", {"values": {"type": "object", "properties": MACROS, "required": list(MACROS), "additionalProperties": False}}, ("values",)),
    _schema("log_workout", "Log one exercise and its sets.", {"exercise": S, "workout_type": S, "muscle_group": S, "sets": {"type": "array", "items": {"type": "object", "properties": {"weight": N, "reps": N}, "required": ["weight", "reps"]}}}, ("exercise", "workout_type", "muscle_group", "sets")),
    _schema("get_recent_workouts", "Read recent workout history.", {"n": {"type": "integer", "minimum": 1, "maximum": 30}}, ("n",)),
    _schema("get_workout_plan", "Read the current workout plan.", {}),
    _schema("set_workout_plan", "Validate and write a workout plan built from the library.", {"plan": {"type": "object"}}, ("plan",)),
    _schema("get_library", "Search the exercise library.", {"query": S}, ("query",)),
    _schema("get_readiness", "Read WHOOP readiness when available, otherwise rotation context.", {}),
]


def _load_persona() -> str:
    """Load the coach persona from coach_persona/SOUL.md (fallback to built-in)."""
    path = Path(__file__).resolve().parent / "coach_persona" / "SOUL.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return SYSTEM_PROMPT


SYSTEM_PROMPT = """You are Macro Coach, a concise, practical nutrition and strength coach with hands: use tools whenever reading or changing user data. Never claim a write succeeded unless its tool result says so. Never estimate food macros; ask for a label/portion or use a saved preset. Keep responses short and human.

Onboarding is active when the system context says the user has no plan or no targets. Interview them conversationally for goal, experience, days per week, equipment, and injuries/limitations. Ask only the next useful question. Once enough information is known, search the workout library, then call set_targets and set_workout_plan. Plans must use library exercises. Do not expose internal tool errors or secrets; explain the actionable part.

For training recommendations use get_readiness. Users without WHOOP still receive rotation-based recommendations. Dates use YYYY-MM-DD."""


def system_prompt(onboarding: bool) -> str:
    """Full system prompt: SOUL.md persona + onboarding context."""
    return _load_persona() + f"\n\nSystem context: onboarding={str(onboarding).lower()}."


async def post_anthropic(token: str, payload: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=45.0) as client:
        return await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": token, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=payload,
        )


async def _call_provider(post: PostMessages, token: str, payload: dict[str, Any]) -> httpx.Response:
    """POST once, retrying a single time on 429/5xx (incl. 529 overloaded) or transport failure."""
    last_exc: httpx.HTTPError | None = None
    for attempt in range(2):
        try:
            response = await post(token, payload)
        except httpx.HTTPError as exc:
            last_exc = exc
            continue
        if (response.status_code == 429 or response.status_code >= 500) and attempt == 0:
            continue
        response.raise_for_status()
        return response
    assert last_exc is not None
    raise last_exc


async def run_agent(
    *, history: list[dict[str, str]], message: str, onboarding: bool,
    handlers: Mapping[str, ToolHandler], post: PostMessages = post_anthropic,
    record_usage: RecordUsage | None = None, max_rounds: int = 8,
    max_tool_calls: int = 16,
) -> tuple[str, list[dict[str, Any]]]:
    """Run one bounded Messages API tool loop and return text plus tool audit."""
    token = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not token:
        raise CoachProviderError("The coach is not configured yet.")
    # Persisted history can contain consecutive same-role rows (e.g. a user turn
    # whose assistant reply never landed). The Messages API requires alternating
    # roles starting with user, so merge same-role neighbors and drop an
    # orphaned leading assistant row instead of failing the whole chat.
    messages: list[dict[str, Any]] = []
    for item in history:
        if item.get("role") not in {"user", "assistant"}:
            continue
        if messages and messages[-1]["role"] == item["role"]:
            messages[-1]["content"] = f"{messages[-1]['content']}\n\n{item['content']}"
        else:
            messages.append({"role": item["role"], "content": item["content"]})
    if messages and messages[0]["role"] == "assistant":
        messages.pop(0)
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = f"{messages[-1]['content']}\n\n{message}"
    else:
        messages.append({"role": "user", "content": message})
    system = system_prompt(onboarding)
    audit: list[dict[str, Any]] = []
    executed_tool_calls = 0

    for round_number in range(max_rounds + 1):
        payload = {"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"), "max_tokens": 1200,
                   "system": system, "tools": TOOLS, "messages": messages}
        try:
            response = await _call_provider(post, token, payload)
            data = response.json()
            blocks = data["content"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise CoachProviderError("The coach is having trouble connecting. Try again in a moment.") from exc
        if not isinstance(blocks, list):
            raise CoachProviderError("The coach returned an invalid response. Try again.")
        if record_usage is not None:
            usage = data.get("usage") or {}
            try:
                await record_usage({"model": str(data.get("model") or payload["model"]),
                                    "input_tokens": int(usage.get("input_tokens") or 0),
                                    "output_tokens": int(usage.get("output_tokens") or 0)})
            except Exception:
                pass  # Usage telemetry must never take down the chat itself.

        uses = [block for block in blocks if isinstance(block, dict) and block.get("type") == "tool_use"]
        if not uses:
            text = "\n".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict) and block.get("type") == "text").strip()
            if not text:
                raise CoachProviderError("The coach did not return a reply. Try again.")
            return text, audit
        if round_number >= max_rounds:
            raise CoachProviderError("The coach reached its tool limit. Please split that into a smaller request.")

        messages.append({"role": "assistant", "content": blocks})
        results = []
        for use in uses:
            # One API round may carry many tool_use blocks, so the round cap
            # alone does not bound writes — recheck before every execution.
            if executed_tool_calls >= max_tool_calls:
                raise CoachProviderError(
                    "The coach reached its tool limit. Please split that into a smaller request."
                )
            executed_tool_calls += 1
            name, tool_input = use.get("name"), use.get("input") or {}
            handler = handlers.get(str(name))
            is_error = handler is None
            if handler is None:
                result: Any = {"error": "Unknown tool"}
            else:
                try:
                    result = await handler(tool_input)
                except Exception as exc:  # Tool failures are observations, not API crashes.
                    is_error = True
                    result = {"error": str(exc)}
            audit.append({"tool": name, "input": tool_input, "ok": not is_error})
            results.append({"type": "tool_result", "tool_use_id": use.get("id"),
                            "content": json.dumps(result, default=str), "is_error": is_error})
        messages.append({"role": "user", "content": results})
    raise CoachProviderError("The coach reached its tool limit.")
