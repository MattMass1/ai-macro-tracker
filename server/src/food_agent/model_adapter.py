"""Production interpreter: one strict structured-output model call per turn.

Calls the existing ``coach.post_openai`` (Chat Completions) with
``response_format={"type": "json_schema", "strict": true}`` built from
:func:`contracts.agent_turn_json_schema`, then validates the reply with
Pydantic. Anything that does not validate (for example a ``confirmed`` flag or
nutrient numbers) raises :class:`InterpreterError`; the coordinator then makes
no change. Tests pass a fake ``post``; nothing here is exercised on the network
in this repo's test suite.
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
- "the one I had last time", "again", "my usual X": use recall_meal with the food words as query.
- A bare "oz" is unit "oz". Fluid ounces are "fl_oz". No amount given: portion kind "unspecified".
- If the user is clearly confirming the shown meal, you may return confirm; code decides if it counts.
- "cancel", "never mind": cancel. Unclear food: ask ONE short question. Anything that is not about food the user
  ate (training, questions, chat): not_food, alone.
- portion.text is the user's own words for the amount ("" when none)."""


class InterpreterError(RuntimeError):
    """The model reply was unavailable or did not match the AgentTurn contract."""


class ModelInterpreter:
    """``await ModelInterpreter()(message, context) -> AgentTurn``."""

    def __init__(self, *, post: PostFn | None = None, token: str | None = None, model: str | None = None,
                 instructions: str = INSTRUCTIONS, max_completion_tokens: int = 800):
        if post is None:
            from coach import post_openai
            post = post_openai
        self.post = post
        self.token = token
        self.model = model
        self.instructions = instructions
        self.max_completion_tokens = max_completion_tokens

    def payload(self, message: str, context: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model or os.environ.get("FOOD_AGENT_MODEL") or os.environ.get("COACH_MODEL", "gpt-5.6-luna"),
            "max_completion_tokens": self.max_completion_tokens,
            "messages": [
                {"role": "system", "content": self.instructions},
                {"role": "user", "content": "Context (the user's own open meals):\n"
                 + json.dumps(context, default=str, sort_keys=True) + "\n\nMessage:\n" + message},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "agent_turn", "strict": True, "schema": agent_turn_json_schema()}},
        }

    async def __call__(self, message: str, context: Mapping[str, Any]) -> AgentTurn:
        token = self.token if self.token is not None else os.environ.get("OPENAI_ACCESS_TOKEN", "").strip()
        if not token:
            raise InterpreterError("the food agent model is not configured")
        try:
            response = await self.post(token, self.payload(message, context))
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise InterpreterError("the food agent model did not answer") from exc
        if not isinstance(content, str):
            raise InterpreterError("the food agent model returned no content")
        try:
            return AgentTurn.model_validate_json(content)
        except ValidationError as exc:
            raise InterpreterError("the food agent model reply did not match the contract") from exc


__all__ = ["ModelInterpreter", "InterpreterError", "INSTRUCTIONS"]
