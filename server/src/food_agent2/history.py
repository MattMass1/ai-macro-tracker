"""Recall from the user's own log. One tenant-scoped query, no unrelated fallback.

Matching and date windows are ``food_agent.recall``. What changes is the ending:
this never asks "which one?" or "did you mean?". The newest match wins and the
note says which meal it was, so a wrong pick costs one correction. No match is
``not_found``, and a recent unrelated meal is never offered instead.
"""

from __future__ import annotations

from datetime import timedelta

import domain
from food_agent.ports import RecallResult
from food_agent.recall import (HistoryRecaller, _candidates, _day_label, _items, _search_terms, _window_label,
                               food_words, resolve_when)


class HistoryRecall(HistoryRecaller):
    """``HistoryRecall(store, clock=None)``; ``store`` has ``search_own_entries``. Read-only."""

    async def recall(self, query: str, when_hint: str | None) -> RecallResult:
        words = food_words(query)
        if not words:
            return RecallResult(status="not_found")
        today = domain.effective_date(self.clock())
        windows = [w for w in (resolve_when(when_hint, today) or resolve_when(query, today)) if w[0] <= today]
        since = min([today - timedelta(days=self.lookback_days)] + [low for low, _ in windows])
        rows = await self.store.search_own_entries(_search_terms(words), since=since, until=today, limit=self.limit)
        found = _candidates(rows or [], words)
        if any(candidate.quality == "exact" for candidate in found):
            found = [candidate for candidate in found if candidate.quality == "exact"]
        if not found:
            return RecallResult(status="not_found")

        missed = None
        for low, high in windows:
            inside = [candidate for candidate in found if low <= candidate.day <= high]
            if inside:
                found, missed = inside, None
                break
            missed = missed or (low, high)

        exact_name = [candidate for candidate in found if candidate.key == frozenset(words)]
        chosen = (exact_name or found)[0]  # newest first
        if missed is not None:
            note = (f"I didn't see it on {_window_label(missed)}, so this is your {chosen.name} "
                    f"from {_day_label(chosen.day)}")
        else:
            note = f"same as your {chosen.name} on {_day_label(chosen.day)}"
        return RecallResult(status="found", items=_items(chosen), source_entry_id=str(chosen.row["id"]), note=note)


__all__ = ["HistoryRecall"]
