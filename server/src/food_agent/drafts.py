"""Draft persistence for the food agent: an adapter over the migration-004 store.

* :class:`AgentDraft` is the coordinator's view of one ``meal_drafts`` row. Each
  item is a :class:`DraftItemState` kept in ``meal_draft_items.resolution``
  JSONB under ``{"food_agent": "food-agent/1", "state": ...}``. Drafts without
  that marker (the older MealDrafts path) are never read or touched here.
* ``meal_drafts.pending_question`` carries exactly one of: a
  :class:`PendingConfirm` (a preview the user may confirm), a
  :class:`CommitIntent` (the user said yes; the commit for that revision is in
  flight or its outcome is unverified), or a plain-text question.
* :class:`StoreDrafts` wraps ``store.Store`` (production). :class:`MemoryDrafts`
  is an in-memory double with the same CAS, idempotency and failure points,
  for offline tests and the bench.

Nothing here ever calls ``MealDrafts.handle``/``settle`` (they auto-commit).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

import domain
from food_agent.contracts import (NUTRIENT_KEYS, PENDING_QUESTION_MAX, SCHEMA_VERSION, DraftItemState,
                                  NutrientPanel, PendingConfirm, Portion, Sha256, UuidText, sum_panels)

OPEN_STATES = ("collecting", "needs_detail", "ready")
AGENT_MARKER = "food_agent"
ACK_PREFIX = "food-agent-ack"


class DraftCommitConflict(RuntimeError):
    """The commit was refused (draft changed, wrong owner, key reused). Nothing was written."""


class DraftCommitBlocked(RuntimeError):
    """Unknown nutrients cannot be stored yet. Nothing was written."""


class DraftNotReady(ValueError):
    """The draft cannot be turned into a meal entry yet (unresolved item, unknown calories)."""


def op_key(draft_id: str, revision: int) -> str:
    """The idempotency key ``Store.commit_meal_draft`` uses for this draft revision."""
    return f"meal-draft:{draft_id}:{int(revision)}"


def ack_key(draft_id: str, revision: int) -> str:
    return f"{ACK_PREFIX}:{draft_id}:{int(revision)}"


class CommitIntent(BaseModel):
    """Written (with state ``ready``) BEFORE the commit, so an unverified outcome is durable.

    ``revision`` is the draft revision the CAS write produces, which is also the
    revision in the commit's idempotency key.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["commit"] = "commit"
    draft_id: UuidText
    revision: Annotated[int, Field(ge=1)]
    content_hash: Sha256
    request_hash: Sha256

    def to_pending_question(self) -> str:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        if len(text) > PENDING_QUESTION_MAX:
            raise ValueError("commit intent exceeds pending_question limit")
        return text

    @classmethod
    def from_pending_question(cls, text: str | None) -> "CommitIntent | None":
        if not text or not text.lstrip().startswith("{"):
            return None
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("kind") != "commit":
                return None
            return cls.model_validate_json(text)
        except ValueError:
            return None


@dataclass(frozen=True)
class AgentDraft:
    """The coordinator's view of one food-agent draft row (owner-scoped by the store)."""

    id: str
    log_date: date
    items: tuple[DraftItemState, ...] = ()
    revision: int = 1
    state: str = "collecting"
    meal_type: str | None = None
    meal_type_origin: str | None = None
    session_ref: str | None = None
    pending: str | None = None
    updated_at: datetime | None = None
    parent_draft_id: str | None = None
    amends_entry_id: str | None = None
    amends_entry_revision: int | None = None
    committed_entry_id: str | None = None
    committed_entry_revision: int | None = None
    committed_operation_key: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_amend(self) -> bool:
        return self.amends_entry_id is not None

    def pending_confirm(self) -> PendingConfirm | None:
        found = PendingConfirm.from_pending_question(self.pending)
        return found if found is not None and found.draft_id == self.id.lower() else None

    def commit_intent(self) -> CommitIntent | None:
        found = CommitIntent.from_pending_question(self.pending)
        return found if found is not None and found.draft_id == self.id.lower() else None

    def question(self) -> str | None:
        text = (self.pending or "").strip()
        return None if not text or text.startswith("{") else text

    def with_(self, **changes: Any) -> "AgentDraft":
        return replace(self, **changes)


def new_draft(*, log_date: date, session_ref: str | None, **extra: Any) -> AgentDraft:
    return AgentDraft(id=str(uuid4()), log_date=log_date, session_ref=session_ref, **extra)


