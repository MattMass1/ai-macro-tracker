"""OpenAI-backed coach loop with bounded, application-owned tool execution."""
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
    _schema("set_display_name", "Save what the user wants the coach to call them.", {"name": S}, ("name",)),
    _schema("set_metrics", """Save the user's structured body measurements. ALWAYS echo back in the USER's units: if they gave feet/inches or pounds, store as cm/kg (convert) but CONFIRM in their units ("5'10\", 300 lb, goal 250 lb"). Never reply in metric when the user speaks imperial. Never misread the goal: if the stated goal contradicts the direction (e.g. goal higher than current weight while they said lose fat), ASK to confirm before storing.""", {
        "height_cm": {"type": "number", "minimum": 100, "maximum": 250},
        "weight_kg": {"type": "number", "minimum": 30, "maximum": 300},
        "goal_weight_kg": {"type": "number", "minimum": 30, "maximum": 300},
        "age": {"type": "integer", "minimum": 13, "maximum": 120},
        "activity_level": {"type": "string", "minLength": 1, "maxLength": 40},
    }, ("height_cm", "weight_kg", "goal_weight_kg")),
    _schema("get_metrics", "Read the user's saved body measurements.", {}),
    _schema("request_metrics_form", "Show the client's structured height, weight, goal-weight, age, and activity form. During onboarding, CALL THIS TOOL instead of asking for weight, height, measurements, or body metrics in plain chat.", {}),
    _schema("get_today", "Read today's macros and meals.", {}),
    _schema("get_day", "Read a specific day.", {"date": S}, ("date",)),
    _schema("get_range_summary", "Read macro totals over an inclusive range.", {"start": S, "end": S}, ("start", "end")),
    _schema("lookup_food", "Look up real macros for a food in free databases (USDA FoodData Central, then OpenFoodFacts). When a food is not a saved preset or known food and macros are needed, call this FIRST, then cite the returned source string as macro_source. Returns macros per 100 g; scale to the portion eaten.", {"query": S}, ("query",)),
    _schema("log_meal", "Log food. Use a preset, known food, or lookup_food macros when available; otherwise log a clearly-flagged estimate with macro_source like 'ESTIMATE — 6 pieces sushi, typical values'. Never refuse or ask for a label — the user wants it logged.", {"name": S, "meal_type": S, **MACROS, "macro_source": S}, ("name", "meal_type", *MACROS, "macro_source")),
    _schema("log_preset", "Log a saved preset.", {"preset_name": S, "servings": N, "meal": S}, ("preset_name", "servings", "meal")),
    _schema("save_preset", "Save a verified reusable meal preset. macro_source must cite where the macros came from (a nutrition label or FDA FoodData Central); placeholders like 'estimate' are rejected.", {"values": {"type": "object"}, "macro_source": S}, ("values", "macro_source")),
    _schema("undo_last_meal", "Delete the most recent meal entry today.", {}),
    _schema("get_targets", "Read current macro targets.", {}),
    _schema("set_targets", "Write macro targets.", {"values": {"type": "object", "properties": MACROS, "required": list(MACROS), "additionalProperties": False}}, ("values",)),
    _schema("log_workout", "Log one exercise and its sets.", {"exercise": S, "workout_type": S, "muscle_group": S, "sets": {"type": "array", "items": {"type": "object", "properties": {"weight": N, "reps": N}, "required": ["weight", "reps"]}}}, ("exercise", "workout_type", "muscle_group", "sets")),
    _schema("get_recent_workouts", "Read recent workout history.", {"n": {"type": "integer", "minimum": 1, "maximum": 30}}, ("n",)),
    _schema("get_workout_plan", "Read the current workout plan.", {}),
    _schema("get_today_session", "Read today's scheduled workout session from the plan rotation: the rotation day type, its exercises, and whether it is already marked done. Use this to tell the user what today is and when the session is complete.", {}),
    _schema("complete_today_session", "Mark today's scheduled workout session as done in the rotation day-state. Call it when the user finishes today's workout so the app's Today view shows the session complete. Idempotent: completing twice just reports already done.", {}),
    _schema("set_workout_plan", """Write a workout plan built from library exercises. The server adds the version automatically. Use EXACTLY this shape:
{"plan": {"rotation": ["Push", "Pull", "Legs"], "days": {"Push": {"label": "Push Day", "exercises": [{"name": "Bench Press", "sets": 3, "reps": "8-10", "rest_sec": 90}]}, "Pull": {"label": "Pull Day", "exercises": [{"name": "Lat Pulldown", "sets": 3, "reps": "10", "rest_sec": 90}]}, "Legs": {"label": "Legs Day", "exercises": [{"name": "Squat", "sets": 3, "reps": "8", "rest_sec": 120}]}}}}
rules: rotation is an array of workout-type strings (Push/Pull/Legs/Abs/Cardio/Full Body) — one per day; days has one key per rotation entry, each with a label and exercises array; each exercise has name (from the library), sets (int 1-10), reps (string like "8-10"), rest_sec (int seconds).
EXERCISE COUNT RULE: 4-6 exercises per day is a good default; NEVER exceed 8 per day (hard cap). Do not interrogate the user about session size — just build a sensible 4-6 exercise session. The user curates their day by swapping exercises (SWAP) and adding more from the library (Add More) — that's their choice, not a question you ask. You may optionally note the range as "session_size" in the plan but it is NOT required.""", {"plan": {"type": "object"}}, ("plan",)),
    _schema("get_library", "Search the exercise library. For any exercise how-to, form, technique, video, or demo request, call this tool FIRST with the exercise name. Then mention the exact returned exercise name in a terse reply so the client can attach its video and instruction card; never say videos cannot be embedded.", {"query": S}, ("query",)),
    _schema("get_readiness", "Read WHOOP readiness when available, otherwise rotation context.", {}),
]


