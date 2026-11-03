"""Typed contracts for the food agent v1 (Pydantic v2, strict).

Two families live here:

* **Model output** (:class:`AgentTurn` and the :data:`AgentAction` union). This
  is the only thing the model may return. It carries food identity, portion
  and modifications as separate fields, and never nutrients, SQL, user ids or
  a ``confirmed`` flag: every model-facing type forbids extra fields, so such
  keys fail validation instead of being ignored.
* **Draft side** (:class:`DraftItemState`, :class:`Preview`,
  :class:`PendingConfirm`, :class:`ToolOutcome`, :class:`Receipt`). Built by
  code, persisted in the existing migration-004 draft tables, and shown to the
  user. Unknown nutrients are ``None`` end to end, never 0.

All models are strict and frozen. Strict mode means no silent coercion
(``"2"`` is not an int). Choice fields use ``Literal`` so plain strings
validate in both Python and JSON mode.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SCHEMA_VERSION = "food-agent/1"
MAX_ACTIONS = 8
MAX_ITEMS_PER_ACTION = 12
MAX_MODIFICATIONS = 10
MAX_PREVIEW_LINES = 24
PENDING_QUESTION_MAX = 1000  # meal_drafts.pending_question CHECK (migration 004)
NUTRIENT_KEYS = ("protein", "calories", "carbs", "fat", "fiber")  # protein leads receipts

_STRICT = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Strict(BaseModel):
    model_config = _STRICT


# ---------------------------------------------------------------- primitives

def _text(max_length: int, min_length: int = 1) -> Any:
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length,
                                            max_length=max_length)]


Name = _text(120)
ShortText = _text(80)
Ref = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_-]{1,40}$")]
UuidText = Annotated[str, StringConstraints(
    strip_whitespace=True, to_lower=True,
    pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveAmount = Annotated[float, Field(gt=0, le=10000, allow_inf_nan=False)]
Nutrient = Annotated[float, Field(ge=0, le=100000, allow_inf_nan=False)]

FoodKind = Literal["history", "restaurant", "packaged", "generic", "homemade"]
PortionKind = Literal["serving_fraction", "count", "mass", "volume", "unspecified"]
MassUnit = Literal["g", "kg", "oz", "lb"]
VolumeUnit = Literal["fl_oz", "ml", "l", "cup", "tbsp", "tsp"]
PortionUnit = Literal["g", "kg", "oz", "lb", "fl_oz", "ml", "l", "cup", "tbsp", "tsp"]
BasisUnit = Literal["g", "kg", "oz", "lb", "fl_oz", "ml", "l", "cup", "tbsp", "tsp", "count"]
MASS_UNITS: frozenset[str] = frozenset({"g", "kg", "oz", "lb"})
VOLUME_UNITS: frozenset[str] = frozenset({"fl_oz", "ml", "l", "cup", "tbsp", "tsp"})
ModificationOp = Literal["add", "remove", "extra", "light", "swap"]
ClearableIdentityField = Literal["brand", "variant", "preparation"]


# ---------------------------------------------------------------- model output: items

class FoodIdentity(_Strict):
    """What the food is. Never how much, never what was changed."""

    name: Name
    brand: ShortText | None = None
    variant: ShortText | None = None
    kind: FoodKind
    preparation: _text(40) | None = Field(default=None, description="raw, cooked, grilled, fried ...")


class IdentityPatch(_Strict):
    """Partial identity revision. ``None`` on a field means keep the current value."""

    name: Name | None = None
    brand: ShortText | None = None
    variant: ShortText | None = None
    kind: FoodKind | None = None
    preparation: _text(40) | None = None

    def is_empty(self) -> bool:
        return all(getattr(self, key) is None for key in type(self).model_fields)

    def apply_to(self, identity: FoodIdentity,
                 clear_fields: Iterable[ClearableIdentityField] = ()) -> FoodIdentity:
        """Return ``identity`` with this patch applied, then ``clear_fields`` set to None."""
        updates: dict[str, Any] = {k: v for k, v in self.model_dump().items() if v is not None}
        for key in clear_fields:
            updates[key] = None
        return identity.model_copy(update=updates)


class Portion(_Strict):
    """How much, kept apart from identity. ``text`` is the user's own words.

    * ``serving_fraction``: ``amount`` x one serving (0.5 = half).
    * ``count``: ``amount`` units or servings (2 eggs).
    * ``mass``: ``amount`` of a mass ``unit``. A bare "oz" is recorded as
      ``oz`` (mass) exactly as said; portion math reads it as fluid ounces
      against a fluid-ounce serving.
    * ``volume``: ``amount`` of a volume ``unit``.
    * ``unspecified``: the user gave no amount.
    """

    kind: PortionKind
    amount: PositiveAmount | None = None
    unit: PortionUnit | None = None
    text: _text(200, min_length=0) = Field(description="The user's original words for the amount")

    @model_validator(mode="after")
    def _consistent(self) -> "Portion":
        if self.kind == "unspecified":
            if self.amount is not None or self.unit is not None:
                raise ValueError("unspecified portion has no amount or unit")
            return self
        if self.amount is None:
            raise ValueError(f"{self.kind} portion needs a positive amount")
        if self.kind in ("serving_fraction", "count") and self.unit is not None:
            raise ValueError(f"{self.kind} portion has no unit")
        if self.kind == "mass" and self.unit not in MASS_UNITS:
            raise ValueError("mass portion needs g, kg, oz or lb")
        if self.kind == "volume" and self.unit not in VOLUME_UNITS:
            raise ValueError("volume portion needs fl_oz, ml, l, cup, tbsp or tsp")
        return self


class Modification(_Strict):
    op: ModificationOp
    text: ShortText


class ItemSpec(_Strict):
    """One food as the model understood it. Identity, portion, mods are separate."""

    item_ref: Ref | None = Field(default=None, description="Existing draft item ref, or null for a new item")
    identity: FoodIdentity
    portion: Portion
    modifications: list[Modification] = Field(default_factory=list, max_length=MAX_MODIFICATIONS)


# ---------------------------------------------------------------- model output: actions

class AddItems(_Strict):
    type: Literal["add_items"]
    items: list[ItemSpec] = Field(min_length=1, max_length=MAX_ITEMS_PER_ACTION)

    @model_validator(mode="after")
    def _new_items_only(self) -> "AddItems":
        if any(item.item_ref is not None for item in self.items):
            raise ValueError("add_items creates new items; use revise_item for an existing item_ref")
        return self


class ReviseItem(_Strict):
    """Change one existing item. An omitted or null field means PRESERVE.

    ``modifications=[]`` clears modifications; ``None`` keeps them.
    """

    type: Literal["revise_item"]
    item_ref: Ref
    identity: IdentityPatch | None = None
    clear_fields: list[ClearableIdentityField] = Field(default_factory=list, max_length=3)
    portion: Portion | None = None
    modifications: list[Modification] | None = Field(default=None, max_length=MAX_MODIFICATIONS)

    @model_validator(mode="before")
    @classmethod
    def _empty_patch_is_none(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and isinstance(data.get("identity"), Mapping):
            if all(value is None for value in data["identity"].values()):
                data = {**data, "identity": None}
        return data

    @model_validator(mode="after")
    def _changes_something(self) -> "ReviseItem":
        if self.identity is None and not self.clear_fields and self.portion is None \
                and self.modifications is None:
            raise ValueError("revise_item must change identity, portion or modifications")
        if self.identity is not None:
            both = {key for key in self.clear_fields if getattr(self.identity, key) is not None}
            if both:
                raise ValueError(f"cannot both set and clear {sorted(both)}")
        return self


class RemoveItem(_Strict):
    type: Literal["remove_item"]
    item_ref: Ref


class RecallMeal(_Strict):
    """Search the user's OWN history by food match ("the one I had last time")."""

    type: Literal["recall_meal"]
    query: _text(200)
    when_hint: _text(80) | None = None