# ---------------------------------------------------------------- labels


_FRACTIONS = {0.5: "half", 0.25: "a quarter", 0.75: "three quarters", 0.3333: "a third", 0.6667: "two thirds"}
_UNIT_TEXT = {"fl_oz": "fl oz"}


def _num(value: float) -> str:
    return f"{round(float(value), 2):g}"


def portion_label(portion: Portion) -> str:
    """Short words for an amount: "half", "2", "6 oz". Empty for one plain serving."""
    amount = portion.amount
    if portion.kind == "unspecified" or amount is None:
        return ""
    if portion.kind == "serving_fraction":
        if amount == 1:
            return ""
        return _FRACTIONS.get(round(amount, 4)) or f"{_num(amount)} servings"
    if portion.kind == "count":
        return "" if amount == 1 else _num(amount)
    return f"{_num(amount)} {_UNIT_TEXT.get(portion.unit or '', portion.unit or '')}".strip()


def identity_text(item: DraftItemState) -> str:
    ident = item.identity
    return " ".join(part for part in (ident.brand, ident.name, ident.variant) if part)


def item_label(item: DraftItemState) -> str:
    name = item.resolved.display_name if item.resolved is not None else identity_text(item)
    label = portion_label(item.portion)
    return f"{name} ({label})" if label else name


# ---------------------------------------------------------------- commit values


@dataclass(frozen=True)
class CommitPlan:
    values: dict[str, Any]
    metadata: dict[str, Any]
    request_hash: str
    unknown: tuple[str, ...]
    totals: NutrientPanel


def item_panels(items: Sequence[DraftItemState]) -> list[NutrientPanel]:
    panels = []
    for item in items:
        panel = item.scaled_panel()
        if panel is None:
            raise DraftNotReady(f"{identity_text(item)} is not resolved yet")
        panels.append(panel)
    return panels


def request_hash_of(values: Mapping[str, Any]) -> str:
    canonical = {**values, "day": values["day"].isoformat() if isinstance(values["day"], date) else values["day"]}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()


def build_commit(draft: AgentDraft, content_hash: str) -> CommitPlan:
    """Values and component_metadata for ``Store.commit_meal_draft``. Deterministic per draft.

    ``draft.meal_type`` must already be pinned so a retry rebuilds the same
    request hash. Unknown nutrients stay ``None``; unknown calories raise.
    """
    if not draft.items:
        raise DraftNotReady("the meal has no items")
    panels = item_panels(draft.items)
    totals = sum_panels(panels)
    unknown = tuple(key for key in NUTRIENT_KEYS if getattr(totals, key) is None)
    if "calories" in unknown:
        raise DraftNotReady("calories are unknown")
    name = domain.validate_name(" + ".join(item_label(item) for item in draft.items)[:200].strip())
    sources = list(dict.fromkeys(item.resolved.macro_source for item in draft.items))  # type: ignore[union-attr]
    source = sources[0] if len(sources) == 1 else "Composite: " + "; ".join(sources)
    source = domain.validate_macro_source(source[:1000])
    known = domain.validate_macros(*(getattr(totals, key) or 0 for key in
                                     ("calories", "protein", "carbs", "fat", "fiber")))
    nutrients = {key: None if key in unknown else known[key] for key in known}
    values = {"name": name, "meal": domain.normalize_meal(draft.meal_type), "day": draft.log_date,
              "macro_source": source, **nutrients}
    metadata = {
        "origin": "food_agent", "schema": SCHEMA_VERSION, "draft_id": draft.id, "content_hash": content_hash,
        "unknown_nutrients": list(unknown),
        "items": [{
            "item_ref": item.item_ref,
            "identity": item.identity.model_dump(mode="json"),
            "portion": item.portion.model_dump(mode="json"),
            "modifications": [m.model_dump(mode="json") for m in item.modifications],
            "multiplier": item.multiplier,
            "food_id": item.resolved.food_id, "provider": item.resolved.provider,  # type: ignore[union-attr]
            "external_id": item.resolved.external_id,  # type: ignore[union-attr]
            "display_name": item.resolved.display_name,  # type: ignore[union-attr]
            "macro_source": item.resolved.macro_source,  # type: ignore[union-attr]
            "serving_basis": item.resolved.serving_basis.model_dump(mode="json"),  # type: ignore[union-attr]
            "nutrients": panel.model_dump(mode="json"),
            "unknown_nutrients": [key for key in NUTRIENT_KEYS if getattr(panel, key) is None],
        } for item, panel in zip(draft.items, panels)],
    }
    return CommitPlan(values=values, metadata=metadata, request_hash=request_hash_of(values),
                      unknown=unknown, totals=totals)