def _load_persona() -> str:
    """Load the coach persona from coach_persona/SOUL.md (fallback to built-in)."""
    path = Path(__file__).resolve().parent / "coach_persona" / "SOUL.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return SYSTEM_PROMPT


SYSTEM_PROMPT = """You are Macro Coach, a concise, practical nutrition and strength coach with hands: use tools whenever reading or changing user data. Never claim a write succeeded unless its tool result says so. Prefer real macros: use a saved preset or known food when one matches; otherwise call lookup_food FIRST and use its macros, citing the returned source as macro_source. For common foods with standard portions (eggs, bread, fruit, rice, chicken, etc.), lookup_food will find them — use standard portion sizes (large egg = 50g, a slice of bread = 28-30g, a medium apple = 180g) when the user doesn't give a weight. When lookup_food finds nothing, log a clearly-flagged estimate (macro_source like 'ESTIMATE — typical serving'); never ask for a label and never refuse to log. Log the whole meal — do not log one item and leave the rest unlogged; when a message lists several foods, call lookup_food/log_meal for EACH.

RULES (non-negotiable):
- MAX 2 SENTENCES PER REPLY. One short message, then stop. No exceptions.
- ONE question at a time. Never list multiple questions.
- No lectures, no explanations, no 'here's why'. No bullet lists in chat.
- EXERCISE DEMOS: When the user asks how to perform an exercise or requests a video/demo, call get_library with the exercise name FIRST. Reply in 1-2 sentences and mention the exact returned exercise name so the client attaches the video + instruction card. Never say you cannot embed or show videos; the card handles it.
- Gather information quietly, then come to conclusions. Confirm data in one line, ask the next single question, stop.
- Onboarding: ask name first (set_display_name), then goal, experience, days per week + equipment, then metrics — ONE per turn. BEFORE asking for metrics, call get_metrics; if metrics exist, never ask again. Otherwise CALL request_metrics_form and let the client render the card. Never ask for weight, height, or measurements in plain chat. If the user already typed measurements, save them with set_metrics.
- HARD COMPLETION RULE: Once you have display name, goal, experience level, days per week + equipment, and metrics (from get_metrics or set_metrics), you have ENOUGH. Do not ask anything more. Immediately search the library, then call set_targets + set_workout_plan. The plan does not need training days of the week, injuries, or additional detail.
- CRITICAL: read the conversation history. Never ask for something the user already provided in this conversation or that exists in data from get_metrics or get_workout_plan. Re-asking is a failure. If the user answers a question already asked, acknowledge it in one line and move FORWARD.

For training recommendations use get_readiness. Users without WHOOP still receive rotation-based recommendations. Dates use YYYY-MM-DD.

You are the ONLY agent. Every message is yours. Log food with lookup_food + log_meal, log workouts with log_workout, keep the rotation correct with get_today_session/complete_today_session. Never say you can't do something another system does — you ARE the system."""


def system_prompt(onboarding: bool) -> str:
    """Full system prompt: SOUL.md persona + onboarding context."""
    return _load_persona() + f"\n\nSystem context: onboarding={str(onboarding).lower()}."