class Confirm(_Strict):
    """A hint that the user means to confirm. Never authority: code decides."""

    type: Literal["confirm"]
    draft_ref: Ref
    shown_revision: Annotated[int, Field(ge=1)]


class Cancel(_Strict):
    type: Literal["cancel"]


class Ask(_Strict):
    type: Literal["ask"]
    question: _text(300)


class NotFood(_Strict):
    """Hand the turn to the existing coach/canvas path."""

    type: Literal["not_food"]


AgentAction = Annotated[
    Union[AddItems, ReviseItem, RemoveItem, RecallMeal, Confirm, Cancel, Ask, NotFood],
    Field(discriminator="type"),
]
_EDIT_TYPES = frozenset({"add_items", "revise_item", "remove_item", "recall_meal"})


class AgentTurn(_Strict):
    """The whole structured model reply for one user turn."""

    schema_version: Literal["food-agent/1"]
    actions: list[AgentAction] = Field(min_length=1, max_length=MAX_ACTIONS)

    @model_validator(mode="after")
    def _coherent(self) -> "AgentTurn":
        types = [action.type for action in self.actions]
        if "not_food" in types and len(types) > 1:
            raise ValueError("not_food must be the only action")
        if types.count("confirm") > 1:
            raise ValueError("at most one confirm per turn")
        if "confirm" in types and _EDIT_TYPES.intersection(types):
            raise ValueError("confirm cannot be combined with edits in the same turn")
        return self


