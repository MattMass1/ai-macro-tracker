"""Application-owned, bounded execution of managed GPT-Live function calls."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from auth import bind_user, reset_user
from coach import TOOLS, _schema, S
from domain import MacroError

READ_TOOLS = {"get_today", "get_targets", "get_today_session", "get_workout_plan",
              "get_recent_workouts", "get_library", "lookup_food"}
WRITE_TOOLS = {"log_meal", "log_preset", "log_workout", "replace_today_exercise", "update_today_workout", "complete_today_session"}


def live_tool_definitions() -> list[dict[str, Any]]:
    """Expose only supported voice operations, with explicit weight units."""
    definitions = []
    for original in TOOLS:
        if original["name"] not in READ_TOOLS | WRITE_TOOLS:
            continue
        tool = json.loads(json.dumps(original))
        if tool["name"] == "log_workout":
            tool["input_schema"]["properties"].pop("muscle_group")
            tool["input_schema"]["required"].remove("muscle_group")
            tool["input_schema"]["properties"]["weight_unit"] = {"type": "string", "enum": ["lb", "kg", "bodyweight"]}
            tool["input_schema"]["required"].append("weight_unit")
            tool["input_schema"]["properties"]["sets"].update(minItems=1, maxItems=4)
            tool["description"] = "Log completed sets today. Include EACH set's actual weight and reps. Each call accepts up to 4 sets; silently split more sets into successive calls without repeating any saved set, then confirm the total once. Ask for missing weights or units; bodyweight requires weight=0. Never turn a proposed workout into completed sets."
        if tool["name"] == "log_meal":
            tool["description"] = "Log one meal eaten today using verified food lookup or supplied nutrition label macros and its real source. Sum components eaten together into one entry. Estimates are rejected."
        definitions.append(tool)
    definitions.append(_schema("replace_today_exercise", "Replace exactly one exercise in today's saved workout only, keeping its sets/reps/rest and the permanent routine. Read get_today_session and get_library first. old_exercise is removed, new_exercise replaces it. Ask if direction is ambiguous.", {"old_exercise": S, "new_exercise": S}, ("old_exercise", "new_exercise")))
    definitions.append(_schema("update_today_workout", "Update ONLY today's planned exercises or their prescribed sets/reps/rest, including explicitly requested additions/removals. FIRST read get_today_session and copy its session_revision. Preserve every exercise and prescription the user did not ask to change. New names must come from get_library. Never change completed workout logs or the permanent routine. An empty list is only for an explicit request to clear today's planned exercises. If the snapshot is stale, read again and reapply the user's minimal edit.", {
        "session_revision": {"type": "string", "minLength": 64, "maxLength": 64},
        "exercises": {"type": "array", "maxItems": 8, "items": {"type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 120},
                           "sets": {"type": "integer", "minimum": 1, "maximum": 10},
                           "reps": {"type": "string", "minLength": 1, "maxLength": 40},
                           "rest_sec": {"type": "integer", "minimum": 0, "maximum": 3600}},
            "required": ["name", "sets", "reps", "rest_sec"]}},
    }, ("session_revision", "exercises")))
    return [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["input_schema"], "strict": False} for t in definitions]


def _tenant_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any("".join(c for c in str(k).casefold() if c.isalnum()) == "userid" or _tenant_field(v) for k, v in value.items())
    return isinstance(value, list) and any(_tenant_field(v) for v in value)


class LiveToolSession:
    """Collect completed Responses calls, execute serially and deduplicate IDs.

    Only the ordered server worker calls handle. Caches live for this socket;
    limits fail closed rather than evicting IDs and permitting duplicate writes.
    """
    def __init__(self, *, user_id, handlers, send, changed, max_calls=128, timeout=30):
        self.user_id, self.handlers, self.send, self.changed = user_id, handlers, send, changed
        self.max_calls, self.timeout = max_calls, timeout
        self.responses: dict[tuple[str, str], dict[str, Any]] = {}
        self.active: dict[str, str] = {}
        self.results: dict[str, tuple[str, str]] = {}

    async def handle(self, envelope: Mapping[str, Any]) -> None:
        """Handle only completed function items nested in response.event."""
        if envelope.get("type") != "response.event":
            return
        delegation = envelope.get("delegation_id")
        event = envelope.get("event")
        if not isinstance(delegation, str) or not delegation or len(delegation) > 256 or not isinstance(event, Mapping):
            return
        kind = event.get("type")
        response = event.get("response") or {}
        if kind == "response.created":
            response_id = response.get("id")
            if not isinstance(response_id, str) or not response_id or len(response_id) > 256:
                return
            key = (delegation, response_id)
            if key not in self.responses:
                if len(self.responses) >= self.max_calls:
                    raise RuntimeError("Voice response limit reached")
                self.responses[key] = {"calls": {}, "done": False}
            self.active[delegation] = response_id
            return
        response_id = response.get("id") or self.active.get(delegation)
        state = self.responses.get((delegation, response_id))
        if state is None or state["done"]:
            return
        if kind == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, Mapping) or item.get("type") != "function_call":
                return
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
                return
            if len(state["calls"]) >= 16 and call_id not in state["calls"]:
                raise RuntimeError("Voice tool limit reached")
            prior = state["calls"].get(call_id)
            if prior is not None and prior != item:
                raise RuntimeError("Conflicting voice tool call")
            state["calls"][call_id] = dict(item)
        elif kind in {"response.failed", "response.incomplete", "response.cancelled"}:
            state["done"] = True
            state["calls"].clear()
        elif kind == "response.completed":
            state["done"] = True
            for call_id, item in state["calls"].items():
                fingerprint = json.dumps([item.get("name"), item.get("arguments")], sort_keys=True)
                previous = self.results.get(call_id)
                if previous is not None:
                    if previous[0] != fingerprint:
                        raise RuntimeError("Conflicting voice tool call")
                    output = previous[1]
                else:
                    if len(self.results) >= self.max_calls:
                        raise RuntimeError("Voice tool limit reached")
                    # Reserve before awaiting a handler. A failing write is not retried.
                    output = json.dumps({"error": "Action outcome is uncertain. Read saved data before retrying."})
                    self.results[call_id] = (fingerprint, output)
                    token = bind_user(self.user_id)
                    try:
                        args = json.loads(item.get("arguments", ""))
                        if not isinstance(args, dict) or _tenant_field(args):
                            raise MacroError("Invalid tool arguments. User identifiers are not accepted.")
                        name = item.get("name")
                        handler = self.handlers.get(name)
                        if handler is None:
                            raise MacroError("This action is not available in voice.")
                        async with asyncio.timeout(self.timeout):
                            result = await handler(args)
                        output = json.dumps(result, default=str, separators=(",", ":"))
                        if name in WRITE_TOOLS and not (isinstance(result, Mapping) and result.get("error")):
                            await self.changed()
                    except (MacroError, ValueError, KeyError, TypeError) as exc:
                        message = str(exc) if isinstance(exc, MacroError) else "Invalid action details. Ask for the missing or corrected details."
                        output = json.dumps({"error": message})
                    except Exception:
                        output = json.dumps({"error": "Action could not be confirmed. Check saved data before retrying; do not claim success."})
                    finally:
                        reset_user(token)
                    if len(output) > 48_000:
                        output = json.dumps({"error": "Result too large. Ask for a narrower lookup; check saved data before retrying a write."})
                    self.results[call_id] = (fingerprint, output)
                await self.send({"type": "response.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": output}})
            if state["calls"]:
                await self.send({"type": "response.create"})
            state["calls"].clear()
