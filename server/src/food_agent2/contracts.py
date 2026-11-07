"""What the model may return, and the state code keeps between turns.

Identity, portion and modifications reuse the shapes in ``food_agent.contracts``.
Two things differ from the earlier contract:

* There is no ``ask`` action. The model never writes a question; code decides
  what is missing and asks it once.
* ``supplied`` carries nutrition the user typed. It is the only way a number
  reaches code from the model, and code checks each one against the user's own
  message before using it (:func:`numbers_in`).

Every model-facing type forbids extra fields, so a ``confirmed`` flag or a
nutrient outside ``supplied`` fails validation.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from food_agent.contracts import (MAX_ITEMS_PER_ACTION, MAX_MODIFICATIONS, PENDING_QUESTION_MAX, Cancel,
                                  ClearableIdentityField, FoodIdentity, IdentityPatch, Modification, Nutrient,
                                  Portion, RecallMeal, Ref, RemoveItem, Sha256, _strictify)

SCHEMA_VERSION = "food-agent/2"
MAX_ACTIONS = 12
_STRICT = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Strict(BaseModel):
    model_config = _STRICT


class Supplied(_Strict):
    """Nutrition the user stated for this item as eaten. ``None`` means they did not say."""

    calories: Nutrient | None = None
    protein: Nutrient | None = None
    carbs: Nutrient | None = None
    fat: Nutrient | None = None
    fiber: Nutrient | None = None

    def given(self) -> dict[str, float]:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class FoodSpec(_Strict):
    """One food as the model understood it."""

    item_ref: Ref | None = None
    identity: FoodIdentity
    portion: Portion
    modifications: list[Modification] = Field(default_factory=list, max_length=MAX_MODIFICATIONS)
    supplied: Supplied | None = None


class AddFoods(_Strict):
    type: Literal["add_items"]
    items: list[FoodSpec] = Field(min_length=1, max_length=MAX_ITEMS_PER_ACTION)


class ReviseFood(_Strict):
    """Change one existing item. A null field means keep it."""

    type: Literal["revise_item"]
    item_ref: Ref
    identity: IdentityPatch | None = None
    clear_fields: list[ClearableIdentityField] = Field(default_factory=list, max_length=3)
    portion: Portion | None = None
    modifications: list[Modification] | None = Field(default=None, max_length=MAX_MODIFICATIONS)
    supplied: Supplied | None = None

    @model_validator(mode="before")
    @classmethod
    def _empty_patch_is_none(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("identity"), dict):
            if all(value is None for value in data["identity"].values()):
                data = {**data, "identity": None}
        return data


class Settle(_Strict):
    """The user wants this done now ("just estimate it", "pick any", "use the internet").

    Code stops asking and uses its best labeled answer. ``use_web`` also sends
    anything still unresolved straight to a web estimate.
    """

    type: Literal["settle"]
    use_web: bool


class ConfirmHint(_Strict):
    """The user seems to agree. Never authority: code shows the preview again."""

    type: Literal["confirm"]


class NotFood(_Strict):
    type: Literal["not_food"]


Action = Annotated[Union[AddFoods, ReviseFood, RemoveItem, RecallMeal, Settle, ConfirmHint, Cancel, NotFood],
                   Field(discriminator="type")]


class Turn(_Strict):
    """The whole structured model reply for one user message."""

    schema_version: Literal["food-agent/2"]
    actions: list[Action] = Field(min_length=1, max_length=MAX_ACTIONS)

    @model_validator(mode="after")
    def _coherent(self) -> "Turn":
        types = [action.type for action in self.actions]
        if "not_food" in types and len(types) > 1:
            raise ValueError("not_food must be the only action")
        return self


def turn_json_schema() -> dict[str, Any]:
    """:class:`Turn` as a JSON schema shaped for OpenAI strict structured outputs."""
    return _strictify(Turn.model_json_schema(mode="validation"))


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text: str) -> set[float]:
    """Every number in the user's own words."""
    return {float(found) for found in _NUMBER.findall(text or "")}


def only_stated(supplied: Supplied | None, message: str) -> Supplied | None:
    """Keep only numbers the user actually typed. Anything else is the model's, so it goes."""
    if supplied is None:
        return None
    stated = numbers_in(message)
    kept = {key: value for key, value in supplied.given().items() if value in stated}
    return Supplied(**kept) if kept else None


class Waiting(_Strict):
    """What a draft is waiting for, stored in ``meal_drafts.pending_question`` (<= 1000 chars).

    ``asked`` is why no question repeats: each question has a key, and a key in
    this list is never asked again. ``assumed`` holds the guesses code made when
    the user said to get on with it, so the reply can name them.
    """

    kind: Literal["agent2"] = "agent2"
    stage: Literal["preview", "question", "idle"] = "idle"
    revision: Annotated[int, Field(ge=1)] = 1
    content_hash: Sha256 | None = None
    session_ref: str | None = Field(default=None, max_length=200)
    question: str | None = Field(default=None, max_length=300)
    asked: tuple[str, ...] = ()
    assumed: tuple[str, ...] = ()

    def to_pending(self) -> str:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        if len(text) > PENDING_QUESTION_MAX:
            raise ValueError("waiting state exceeds the pending_question limit")
        return text

    @classmethod
    def read(cls, text: str | None) -> "Waiting":
        """The stored state. Anything else (a commit intent, nothing) is an empty idle state."""
        if not text or not text.lstrip().startswith("{"):
            return cls()
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("kind") != "agent2":
                return cls()
            return cls.model_validate({**data, "asked": tuple(data.get("asked") or ()),
                                       "assumed": tuple(data.get("assumed") or ())})
        except ValueError:
            return cls()


__all__ = ["SCHEMA_VERSION", "Supplied", "FoodSpec", "AddFoods", "ReviseFood", "Settle", "ConfirmHint", "NotFood",
           "Action", "Turn", "turn_json_schema", "numbers_in", "only_stated", "Waiting"]