async def post_openai(token: str, payload: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=90.0) as client:
        return await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
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
    handlers: Mapping[str, ToolHandler], post: PostMessages = post_openai,
    record_usage: RecordUsage | None = None, max_rounds: int = 8,
    max_tool_calls: int = 16,
) -> tuple[str, list[dict[str, Any]]]:
    """Run one bounded Chat Completions tool loop and return text plus tool audit."""
    token = os.environ.get("OPENAI_ACCESS_TOKEN", "").strip()
    if not token:
        raise CoachProviderError("The coach is not configured yet.")
    # Persisted history can contain consecutive same-role rows (e.g. a user turn
    # whose assistant reply never landed). Normalize those rows to keep the
    # provider context coherent and drop an orphaned leading assistant reply.
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
    available_tools = [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in TOOLS
        if tool["name"] in handlers
    ]
    audit: list[dict[str, Any]] = []
    executed_tool_calls = 0

    for round_number in range(max_rounds + 1):
        payload = {
            "model": os.environ.get("COACH_MODEL", "gpt-5.6-luna"),
            "max_completion_tokens": 1200,
            "reasoning_effort": "none",  # gpt-5.6-luna requires this for function tools in chat/completions
            "tools": available_tools,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        try:
            response = await _call_provider(post, token, payload)
            data = response.json()
            assistant = data["choices"][0]["message"]
        except (httpx.HTTPError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise CoachProviderError("The coach is having trouble connecting. Try again in a moment.") from exc
        if not isinstance(assistant, dict):
            raise CoachProviderError("The coach returned an invalid response. Try again.")
        if record_usage is not None:
            usage = data.get("usage") or {}
            try:
                await record_usage({"model": str(data.get("model") or payload["model"]),
                                    "input_tokens": int(usage.get("prompt_tokens") or 0),
                                    "output_tokens": int(usage.get("completion_tokens") or 0)})
            except Exception:
                pass  # Usage telemetry must never take down the chat itself.

        uses = assistant.get("tool_calls") or []
        if not isinstance(uses, list) or not all(isinstance(use, dict) for use in uses):
            raise CoachProviderError("The coach returned an invalid response. Try again.")
        if not uses:
            text = str(assistant.get("content") or "").strip()
            if not text:
                raise CoachProviderError("The coach did not return a reply. Try again.")
            return text, audit
        if round_number >= max_rounds:
            raise CoachProviderError("The coach reached its tool limit. Please split that into a smaller request.")

        messages.append({"role": "assistant", "content": assistant.get("content"),
                         "tool_calls": uses})
        for use in uses:
            # One API round may carry many tool_use blocks, so the round cap
            # alone does not bound writes — recheck before every execution.
            if executed_tool_calls >= max_tool_calls:
                raise CoachProviderError(
                    "The coach reached its tool limit. Please split that into a smaller request."
                )
            executed_tool_calls += 1
            function = use.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            raw_arguments = function.get("arguments", "{}") if isinstance(function, dict) else "{}"
            try:
                tool_input = json.loads(raw_arguments)
                if not isinstance(tool_input, dict):
                    raise ValueError("Tool arguments must be a JSON object")
            except (TypeError, ValueError) as exc:
                tool_input = {}
                is_error = True
                result: Any = {"error": f"Invalid tool arguments: {exc}"}
            else:
                handler = handlers.get(str(name))
                is_error = handler is None
                if handler is None:
                    result = {"error": "Unknown tool"}
                else:
                    try:
                        result = await handler(tool_input)
                    except Exception as exc:  # Tool failures are observations, not API crashes.
                        is_error = True
                        result = {"error": str(exc)}
            audit_entry = {"tool": name, "input": tool_input, "ok": not is_error}
            # Keep the complete result in the tool message for the model, but
            # persist only the fields needed by the exercise card.
            if name in {"get_library", "library_tool"} and not is_error:
                exercises = result.get("exercises") if isinstance(result, Mapping) else None
                if isinstance(exercises, list):
                    compact_fields = (
                        "name", "instructions", "video_url", "muscle_group",
                        "equipment", "workout_type",
                    )
                    audit_entry["result"] = {"exercises": [
                        {key: exercise.get(key) for key in compact_fields}
                        for exercise in exercises
                        if isinstance(exercise, Mapping)
                    ]}
            audit.append(audit_entry)
            messages.append({"role": "tool", "tool_call_id": use.get("id"),
                             "content": json.dumps({"result": result, "is_error": is_error},
                                                   default=str)})
    raise CoachProviderError("The coach reached its tool limit.")
