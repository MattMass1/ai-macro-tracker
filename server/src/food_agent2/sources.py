"""The live providers behind :class:`food_agent2.retrieval.Retrieval`.

Each method is a thin call into an existing adapter. Imports are lazy so that
importing this module has no ``server`` side effects, and module attributes are
read at call time so the bench's wire counters see every attempt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class Sources(Protocol):
    async def exact(self, query: str, lane: str | None) -> Mapping[str, Any] | None: ...
    async def rows(self, query: str, require_all: bool) -> Sequence[Mapping[str, Any]]: ...
    async def hits(self, query: str) -> Sequence[Mapping[str, Any]]: ...
    async def food(self, food_id: str, query: str, hit: Mapping[str, Any]) -> Mapping[str, Any] | None: ...
    async def web(self, query: str) -> Mapping[str, Any] | None: ...


class LiveSources:
    async def exact(self, query, lane):
        """Presets, the tenant catalog, curated rows and each lane's exact provider match. No web."""
        import server  # noqa: PLC0415 (lazy on purpose)

        found = await server.resolve_food(query, atomic=True, kind=lane, allow_web=False)
        return found if server._trusted_food(found) else None

    async def rows(self, query, require_all):
        import usda_lookup  # noqa: PLC0415

        return await usda_lookup.rows(query, require_all=require_all)

    async def hits(self, query):
        import food_lookup  # noqa: PLC0415

        return await food_lookup.fatsecret_candidates(query)

    async def food(self, food_id, query, hit):
        import food_lookup  # noqa: PLC0415

        return await food_lookup.fatsecret_detail(food_id, query, hit)

    async def web(self, query):
        import web_nutrition_lookup  # noqa: PLC0415

        try:
            return await web_nutrition_lookup.lookup(query)
        except web_nutrition_lookup.NutritionLookupError:
            return None


__all__ = ["Sources", "LiveSources"]
