"""Ports (Protocols + result types) shared between food agent work packages.

* :class:`FoodFinder` / :class:`FindResult`: implemented by ``find_food.py`` (WP4).
* :class:`MealRecaller` / :class:`RecallResult`: implemented by ``recall.py`` (WP5).
* :data:`Interpreter`: the model, ``async (message, context) -> AgentTurn``.

The coordinator codes against these only, so tests use fakes and never the
network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from food_agent.contracts import AgentTurn, DraftItemState, FoodIdentity, Portion, ResolvedFood


@dataclass(frozen=True)
class FindResult:
    """Outcome of one food search.

    ``note`` is a short plain remark such as "matched to McDonald's Quarter
    Pounder with Cheese"; ``question`` is ONE plain question, never a list.
    """

    status: Literal["matched", "no_match", "needs_detail"]
    food: ResolvedFood | None
    note: str | None = None
    question: str | None = None


@runtime_checkable
class FoodFinder(Protocol):
    async def find(self, identity: FoodIdentity, portion: Portion) -> FindResult: ...


@dataclass(frozen=True)
class RecallResult:
    """Outcome of searching the user's OWN saved history."""

    status: Literal["found", "not_found", "needs_detail"]
    items: tuple[DraftItemState, ...] = ()
    source_entry_id: str | None = None
    note: str | None = None
    question: str | None = None


@runtime_checkable
class MealRecaller(Protocol):
    async def recall(self, query: str, when_hint: str | None) -> RecallResult: ...


Interpreter = Callable[[str, Mapping[str, Any]], Awaitable[AgentTurn]]


__all__ = ["FindResult", "FoodFinder", "RecallResult", "MealRecaller", "Interpreter"]