# ---------------------------------------------------------------- row mapping


def _item_row(item: DraftItemState) -> dict[str, Any]:
    portion = item.portion
    needs = None
    if item.resolved is None:
        needs = "identity"
    elif item.multiplier is None:
        needs = "portion"
    prep = (item.identity.preparation or "").casefold()
    return {
        "id": str(uuid4()), "kind": "food", "status": "included",
        "description": (identity_text(item) or item.identity.name)[:200],
        "quantity": portion.amount if portion.amount is not None and portion.amount <= 5000 else None,
        "unit": portion.unit, "preparation": prep if prep in ("raw", "cooked") else None, "size": None,
        "field_origins": {"source": AGENT_MARKER}, "needs": needs, "question": None,
        "resolution": {AGENT_MARKER: SCHEMA_VERSION, "state": item.model_dump(mode="json")},
        "estimate_consent": None, "template_item_id": None,
    }


def draft_to_row(draft: AgentDraft) -> dict[str, Any]:
    return {
        "id": draft.id, "intent": "consume", "state": draft.state, "log_date": draft.log_date,
        "meal_type": draft.meal_type, "meal_type_origin": draft.meal_type_origin, "usual_label": None,
        "session_ref": draft.session_ref, "pending_question": draft.pending,
        "parent_draft_id": draft.parent_draft_id, "amends_entry_id": draft.amends_entry_id,
        "amends_entry_revision": draft.amends_entry_revision,
        "items": [_item_row(item) for item in draft.items],
    }


def _as_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def row_to_draft(row: Mapping[str, Any] | None) -> AgentDraft | None:
    """Parse a store draft row. ``None`` for drafts that are not food-agent drafts."""
    if not row or not row.get("items"):
        return None
    items = []
    for item in row["items"]:
        resolution = item.get("resolution")
        if isinstance(resolution, str):
            resolution = json.loads(resolution)
        if not isinstance(resolution, Mapping) or resolution.get(AGENT_MARKER) != SCHEMA_VERSION:
            return None
        try:
            items.append(DraftItemState.model_validate_json(json.dumps(resolution["state"], default=str)))
        except (ValueError, KeyError):
            return None
    if row.get("intent", "consume") != "consume":
        return None
    revision = row.get("committed_entry_revision")
    amends = row.get("amends_entry_revision")
    return AgentDraft(
        id=str(row["id"]).lower(), log_date=_as_date(row["log_date"]), items=tuple(items),
        revision=int(row["revision"]), state=str(row["state"]), meal_type=row.get("meal_type"),
        meal_type_origin=row.get("meal_type_origin"), session_ref=row.get("session_ref"),
        pending=row.get("pending_question"), updated_at=_as_datetime(row.get("updated_at")),
        parent_draft_id=str(row["parent_draft_id"]) if row.get("parent_draft_id") else None,
        amends_entry_id=str(row["amends_entry_id"]) if row.get("amends_entry_id") else None,
        amends_entry_revision=int(amends) if amends is not None else None,
        committed_entry_id=str(row["committed_entry_id"]) if row.get("committed_entry_id") else None,
        committed_entry_revision=int(revision) if revision is not None else None,
        committed_operation_key=row.get("committed_operation_key"),
    )


# ---------------------------------------------------------------- the port


class DraftPort(Protocol):
    """What the coordinator needs from persistence. Every call is owner-scoped."""

    async def recent(self, since: datetime) -> list[AgentDraft]: ...
    async def get(self, draft_id: str) -> AgentDraft | None: ...
    async def create(self, draft: AgentDraft) -> AgentDraft: ...
    async def save(self, draft: AgentDraft, expected_revision: int) -> AgentDraft | None: ...
    async def cancel(self, draft_id: str, expected_revision: int) -> bool: ...
    async def commit(self, draft_id: str, expected_revision: int, request_hash: str,
                     values: Mapping[str, Any], metadata: Any) -> dict[str, Any]: ...
    async def fetch_entry(self, entry_id: str) -> dict[str, Any] | None: ...
    async def fetch_operation(self, key: str) -> dict[str, Any] | None: ...
    async def acknowledge(self, draft_id: str, revision: int, request_hash: str, entry_id: str) -> None: ...
    async def is_acknowledged(self, draft_id: str, revision: int) -> bool: ...
    async def unknown_nutrients_writable(self) -> bool: ...


