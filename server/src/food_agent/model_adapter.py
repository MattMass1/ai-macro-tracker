"""New food interpreter: bounded, non-stored OpenAI Responses strict JSON.

Unrelated coach and lookup model routes are deliberately unchanged. No retries,
SDK, tools, partial JSON execution or credential logging.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from pydantic import ValidationError

from food_agent.contracts import AgentTurn, agent_turn_json_schema

PostFn = Callable[[str, dict[str, Any]], Awaitable[httpx.Response]]

INSTRUCTIONS = """You read one message from a lifter logging food in a macro tracker and return ONE JSON object
that matches the schema. You never return nutrient numbers, ids you were not given, or SQL. Code looks up foods,
does the math, shows the user a preview, and decides when anything is saved.

Rules:
- Food identity, portion and modifications are separate. "Half of a McDonald's quarter pounder" is identity
  {name: "Quarter Pounder", brand: "McDonald's", kind: "restaurant"} with portion
  {kind: "serving_fraction", amount: 0.5}. Never put an amount in the name.
- To change an item already in open_drafts or last_logged, use revise_item with its item_ref and ONLY the fields
  that change; leave the rest null so they are kept. "quarter pounder with cheese" after a half quarter pounder
  changes identity.name only (the half is kept). "6 oz instead of 12" changes portion only
  ({kind: "mass", amount: 6, unit: "oz"}); do not re-add the food.
- Toppings taken off or changed ("no tomato", "light mayo", "swap fries") are modifications on that item.
  Something added on top that has its own nutrition ("extra patty", "add bacon", "side of ranch") is its own
  item in add_items, so it gets looked up.
- "the one I had last time", "again", "my usual X": use recall_meal with the food words as query.
- A bare "oz" is unit "oz". Fluid ounces are "fl_oz". No amount given: portion kind "unspecified".
- If the user is clearly confirming the shown meal, you may return confirm; code decides if it counts.
- "cancel", "never mind": cancel. Unclear food: ask ONE short question. Anything that is not about food the user
  ate (training, questions, chat): not_food, alone.
- portion.text is the user's own words for the amount ("" when none)."""


class InterpreterError(RuntimeError):
    """The model reply was unavailable or did not match the AgentTurn contract."""


DEFAULT_MODEL = "gpt-6.1-terra"
RESPONSES_URL = "https://api.openai.com/v1/responses"


async def post_responses(token: str, payload: dict[str, Any]) -> httpx.Response:
    """One backend-only request; callers may wrap this seam for metering."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False, trust_env=False) as client:
        return await client.post(RESPONSES_URL, headers={"Authorization": f"Bearer {token}"}, json=payload)


def api_key() -> str:
    """Standard backend key wins; the old app token remains explicit compatibility."""
    return os.environ.get("OPENAI_API_KEY", "").strip() or os.environ.get("OPENAI_ACCESS_TOKEN", "").strip()


class ModelInterpreter:
    """Validate exactly one completed assistant message before returning an action."""

    def __init__(self, *, post: PostFn | None = None, token: str | None = None, model: str | None = None,
                 instructions: str = INSTRUCTIONS, max_completion_tokens: int = 2000):
        self.post = post or post_responses
        self.token, self.model = token, model
        self.instructions = instructions
        if not 1 <= max_completion_tokens <= 2000:
            raise ValueError("food interpreter output cap must be 1..2000")
        self.max_completion_tokens = max_completion_tokens

    def payload(self, message: str, context: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model or os.environ.get("FOOD_AGENT_MODEL") or DEFAULT_MODEL,
            "store": False,
            "service_tier": "default",
            "reasoning": {"effort": "none"},
            "max_output_tokens": self.max_completion_tokens,
            "input": [
                {"role": "system", "content": self.instructions},
                {"role": "user", "content": "Context (the user's own open meals):\n"
                 + json.dumps(context, default=str, sort_keys=True) + "\n\nMessage:\n" + message},
            ],
            "text": {"format": {"type": "json_schema", "name": "agent_turn", "strict": True,
                                "schema": agent_turn_json_schema()}},
        }

    async def __call__(self, message: str, context: Mapping[str, Any]) -> AgentTurn:
        token = self.token if self.token is not None else api_key()
        if not token.strip():
            raise InterpreterError("the food agent model is not configured")
        try:
            response = await self.post(token, self.payload(message, context))
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "completed" or data.get("error"):
                raise ValueError("response not completed")
            output = data["output"]
            # Reasoning metadata is not executable. Everything else must be the single message.
            messages = [item for item in output if item.get("type") != "reasoning"]
            if len(messages) != 1:
                raise ValueError("ambiguous output")
            item = messages[0]
            if item.get("type") != "message" or item.get("role") != "assistant" or item.get("status") != "completed":
                raise ValueError("not a completed assistant message")
            parts = item["content"]
            if len(parts) != 1 or parts[0].get("type") != "output_text" or not isinstance(parts[0].get("text"), str):
                raise ValueError("refusal or ambiguous content")
            return AgentTurn.model_validate_json(parts[0]["text"])
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            # No provider body, request headers or exception text in this public error.
            raise InterpreterError("the food agent model did not return a complete valid answer") from None


__all__ = ["ModelInterpreter", "InterpreterError", "INSTRUCTIONS", "post_responses", "DEFAULT_MODEL"]