# ---------------------------------------------------------------- strict JSON schema

_SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items",
                          "anyOf", "enum", "$ref", "$defs", "description", "minItems", "maxItems"})


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(value) for value in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:  # no sibling keywords next to $ref
        return {"$ref": node["$ref"]}
    node = dict(node)
    if "oneOf" in node:
        node["anyOf"] = node.pop("oneOf")
    if "const" in node:
        node["enum"] = [node.pop("const")]
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key in ("properties", "$defs"):
            out[key] = {name: _strictify(sub) for name, sub in value.items()}
        else:
            out[key] = _strictify(value)
    if "properties" in out:
        out["type"] = "object"
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def agent_turn_json_schema() -> dict[str, Any]:
    """JSON schema for :class:`AgentTurn`, shaped for OpenAI strict structured outputs.

    Object root, no root ``anyOf``, every property required (optional fields are
    nullable), ``additionalProperties: false`` on every object, the action
    union as a nested ``anyOf``. Length and range limits are stripped from the
    schema and enforced by Pydantic when the reply is parsed.
    """
    return _strictify(AgentTurn.model_json_schema(mode="validation"))


# ---------------------------------------------------------------- draft side

class ServingBasis(_Strict):
    """What one serving of the resolved food is, as the source states it."""

    text: _text(200, min_length=0)
    basis: Literal["per_serving", "per_100g", "per_100ml"] = "per_serving"
    amount: PositiveAmount | None = None
    unit: BasisUnit | None = None
    count_noun: _text(80) | None = None
    grams: PositiveAmount | None = None
    ml: PositiveAmount | None = None


class NutrientPanel(_Strict):
    """Nutrients in grams (calories in kcal). ``None`` means unknown, never 0."""

    protein: Nutrient | None = None
    calories: Nutrient | None = None
    carbs: Nutrient | None = None
    fat: Nutrient | None = None
    fiber: Nutrient | None = None


class ResolvedFood(_Strict):
    """The source candidate an item resolved to. Panel is per ONE serving basis."""

    food_id: _text(200)
    provider: _text(40)
    external_id: _text(200) | None = None
    display_name: _text(200)
    macro_source: _text(200)
    serving_basis: ServingBasis
    panel: NutrientPanel


class DraftItemState(_Strict):
    """One draft item as stored in ``meal_draft_items.resolution`` JSONB."""

    item_ref: Ref
    identity: FoodIdentity
    portion: Portion
    modifications: list[Modification] = Field(default_factory=list, max_length=MAX_MODIFICATIONS)
    resolved: ResolvedFood | None = None
    multiplier: PositiveAmount | None = None

    def scaled_panel(self) -> NutrientPanel | None:
        """Per-serving panel x multiplier, applied once. ``None`` if not resolved."""
        if self.resolved is None or self.multiplier is None:
            return None
        from food_agent.portion import scale_panel
        return scale_panel(self.resolved.panel, Decimal(repr(self.multiplier)))