class StoreDrafts:
    """Production adapter over ``store.Store`` (migration-004 tables, run_idempotent, readback)."""

    def __init__(self, store: Any, *, limit: int = 8):
        self.store = store
        self.limit = limit

    async def recent(self, since: datetime) -> list[AgentDraft]:
        rows = await self.store.fetch_recent_meal_drafts(since=since, limit=self.limit)
        return [draft for draft in map(row_to_draft, rows) if draft is not None]

    async def get(self, draft_id: str) -> AgentDraft | None:
        return row_to_draft(await self.store.fetch_meal_draft(draft_id))

    async def create(self, draft: AgentDraft) -> AgentDraft:
        created = row_to_draft(await self.store.create_meal_draft(draft_to_row(draft)))
        if created is None:
            raise RuntimeError("created draft could not be read back")
        return created

    async def save(self, draft: AgentDraft, expected_revision: int) -> AgentDraft | None:
        return row_to_draft(await self.store.save_meal_draft(draft_to_row(draft), int(expected_revision)))

    async def cancel(self, draft_id: str, expected_revision: int) -> bool:
        return await self.store.cancel_meal_draft(draft_id, int(expected_revision))

    async def commit(self, draft_id, expected_revision, request_hash, values, metadata):
        from store import CalorieOnlyWriteBlocked, DraftConflict, IdempotencyConflict
        try:
            return await self.store.commit_meal_draft(draft_id, int(expected_revision), request_hash,
                                                      values, metadata)
        except (DraftConflict, IdempotencyConflict) as exc:
            raise DraftCommitConflict(str(exc)) from exc
        except CalorieOnlyWriteBlocked as exc:
            raise DraftCommitBlocked(str(exc)) from exc

    async def fetch_entry(self, entry_id: str) -> dict[str, Any] | None:
        return await self.store.fetch_entry_revision(entry_id)

    async def fetch_operation(self, key: str) -> dict[str, Any] | None:
        return await self.store.fetch_operation(key)

    async def acknowledge(self, draft_id, revision, request_hash, entry_id) -> None:
        async def write(_conn):
            return {"acknowledged": True, "entry_id": str(entry_id)}
        await self.store.run_idempotent(ack_key(draft_id, revision), request_hash, write)

    async def is_acknowledged(self, draft_id, revision) -> bool:
        return await self.store.fetch_operation(ack_key(draft_id, revision)) is not None

    async def unknown_nutrients_writable(self) -> bool:
        from release_gates import calorie_only_write_allowed
        return calorie_only_write_allowed() and await self.store.nullable_nutrients_ready()


