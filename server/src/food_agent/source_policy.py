"""Provider permission metadata, separate from reusable nutrient evidence.

A consumed ledger entry may retain its consumed amounts. A pending draft is
not a consumed ledger: restricted panels must be refreshed and checked against
the displayed evidence before a commit. Unknown retention policies fail closed.
"""
from collections.abc import Mapping
from typing import Any

NUTRIENTS = {"panel", "nutrients", "macros_per_serving", "macros_per_100g",
             "calories", "protein", "carbs", "fat", "fiber"}


def policy_only(value: Any) -> Any:
    """Retain attribution/retention recursively, never nested nutrient panels."""
    if isinstance(value, Mapping):
        return {k: policy_only(v) for k, v in value.items() if k not in NUTRIENTS}
    if isinstance(value, (list, tuple)):
        return [policy_only(v) for v in value]
    return value


def permits_cache(value: Any) -> bool:
    if isinstance(value, Mapping):
        if "cache_allowed" in value and value.get("cache_allowed") is not True:
            return False
        if str(value.get("provider", "")).casefold() == "fatsecret" and value.get("cache_allowed") is not True:
            return False
        # No generic TTL interpretation is safe: keep the policy and refresh.
        if any(("retention" in k or k in ("expires_at", "cache_expires_at")) and v is not None
               for k, v in value.items()):
            return False
        return all(permits_cache(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(permits_cache(v) for v in value)
    return True


def attribution_texts(value):
    """Source-required credit, including component providers."""
    if isinstance(value, Mapping):
        text = value.get("attribution_text")
        if isinstance(text, str) and text.strip():
            yield text.strip()
        for key, nested in value.items():
            if key != "attribution_text":
                yield from attribution_texts(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from attribution_texts(nested)


def persistable_state(item):
    state = item.model_dump(mode="json")
    food = state.get("resolved")
    if food and (not food["cache_allowed"] or not permits_cache(food)):
        food["panel"] = {}
    return state