def _canon(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        return format(number.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize(), "f")
    if isinstance(value, Mapping):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


def content_hash(items: Sequence[DraftItemState | Mapping[str, Any]]) -> str:
    """Canonical sha256 of what would be logged.

    Covers identity, portion (kind, amount, unit), modifications, resolved
    food id and multiplier. Independent of dict key order and item order;
    excludes the user's portion wording and display text.
    """
    entries = []
    for item in items:
        state = item if isinstance(item, DraftItemState) \
            else DraftItemState.model_validate_json(json.dumps(dict(item), default=str))
        entries.append(json.dumps(_canon({
            "identity": state.identity.model_dump(mode="json"),
            "portion": {"kind": state.portion.kind, "amount": state.portion.amount,
                        "unit": state.portion.unit},
            "modifications": [m.model_dump(mode="json") for m in state.modifications],
            "food_id": state.resolved.food_id if state.resolved else None,
            "multiplier": state.multiplier,
        }), sort_keys=True, separators=(",", ":")))
    payload = json.dumps(sorted(entries), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sum_panels(panels: Iterable[NutrientPanel]) -> NutrientPanel:
    """Total of several panels. One unknown makes that nutrient's total unknown."""
    totals: dict[str, Decimal | None] = {key: Decimal(0) for key in NUTRIENT_KEYS}
    for panel in panels:
        for key in NUTRIENT_KEYS:
            value = getattr(panel, key)
            current = totals[key]
            totals[key] = None if value is None or current is None else current + Decimal(repr(value))
    return NutrientPanel(**{key: None if value is None else
                            float(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
                            for key, value in totals.items()})


class PreviewLine(_Strict):
    item_ref: Ref
    display_name: _text(200)
    portion_text: _text(200, min_length=0)
    nutrients: NutrientPanel


class Preview(_Strict):
    """What the user is shown and may confirm. Bound to one revision and hash."""

    draft_id: UuidText
    revision: Annotated[int, Field(ge=1)]
    content_hash: Sha256
    lines: list[PreviewLine] = Field(min_length=1, max_length=MAX_PREVIEW_LINES)
    totals: NutrientPanel
    created_at: datetime


class PendingConfirm(_Strict):
    """The preview binding stored in ``meal_drafts.pending_question`` (<= 1000 chars)."""

    kind: Literal["confirm"] = "confirm"
    draft_id: UuidText
    revision: Annotated[int, Field(ge=1)]
    content_hash: Sha256

    def to_pending_question(self) -> str:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        if len(text) > PENDING_QUESTION_MAX:  # cannot happen with these bounded fields
            raise ValueError("pending confirmation exceeds pending_question limit")
        return text

    @classmethod
    def from_pending_question(cls, text: str | None) -> "PendingConfirm | None":
        """Parse a stored binding. Anything else (a plain question, garbage) is ``None``."""
        if not text or not text.lstrip().startswith("{"):
            return None
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("kind") != "confirm":
                return None
            return cls.model_validate_json(text)
        except ValueError:  # JSONDecodeError and ValidationError are both ValueErrors
            return None


class ToolOutcome(_Strict):
    """Result of one typed tool call. ``unknown`` effect is never reported as failure."""

    status: Literal["ok", "needs_detail", "no_match", "conflict", "error", "unknown"]
    effect: Literal["not_attempted", "not_applied", "applied_verified", "unknown"]
    safe_message: _text(300)


def _nutrient_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("nutrient cannot be a bool")
    return float(Decimal(str(value)))


class Receipt(_Strict):
    """Built from the SAVED row (``Store.fetch_entry_revision``), never from the draft."""

    entry_id: _text(200)
    entry_revision: Annotated[int, Field(ge=1)]
    name: _text(300)
    nutrients: NutrientPanel
    verified: bool
    macro_source: _text(200) | None = None

    @classmethod
    def from_saved_row(cls, row: Mapping[str, Any], *, verified: bool,
                       unknown_nutrients: Iterable[str] = ()) -> "Receipt":
        """``unknown_nutrients`` (recorded in component_metadata at commit) override
        any stored default so an unknown is never shown as 0."""
        unknown = set(unknown_nutrients)
        return cls(
            entry_id=str(row["id"]), entry_revision=int(row["entry_revision"]), name=str(row["name"]),
            nutrients=NutrientPanel(**{key: None if key in unknown else _nutrient_value(row.get(key))
                                       for key in NUTRIENT_KEYS}),
            verified=verified, macro_source=row.get("macro_source") or None,
        )


__all__ = [
    "SCHEMA_VERSION", "MAX_ACTIONS", "MAX_ITEMS_PER_ACTION", "PENDING_QUESTION_MAX", "NUTRIENT_KEYS",
    "MASS_UNITS", "VOLUME_UNITS",
    "FoodIdentity", "IdentityPatch", "Portion", "Modification", "ItemSpec",
    "AddItems", "ReviseItem", "RemoveItem", "RecallMeal", "Confirm", "Cancel", "Ask", "NotFood",
    "AgentAction", "AgentTurn", "agent_turn_json_schema",
    "ServingBasis", "NutrientPanel", "ResolvedFood", "DraftItemState", "content_hash", "sum_panels",
    "PreviewLine", "Preview", "PendingConfirm", "ToolOutcome", "Receipt",
]