@dataclass
class MemoryDrafts:
    """In-memory double of :class:`StoreDrafts` for offline tests and the bench.

    Rows keep the store's shape. Failure knobs: ``fail_before_commit`` (the
    request never reaches the DB), ``fail_after_commit`` (the DB commits, the
    response is lost), ``readback_fails`` (readback returns nothing).
    """

    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    drafts: dict[str, dict[str, Any]] = field(default_factory=dict)
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    ops: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    commit_calls: list[tuple[Any, ...]] = field(default_factory=list)
    unknown_writable: bool = False
    fail_before_commit: int = 0
    fail_after_commit: int = 0
    readback_fails: int = 0
    limit: int = 8

    def _row(self, draft_id: str) -> dict[str, Any] | None:
        return self.drafts.get(str(draft_id).lower())

    async def recent(self, since):
        rows = sorted((row for row in self.drafts.values() if row["updated_at"] >= since),
                      key=lambda row: row["updated_at"], reverse=True)[:self.limit]
        return [draft for draft in map(row_to_draft, copy.deepcopy(rows)) if draft is not None]

    async def get(self, draft_id):
        row = self._row(draft_id)
        return row_to_draft(copy.deepcopy(row)) if row else None

    async def create(self, draft):
        row = {**draft_to_row(draft), "id": draft.id.lower(), "revision": 1, "updated_at": self.clock(),
               "committed_entry_id": None, "committed_entry_revision": None, "committed_operation_key": None}
        self.drafts[row["id"]] = row
        return row_to_draft(copy.deepcopy(row))

    async def save(self, draft, expected_revision):
        row = self._row(draft.id)
        if row is None or row["revision"] != int(expected_revision) or row["state"] not in OPEN_STATES:
            return None
        new = draft_to_row(draft)
        for key in ("state", "meal_type", "meal_type_origin", "pending_question", "items"):
            row[key] = new[key]
        row["revision"] += 1
        row["updated_at"] = self.clock()
        return row_to_draft(copy.deepcopy(row))

    async def cancel(self, draft_id, expected_revision):
        row = self._row(draft_id)
        if row is None or row["revision"] != int(expected_revision) or row["state"] not in OPEN_STATES:
            return False
        row.update(state="canceled", revision=row["revision"] + 1, updated_at=self.clock())
        return True

    async def commit(self, draft_id, expected_revision, request_hash, values, metadata):
        self.commit_calls.append((draft_id, int(expected_revision), request_hash, dict(values), metadata))
        if self.fail_before_commit:
            self.fail_before_commit -= 1
            raise ConnectionError("simulated: request never reached the database")
        key = op_key(str(draft_id).lower(), expected_revision)
        if key in self.ops:
            stored_hash, response = self.ops[key]
            if stored_hash != request_hash:
                raise DraftCommitConflict("Idempotency key was already used for a different request")
            return {**copy.deepcopy(response), "_operation_replayed": True}
        row = self._row(draft_id)
        if row is None or row["state"] != "ready" or row["revision"] != int(expected_revision):
            raise DraftCommitConflict("The meal changed before it was saved")
        if any(values.get(k) is None for k in ("protein", "carbs", "fat", "fiber")) and not self.unknown_writable:
            raise DraftCommitBlocked("Calorie-only entries are not enabled")
        fields = {k: values[k] for k in ("name", "meal", "calories", "protein", "carbs", "fat", "fiber",
                                         "macro_source")}
        if row.get("amends_entry_id"):
            entry = self.entries.get(row["amends_entry_id"])
            if entry is None or entry["entry_revision"] != row["amends_entry_revision"]:
                raise DraftCommitConflict("That logged meal changed since it was read")
            entry.update(fields, entry_revision=entry["entry_revision"] + 1,
                         component_metadata=copy.deepcopy(metadata))
            outcome = "amended"
        else:
            entry = {"id": str(uuid4()), **fields, "date": row["log_date"].isoformat(), "entry_revision": 1,
                     "component_metadata": copy.deepcopy(metadata)}
            self.entries[entry["id"]] = entry
            outcome = "committed"
        row.update(state="committed", committed_entry_id=entry["id"], committed_entry_revision=entry["entry_revision"],
                   committed_operation_key=key, meal_type=values["meal"], updated_at=self.clock())
        response = {"outcome": outcome, "entry": self._public(entry), "day_total": None}
        self.ops[key] = (request_hash, copy.deepcopy(response))
        if self.fail_after_commit:
            self.fail_after_commit -= 1
            raise ConnectionError("simulated: committed, response lost")
        return copy.deepcopy(response)

    @staticmethod
    def _public(entry):
        return {k: entry[k] for k in ("id", "name", "meal", "calories", "protein", "carbs", "fat", "fiber",
                                      "date", "entry_revision", "macro_source")}

    async def fetch_entry(self, entry_id):
        if self.readback_fails:
            self.readback_fails -= 1
            return None
        entry = self.entries.get(str(entry_id))
        return self._public(entry) if entry else None

    async def fetch_operation(self, key):
        if key not in self.ops:
            return None
        request_hash, response = self.ops[key]
        return {"idempotency_key": key, "request_hash": request_hash, "status": "completed",
                "stored_response": copy.deepcopy(response)}

    async def acknowledge(self, draft_id, revision, request_hash, entry_id):
        self.ops.setdefault(ack_key(draft_id, revision), (request_hash, {"acknowledged": True,
                                                                           "entry_id": str(entry_id)}))

    async def is_acknowledged(self, draft_id, revision):
        return ack_key(draft_id, revision) in self.ops

    async def unknown_nutrients_writable(self):
        return self.unknown_writable


__all__ = [
    "OPEN_STATES", "DraftCommitConflict", "DraftCommitBlocked", "DraftNotReady", "op_key", "ack_key",
    "CommitIntent", "AgentDraft", "new_draft", "portion_label", "identity_text", "item_label",
    "CommitPlan", "item_panels", "request_hash_of", "build_commit", "draft_to_row", "row_to_draft",
    "DraftPort", "StoreDrafts", "MemoryDrafts",
]
