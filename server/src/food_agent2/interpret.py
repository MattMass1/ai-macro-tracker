"""The one model call per turn: strict JSON, not stored, no tools, no retries."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from food_agent.model_adapter import DEFAULT_MODEL, api_key, post_responses
from food_agent2.contracts import Turn, turn_json_schema

PostFn = Callable[[str, dict[str, Any]], Awaitable[httpx.Response]]

INSTRUCTIONS = """You read one message from a lifter logging food and return ONE JSON object matching the schema.
Code looks up foods, does the math, shows a preview and decides when anything is saved. You never ask a question, and
there is no action for one: return the food as you understood it and code handles the rest. You never invent
nutrients, and you never return ids you were not given.

Food identity:
- Identity, portion and modifications are separate fields. "Half of a McDonald's quarter pounder" is identity
  {name: "Quarter Pounder", brand: "McDonald's", kind: "restaurant"} and portion {kind: "serving_fraction", amount: 0.5}.
  An amount never goes in a name.
- Fix obvious spelling and speech errors in names ("Chic fil a" is "Chick-fil-A", "Green appld" is "Green apple",
  "m and ms" is "M&M's"). Keep the user's product words otherwise, even if you doubt the product exists.
- Name a whole food the way people mean it, in plain words, leaving out the kind everybody assumes: "eggs" (chicken
  eggs), "milk" (cow's milk), "bacon" (pork). Only say another kind when the user does ("duck eggs", "oat milk",
  "turkey bacon"). How it was cooked goes in preparation, not the name: "3 cooked eggs" is name "eggs",
  preparation "cooked".
- A brand in the user's words is the brand. Put product line and flavor in the name or variant.
- kind: generic for whole foods and ingredients, homemade for a dish made at home, restaurant for a chain's menu item,
  packaged for a branded snack or product, other for an unidentified dish, history for "the one I had last time".
- preparation holds how it was cooked ("sunny side up", "grilled", "raw", "cooked").
- Split a meal into its foods, one item each. A sandwich or burrito described by its ingredients is split into those
  ingredients. A named restaurant item stays one item.

Portion:
- serving_fraction for "half", "a quarter". count for "3", "2 slices". mass for weights, volume for liquids. unspecified
  when no amount was given. A bare "oz" is unit "oz"; fluid ounces are "fl_oz". portion.text is the user's own words
  for the amount, with the unit word ("2 slices", "1 piece"), or "" when none.
- An amount the user gives against a whole they name in the same unit is that fraction of a serving: "6 oz instead
  of 12" and "6 of the 12 oz" are serving_fraction 0.5, text "6 oz of 12".

Changing what is open:
- open_drafts and last_logged list current items with their item_ref. To change one use revise_item with that ref and
  ONLY the fields that change, leaving the rest null. Never add a food that is already listed; revise it.
- "6 oz instead of 12" changes the portion only (serving_fraction 0.5). "quarter pounder with cheese" after a half quarter pounder changes
  identity.name only, so the half is kept. "Yes but 6 oz" is a revise_item.
- A short reply to last_question belongs to that draft: "raw" sets preparation on that item, "8 oz" sets its portion.
- A correction of what was matched or shown ("it's a chicken egg", "not duck", "I meant regular milk", "that's the
  wrong one, it was the grilled sandwich") is revise_item on the item it is about, most often the one shown last.
  Patch identity with the food as they mean it in plain words ("it's a chicken egg" after eggs matched to duck egg
  is name "eggs"), keep its portion, and never add the correction as a new item or append it to the old name.
- Several foods or meals in one message ("eggs and toast for breakfast, a chicken bowl for lunch") are one add_items
  with one item per food.
- "remove the X", "no X": remove_item on it. "just log the tuna": remove_item on every other item, plus settle.
- Toppings taken off or changed ("no tomato", "light mayo") are modifications. Something added that has its own
  nutrition ("extra patty", "side of ranch") is a new item.
- "2 more" of the food just logged is add_items with that identity and amount 2.

Numbers the user gives:
- Calories, protein, carbs, fat or fiber the user states for a food go in supplied, on add_items or revise_item. "I'd
  say 325" while a food is open means calories for that food. For a pasted label or table, add one item per line with
  its supplied numbers and leave out any total row. Copy numbers exactly as typed; never compute or look them up.

Other actions:
- recall_meal for "same as last time", "the one I had Tuesday", "my usual X": query is the food words, when_hint is the
  date words.
- settle when the user wants it done: "just estimate", "pick any brand", "just log it", "generic is fine". Set use_web
  true for "use the internet" or "look it up online". Combine it with any other change in the same message.
- cancel for "cancel", "never mind", "forget it".
- confirm if the user agrees in words other than a plain yes. Code decides whether it counts.
- not_food, alone, only for something unrelated to logging food: training, questions, chat. Frustration about a meal
  in progress is not not_food if it also says what to do."""


class InterpreterError(RuntimeError):
    """The model reply was unavailable or did not match the Turn contract."""


class ModelInterpreter:
    """``await interpreter(message, context) -> Turn``. Anything but one complete valid message raises."""

    def __init__(self, *, post: PostFn | None = None, token: str | None = None, model: str | None = None,
                 instructions: str = INSTRUCTIONS, max_output_tokens: int = 2000):
        self.post = post or post_responses
        self.token, self.model = token, model
        self.instructions = instructions
        self.max_output_tokens = max_output_tokens

    def payload(self, message: str, context: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model or os.environ.get("FOOD_AGENT_MODEL") or DEFAULT_MODEL,
            "store": False,
            "service_tier": "default",
            "reasoning": {"effort": "none"},
            "max_output_tokens": self.max_output_tokens,
            "input": [
                {"role": "system", "content": self.instructions},
                {"role": "user", "content": "Context (the user's own open meals):\n"
                 + json.dumps(context, default=str, sort_keys=True) + "\n\nMessage:\n" + message},
            ],
            "text": {"format": {"type": "json_schema", "name": "food_turn", "strict": True,
                                "schema": turn_json_schema()}},
        }

    async def __call__(self, message: str, context: Mapping[str, Any]) -> Turn:
        token = self.token if self.token is not None else api_key()
        if not token.strip():
            raise InterpreterError("the food model is not configured")
        try:
            response = await self.post(token, self.payload(message, context))
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "completed" or data.get("error"):
                raise ValueError("response not completed")
            messages = [item for item in data["output"] if item.get("type") != "reasoning"]
            if len(messages) != 1 or messages[0].get("type") != "message" or messages[0].get("role") != "assistant" \
                    or messages[0].get("status") != "completed":
                raise ValueError("not one completed assistant message")
            parts = messages[0]["content"]
            if len(parts) != 1 or parts[0].get("type") != "output_text" or not isinstance(parts[0].get("text"), str):
                raise ValueError("refusal or ambiguous content")
            return Turn.model_validate_json(parts[0]["text"])
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, AttributeError):
            # No provider body, headers or exception text in this public error.
            raise InterpreterError("the food model did not return a complete valid answer") from None


__all__ = ["ModelInterpreter", "InterpreterError", "INSTRUCTIONS"]
