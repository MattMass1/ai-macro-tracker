"""The agentic step for named products: which of these real menu records did the user mean?

Two places use it. A named product (a chain's menu item): code keeps candidates under the
same brand that are not a different food, and when none is a clear word match the model
infers which one was meant from what it knows about the chain ("personal" pizza is the
chain's smallest). A whole food: when more than one database row could be it, the model
chooses with everyday defaults (eggs are chicken eggs). Code's hard guards (another animal,
flavor, variant, a stated size or raw/cooked) have already removed every candidate that is
a different food, and the model can only return an id it was given. When no candidate is the
item, it may give up to two search phrases, and code searches once more (named products).

One strict JSON call, not stored, no tools. Any failure is "no pick", and the caller falls
back to the code ranking.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from food_agent.model_adapter import DEFAULT_MODEL, api_key, post_responses

PostFn = Callable[[str, dict[str, Any]], Awaitable[httpx.Response]]

MAX_CANDIDATES = 12
MAX_SEARCHES = 2
FLAG = "FOOD_AGENT2_MENU_PICK"

INSTRUCTIONS = """You match what a person said they ate to one record in a nutrition database.
You get their words (food or menu item, brand if any, variant, preparation, and the amount words they used) and
numbered candidate records. Return the id of the candidate that IS the food they mean, the way a friend who heard
them would understand it.

Everyday defaults when they did not say otherwise:
- Eggs are chicken eggs, milk is cow's milk, bacon is pork bacon, rice is white rice, bread is regular sliced bread.
- The plain, as-usually-eaten form: no added sauce, breading, sugar or flavor they did not mention.
- "Cooked" with no method is the most ordinary method for that food (eggs: boiled or scrambled, not a recipe).
For a brand's menu (records under a brand), use what you know about that menu:
- Size names and diameters: a "personal" or "individual" pizza is that chain's smallest (at Domino's, the Small 10");
  "10 inch" is the 10" size; "kids" is the kids size.
- With no topping or flavor named, the plain version (cheese pizza, original recipe, regular crust).
- Nicknames and spoken forms of menu names.
Amount words ("whole pie", "half", "3", "2 slices", "a box") describe how much, not which food: ignore them for the pick.

Never pick a different food: not another animal (a duck egg is not an egg), size, flavor, topping, protein or
product line than they stated or plainly meant, and not a side or drink for a main.
If several candidates are the same food and differ only in how the record is written, pick the one whose name says
the most of what they said.
If no candidate is the food, set pick to null and give up to two short search phrases that would find it (in a
brand's own menu words for a menu item). Otherwise search is empty."""

_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["pick", "search"],
    "properties": {
        "pick": {"type": ["string", "null"]},
        "search": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_SEARCHES},
    },
}


@dataclass(frozen=True)
class Pick:
    food_id: str | None
    search: tuple[str, ...] = ()


NO_PICK = Pick(None)


def enabled() -> bool:
    """On unless switched off with ``FOOD_AGENT2_MENU_PICK=false``."""
    return os.environ.get(FLAG, "true").strip().casefold() not in ("false", "0", "off", "no")


class MenuPicker:
    """``await picker(said, candidates) -> Pick``. ``candidates`` are ``{"id", "name", "brand"}``."""

    def __init__(self, *, post: PostFn | None = None, token: str | None = None, model: str | None = None):
        self.post = post or post_responses
        self.token, self.model = token, model

    def payload(self, said: Mapping[str, Any], candidates: Sequence[Mapping[str, str]]) -> dict[str, Any]:
        listing = [{"id": str(c["id"]), "name": str(c["name"])[:160], "brand": str(c.get("brand") or "")[:80]}
                   for c in candidates[:MAX_CANDIDATES]]
        return {
            "model": self.model or os.environ.get("FOOD_AGENT_MODEL") or DEFAULT_MODEL,
            "store": False,
            "service_tier": "default",
            "reasoning": {"effort": "none"},
            "max_output_tokens": 300,
            "input": [
                {"role": "system", "content": INSTRUCTIONS},
                {"role": "user", "content": "They said:\n" + json.dumps(dict(said), sort_keys=True)
                 + "\n\nCandidates:\n" + json.dumps(listing)},
            ],
            "text": {"format": {"type": "json_schema", "name": "menu_pick", "strict": True, "schema": _SCHEMA}},
        }

    async def __call__(self, said: Mapping[str, Any], candidates: Sequence[Mapping[str, str]]) -> Pick:
        if not candidates and not said:
            return NO_PICK
        token = self.token if self.token is not None else api_key()
        if not token.strip():
            return NO_PICK
        try:
            response = await self.post(token, self.payload(said, candidates))
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "completed" or data.get("error"):
                return NO_PICK
            texts = [part.get("text") for item in data["output"] if item.get("type") == "message"
                     for part in item.get("content") or [] if part.get("type") == "output_text"]
            if len(texts) != 1 or not isinstance(texts[0], str):
                return NO_PICK
            raw = json.loads(texts[0])
        except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
            return NO_PICK
        return parse(raw, candidates)


def parse(raw: Any, candidates: Sequence[Mapping[str, str]]) -> Pick:
    """Only an id that was offered counts. Search phrases are short plain text."""
    if not isinstance(raw, Mapping):
        return NO_PICK
    offered = {str(c["id"]) for c in candidates}
    pick = raw.get("pick")
    food_id = str(pick) if isinstance(pick, str) and str(pick) in offered else None
    search = tuple(" ".join(str(text).split())[:80] for text in (raw.get("search") or [])[:MAX_SEARCHES]
                   if isinstance(text, str) and text.strip()) if food_id is None else ()
    return Pick(food_id, search)


__all__ = ["MenuPicker", "Pick", "NO_PICK", "parse", "enabled", "INSTRUCTIONS", "MAX_CANDIDATES", "FLAG"]
