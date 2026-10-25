"""Durable conversational meal drafts.

One draft per meal the user describes. The model sends structured edits
(add / edit / remove items, answer a question, consent to an estimate); the
server owns everything else: tenant scope, item identity, resolution,
arithmetic, the question to ask, and one atomic idempotent write.

Why this replaces the old "one question per meal" path: that path kept the
meal in process memory and guessed whether the next message answered the
question. A guess that failed either asked again or assumed a standard
serving. Here the meal and the exact open field live in Postgres, so a short
answer patches one item, resolved items are never looked up again, and a
restart or reconnect loses nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID, uuid4

import dish_estimates
import domain
import meal_intent
from food_catalog import normalize_food_name

logger = logging.getLogger("meal_drafts")

OPEN_STATES = frozenset({"collecting", "needs_detail", "ready"})
MASS_UNITS = frozenset({"oz", "ounce", "ounces", "g", "gram", "grams", "lb", "lbs",
                        "pound", "pounds", "kg", "kilogram", "kilograms"})
SIZES = frozenset({"small", "medium", "large"})
PREPARATIONS = frozenset({"raw", "cooked"})
MACROS = domain.MACRO_KEYS
MAX_ITEMS = 12
# Foods whose weight changes a lot with cooking. A stated weight of these is
# ambiguous until raw/cooked is known; nothing else triggers that question.
_WEIGHT_BASIS_FOODS = re.compile(
    r"\b(beef|chicken|turkey|pork|steak|salmon|tuna|fish|shrimp|lamb|bison|venison|"
    r"tilapia|cod|rice|pasta|noodles|quinoa)\b", re.IGNORECASE)
_PREP_WORD = re.compile(r"\b(raw|uncooked|cooked)\b", re.IGNORECASE)
# Words that already say the food was cooked; the weight is then a cooked weight.
_COOKED_WORD = re.compile(r"\b(cooked|grilled|baked|roasted|fried|pan[- ]?browned|broiled|"
                          r"boiled|steamed|seared|smoked|sauteed|sautéed)\b", re.IGNORECASE)
_LEADING_FILLER = re.compile(r"^(?:(?:my|a|an|the|some)\s+)+", re.IGNORECASE)
# "sticky rice with gyoza sauce": the core food and a condiment tail. Only a
# tail that names a condiment is dropped ("rice and beans" stays whole).
_CONDIMENT_TAIL = re.compile(
    r"^(?P<core>.+)\s+(?:with|in|and|plus|&|topped with)\s+(?:(?:a|some|the|extra)\s+)?"
    # The tail must END with the condiment word and not run past another
    # "and"/"with"/"," ("steak with peppercorn sauce and mashed potatoes"
    # stays whole; "chicken with rice and teriyaki sauce" keeps the rice).
    # Compound condiment names are protected first (_COMPOUND_CONDIMENT).
    r"(?P<tail>(?:(?!\b(?:and|with|plus)\b)[^,&])*?\b(?:sauces?|dressings?|glazes?|toppings?|gravy|"
    r"drizzle|dips?|marinade|seasoning))\s*$",
    re.IGNORECASE)
# The only condiment names that may contain "and"/"&" (one token while splitting).
_COMPOUND_CONDIMENT = re.compile(
    r"\b(?:sweet\s*(?:and|&)\s*sour|salt\s*(?:and|&)\s*pepper|oil\s*(?:and|&)\s*vinegar)\b", re.IGNORECASE)
ESTIMATE_PROVENANCE = frozenset({"curated_generic_assumption", "server_web_estimate", "generic_core_fallback",
                                 "typical_serving_estimate"})
TYPICAL = "typical_serving_estimate"


def typical_estimates_enabled() -> bool:
    """Kill switch: TYPICAL_SERVING_ESTIMATES=off restores the previous
    behavior (no recipe-built dish estimates, no typical piece counts)."""
    import os
    return os.environ.get("TYPICAL_SERVING_ESTIMATES", "").strip().casefold() != "off"
NOT_COUNTED_HINT = "tell me what it was to add it"

Resolver = Callable[[Mapping[str, Any]], Awaitable[dict[str, Any] | None]]


class DraftInputError(ValueError):
    """The model sent an edit the server cannot apply. Nothing changed."""


def _fingerprint(resolution: Mapping[str, Any]) -> str:
    material = {k: resolution.get(k) for k in (*MACROS, "macro_source")}
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _clean_description(text: Any) -> str:
    value = " ".join(str(text or "").split()).strip(" .,")
    value = _LEADING_FILLER.sub("", value).strip()
    if not 1 <= len(value) <= 200:
        raise DraftInputError("Each food needs a short description")
    return value


def _number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise DraftInputError(f"{field} must be a number") from None
    if not 0 < number <= 5000:
        raise DraftInputError(f"{field} must be above zero")
    return number


def item_ref(item: Mapping[str, Any]) -> str:
    return f"item{int(item['position']) + 1}"


def portion_text(item: Mapping[str, Any]) -> str:
    quantity, unit, size = item.get("quantity"), item.get("unit"), item.get("size")
    parts = []
    if quantity is not None:
        parts.append(f"{quantity:g}")
    elif size or unit:
        parts.append("1")
    if size:
        parts.append(size)
    if unit:
        parts.append(unit)
    return " ".join(parts)


def item_text(item: Mapping[str, Any]) -> str:
    text = " ".join(part for part in (portion_text(item), item["description"]) if part)
    if item.get("preparation") and item.get("unit") in MASS_UNITS:
        text += f", weighed {item['preparation']}"
    elif item.get("preparation") and not _PREP_WORD.search(item["description"]):
        text += f" ({item['preparation']})"
    return text


def needs_weight_basis(item: Mapping[str, Any]) -> bool:
    return (item.get("unit") in MASS_UNITS and not item.get("preparation")
            and not _PREP_WORD.search(item["description"])
            and not _COOKED_WORD.search(item["description"])
            and bool(_WEIGHT_BASIS_FOODS.search(item["description"])))


def generic_core(description: str) -> tuple[str, str] | None:
    """(core food, dropped condiment) for "X with <sauce>", else None."""
    compounds: list[str] = []

    def protect(match) -> str:
        compounds.append(match[0])
        return f"condimentname{len(compounds) - 1}x"

    def restore(text: str) -> str:
        return re.sub(r"condimentname(\d+)x", lambda m: compounds[int(m[1])], text)

    core, tails = _COMPOUND_CONDIMENT.sub(protect, " ".join(description.split())), []
    while match := _CONDIMENT_TAIL.match(core):
        core = match["core"].strip(" ,.")
        tails.insert(0, restore(match["tail"].strip(" ,.")))
    core = _LEADING_FILLER.sub("", restore(core)).strip()
    return (core, " and ".join(tails)) if tails and core else None


def not_counted(item: Mapping[str, Any]) -> str | None:
    """Why an item was left out of the meal's numbers, or None."""
    if item.get("status") != "removed":
        return None
    reason = (item.get("field_origins") or {}).get("not_counted")
    return str(reason) if reason else None


def estimate_label(resolution: Mapping[str, Any]) -> str | None:
    """'(est., <source>)' text for an estimated item, else None."""
    provenance = resolution.get("estimate_provenance")
    if provenance not in ESTIMATE_PROVENANCE:
        return None
    if provenance == TYPICAL:
        size = resolution.get("typical_size")
        return f"(est., typical {size} recipe)" if size else "(est., standard serving)"
    web = provenance == "server_web_estimate" or resolution.get("core_provenance") == "server_web_estimate"
    host = (resolution.get("attribution") or {}).get("source_host") if web else None
    parts = [f"est., {host or ('web' if web else 'generic')}"]
    if resolution.get("core_note"):
        parts.append(str(resolution["core_note"]))
    return "(" + "; ".join(parts) + ")"


def default_meal_type(now: datetime) -> str:
    """Meal slot from the local clock when the user didn't name one."""
    local = now.astimezone(domain.LOCAL_TZ)
    minutes = local.hour * 60 + local.minute
    if minutes < 4 * 60:
        return "Snack"  # after midnight, before the 4am logging-day rollover
    if minutes < 10 * 60 + 30:
        return "Breakfast"
    if minutes < 15 * 60:
        return "Lunch"
    if minutes < 21 * 60:
        return "Dinner"
    return "Snack"


def _food_query(item: Mapping[str, Any]) -> dict[str, Any]:
    """The resolver component for one item: identity plus the stated portion."""
    description = meal_intent.lookup_description(item)
    preparation = item.get("preparation")
    if preparation and not _PREP_WORD.search(description):
        description = f"{preparation} {description}"
    component: dict[str, Any] = {"description": description}
    if item.get("unit") or item.get("size"):
        component["portion"] = portion_text(item)
    elif item.get("quantity") is not None:
        component["quantity"] = item["quantity"]
    return component


def _persistable(resolution: Mapping[str, Any]) -> dict[str, Any]:
    """What may be stored in a draft. Provider panels that are not licensed
    for caching keep only their identity; nutrition is re-fetched on resume."""
    raw_attribution = resolution.get("attribution")
    attribution: Mapping[str, Any] = raw_attribution if isinstance(raw_attribution, Mapping) else {}
    kept = {key: resolution.get(key) for key in (
        "name", "macro_source", *MACROS, "assumption", "basis", "estimate_provenance",
        "carbohydrate_definition", "typical_size", "built_from", "not_counted_parts", "place_not_found")
        if resolution.get(key) is not None}
    kept["attribution"] = {k: attribution.get(k) for k in (
        "provider", "external_id", "verification_state", "cache_allowed", "source_host")
        if attribution.get(k) is not None}
    kept["fingerprint"] = _fingerprint(resolution)
    nested = [a for a in (resolution.get("component_attributions") or []) if isinstance(a, Mapping)]
    nested += [m["attribution"] for m in (resolution.get("component_metadata") or [])
               if isinstance(m, Mapping) and isinstance(m.get("attribution"), Mapping)]
    unlicensed = any(a.get("provider") == "FatSecret" and not a.get("cache_allowed")
                     for a in [attribution, *nested])
    if unlicensed:
        for key in MACROS:
            kept.pop(key, None)
        kept["refresh"] = True
    return kept


class MealDrafts:
    """Server-owned meal conversation state for the authenticated user."""

    def __init__(self, *, store, resolve: Resolver,
                 validate_values: Callable[..., dict[str, Any]],
                 unresolved_question: Callable[[str], Awaitable[str]] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.store = store
        self.resolve = resolve
        # Kept for callers of the previous signature; drafts never ask the
        # brand/dish question (an unknown food is estimated or not counted).
        self.unresolved_question = unresolved_question
        self.validate_values = validate_values
        self.clock = clock or (lambda: datetime.now(domain.LOCAL_TZ))

    # ------------------------------------------------------------------ #
    # Context for the model
    # ------------------------------------------------------------------ #

    async def context(self) -> dict[str, Any]:
        """Open drafts and today's/yesterday's current logged meals, compactly."""
        today = domain.effective_date(self.clock())
        found = await self.store.fetch_meal_draft_context(
            open_since=self.clock() - timedelta(hours=12),
            committed_days=[today, today - timedelta(days=1)])

        def brief(draft):
            return {"draft_ref": str(draft["id"]), "state": draft["state"], "intent": draft["intent"],
                    "date": draft["log_date"].isoformat() if isinstance(draft["log_date"], date) else str(draft["log_date"]),
                    "meal_type": draft.get("meal_type"),
                    "items": [{"item_ref": item_ref(item), "text": item_text(item),
                               **({"needs": item["needs"]} if item.get("needs") else {})}
                              for item in draft["items"] if item["status"] == "included"],
                    **self._not_counted_brief(draft),
                    **({"question": draft["pending_question"]} if draft.get("pending_question") else {})}
        return {"open_meals": [brief(d) for d in found["open"]],
                "logged_meals": [brief(d) for d in found["committed"]]}

    # ------------------------------------------------------------------ #
    # The one tool
    # ------------------------------------------------------------------ #

    async def settle(self, draft_ref: str, *, turn_ref: str) -> dict[str, Any] | None:
        """End of a user turn: an open meal whose question was asked on an
        EARLIER turn and is still unanswered is not asked again (ask once).
        Returns the outcome, or None when nothing was due."""
        draft = await self.store.fetch_meal_draft(str(draft_ref))
        if draft is None or draft["state"] not in OPEN_STATES:
            return None
        if not any(item["status"] == "included" and self._asked_before(item, turn_ref)
                   for item in draft["items"]):
            return None
        return await self.handle({"draft_ref": str(draft_ref)}, turn_ref=turn_ref, settle=True)

    @staticmethod
    def _asked_before(item, turn_ref: str | None) -> bool:
        """The item still needs what it was asked, and it was asked on another
        user turn (without a turn reference, any earlier call counts)."""
        origins = item.get("field_origins") or {}
        return (bool(item.get("needs")) and origins.get("asked") == item["needs"]
                and (turn_ref is None or origins.get("asked_turn") != turn_ref))

    async def handle(self, args: Mapping[str, Any], *, session_ref: str | None = None,
                     turn_ref: str | None = None, settle: bool = True) -> dict[str, Any]:
        """Apply one update_meal call. `turn_ref` names the user turn; with
        settle=False (the canvas, which settles once at the end of the turn)
        no unanswered question is settled mid-turn, so several calls in one
        round all apply before ask-once decides anything."""
        allowed = {"draft_ref", "amend_ref", "repeat_ref", "intent", "meal_type", "add", "edit",
                   "remove", "only", "skip", "log_now", "consent_estimate", "remember_preparation",
                   "save_usual", "cancel"}
        unknown = set(args) - allowed
        if unknown:
            raise DraftInputError("Unknown meal fields: " + ", ".join(sorted(unknown)))
        if sum(bool(args.get(key)) for key in ("draft_ref", "amend_ref", "repeat_ref")) > 1:
            raise DraftInputError("Use only one of draft_ref, amend_ref or repeat_ref")

        if args.get("save_usual"):
            return await self._save_usual(args)

        draft = await self._open_draft(args, session_ref)
        if isinstance(draft, dict) and draft.get("status"):
            return draft  # conflict / not found message
        if draft.get("_new"):
            # Validate the edits on the in-memory draft first, so a bad edit
            # never leaves a stray open (or repeat-copy) draft behind.
            await self._apply_edits(draft, args)
            draft.pop("_new")
            draft["_edits_applied"] = True
            draft = {**(await self.store.create_meal_draft(draft)), "_edits_applied": True,
                     "items": draft["items"]}
        expected = int(draft["revision"])
        if args.get("cancel"):
            if draft["state"] not in OPEN_STATES:
                return {"status": "conflict", "confirmation": "That meal is already logged; nothing was canceled."}
            await self.store.cancel_meal_draft(str(draft["id"]), expected)
            return {"status": "canceled", "confirmation": "Okay, I didn't log that meal."}
        if draft["state"] == "committed" and (set(args) == {"draft_ref"} or self._already_applied(draft, args)):
            # Recovery after an uncertain save, or a retried identical call:
            # report what was written, never write again.
            return await self._recover(draft)
        if draft["state"] not in OPEN_STATES:
            return {"status": "conflict", "draft_ref": str(draft["id"]),
                    "confirmation": "That meal is already logged. To change it, use amend_ref."}

        if not draft.pop("_edits_applied", False):
            await self._apply_edits(draft, args)
        draft["_turn_ref"], draft["_settle"] = turn_ref, settle
        self._normalize_legacy(draft)

        def log_now():
            # "Log what you have": an item already asked on an earlier turn is
            # left out, never guessed. A blocking question the user never saw
            # (a new weighed raw/cooked food) is still asked once.
            for item in draft["items"]:
                if item["status"] == "included" and self._asked_before(item, turn_ref):
                    self._not_count(item, self._open_reason(item))
        if args.get("log_now"):
            log_now()  # before resolution, so no basis is even looked up
        await self._resolve_items(draft)
        if args.get("log_now"):
            log_now()
        self._decide_state(draft)
        if draft["state"] == "ready" and draft["intent"] == "consume":
            # Panels that may not be stored are fetched again before the save,
            # so a source that no longer answers is left out of this meal (and
            # of the saved draft) instead of blocking it.
            for item in draft["items"]:
                if (item["status"] == "included" and "_live" not in item
                        and (item.get("resolution") or {}).get("refresh")):
                    found = await self._refresh(item)
                    if found is None and draft.get("amends_entry_id"):
                        # Amending a logged entry never drops a food it already
                        # counts (same rule as a usual): refuse; the entry stays.
                        await self.store.cancel_meal_draft(str(draft["id"]), expected)
                        return {"status": "failed", "draft_ref": str(draft["id"]),
                                "confirmation": f"I couldn't re-check {item_text(item)} right now, "
                                                "so nothing changed."}
                    if found is None:
                        self._not_count(item, "nutrition source unavailable")
                    else:
                        item["_live"] = {k: found.get(k) for k in MACROS}
            self._decide_state(draft)
        draft.pop("_turn_ref", None)
        draft.pop("_settle", None)
        # Numbers fetched this turn but not storable (unlicensed panels) are
        # carried in memory to the commit so nothing is looked up twice.
        live = {item["id"]: item.pop("_live") for item in draft["items"] if "_live" in item}
        saved = await self.store.save_meal_draft(draft, expected)
        if saved is None:
            current = await self.store.fetch_meal_draft(str(draft["id"]))
            return {"status": "conflict", "draft_ref": str(draft["id"]),
                    "draft": self._summary(current) if current else None,
                    "confirmation": "That meal changed while I was updating it. Check it and tell me again."}
        draft = saved
        if draft["state"] == "ready" and draft["intent"] == "consume":
            return await self._commit(draft, live)
        return self._reply(draft)

    async def _open_draft(self, args, session_ref):
        now = self.clock()
        if args.get("draft_ref"):
            draft = await self.store.fetch_meal_draft(str(args["draft_ref"]))
            if draft is None:
                return {"status": "not_found", "confirmation": "I couldn't find that meal. Tell me what you ate."}
            return draft
        source_ref = args.get("amend_ref") or args.get("repeat_ref")
        source = None
        if source_ref:
            source = await self.store.fetch_meal_draft(str(source_ref))
            if source is None or source["state"] != "committed":
                return {"status": "not_found", "confirmation": "I couldn't find that logged meal."}
        intent = args.get("intent") or (source["intent"] if source else "consume")
        if intent not in {"consume", "plan"}:
            raise DraftInputError("intent must be consume or plan")
        draft: dict[str, Any] = {
            "id": str(uuid4()), "intent": intent, "state": "collecting",
            "log_date": domain.effective_date(now), "meal_type": None, "meal_type_origin": None,
            "usual_label": None, "session_ref": session_ref, "pending_question": None,
            "parent_draft_id": None, "amends_entry_id": None, "amends_entry_revision": None, "items": [],
        }
        if source is not None:
            draft["items"] = [{**item, "id": str(uuid4())} for item in source["items"]]
            for item in draft["items"]:
                resolution = item.get("resolution") or {}
                # A web estimate is fetched fresh for a repeat, never reused;
                # unlicensed panels were never stored, so they are fetched again.
                if args.get("repeat_ref") and (resolution.get("estimate_provenance") == "server_web_estimate"
                                               or resolution.get("refresh")):
                    self._invalidate(item)
            draft["meal_type"], draft["meal_type_origin"] = source.get("meal_type"), source.get("meal_type_origin")
            draft["usual_label"] = source.get("usual_label")
            if args.get("amend_ref"):
                entry = await self.store.fetch_entry_revision(str(source["committed_entry_id"]))
                if entry is None or int(entry["entry_revision"]) != int(source["committed_entry_revision"]):
                    return {"status": "conflict",
                            "confirmation": "That logged meal was changed or removed since. Check Today first."}
                if draft["meal_type_origin"] != "user":
                    # A correction keeps the slot the entry was logged in.
                    draft["meal_type"], draft["meal_type_origin"] = entry["meal"], "logged"
                draft.update(parent_draft_id=str(source["id"]), log_date=source["log_date"],
                             amends_entry_id=str(source["committed_entry_id"]),
                             amends_entry_revision=int(source["committed_entry_revision"]))
            else:
                # A repeat is a new meal now: its slot follows the current time
                # unless the user names one (an amend keeps the logged slot).
                if draft["meal_type_origin"] != "user":
                    draft["meal_type"] = draft["meal_type_origin"] = None
        draft["_new"] = True
        return draft

    # ------------------------------------------------------------------ #
    # Edits
    # ------------------------------------------------------------------ #

    def _item(self, draft, ref) -> dict[str, Any]:
        for item in draft["items"]:
            if item_ref(item) == ref and item["status"] == "included":
                return item
        raise DraftInputError(f"Unknown item_ref {ref!r}; use an item_ref from this meal")

    def _any_item(self, draft, ref) -> dict[str, Any]:
        """An item by ref whatever its status (remove/skip of a left-out item is a no-op)."""
        for item in draft["items"]:
            if item_ref(item) == ref:
                return item
        raise DraftInputError(f"Unknown item_ref {ref!r}; use an item_ref from this meal")

    @staticmethod
    def _not_count(item, reason: str) -> None:
        """Leave an item out of the meal's numbers, with the reason the receipt names."""
        item["status"] = "removed"
        item["field_origins"] = {**(item.get("field_origins") or {}), "not_counted": reason}
        item["needs"] = item["question"] = item["resolution"] = None
        item.pop("_live", None)

    @staticmethod
    def _user_remove(item) -> None:
        """Removed on purpose by the user: left out of the meal and of the
        receipt's "not counted" list."""
        item["status"] = "removed"
        item["field_origins"] = {**{k: v for k, v in (item.get("field_origins") or {}).items()
                                    if k != "not_counted"}, "user_removed": True}

    @staticmethod
    def _open_reason(item) -> str:
        origins = item.get("field_origins") or {}
        return {"preparation": "raw or cooked unknown",
                "portion": (f"its nutrition source can't be measured as {origins['unmeasurable']}"
                            if origins.get("unmeasurable") else "amount unknown"),
                "contents": "contents unknown"}.get(item.get("needs"), "no nutrition source found")

    def _already_applied(self, draft, args) -> bool:
        """Whether a call on a logged meal changes nothing it doesn't already say
        (a retry of the call that logged it)."""
        if set(args) - {"draft_ref", "edit", "remove", "only", "skip", "log_now", "consent_estimate"}:
            return False
        try:
            only = args.get("only") or []
            for ref in only:
                self._any_item(draft, ref)
            others = [item_ref(item) for item in draft["items"] if only and item_ref(item) not in only]
            for ref in [*(args.get("remove") or []), *(args.get("skip") or []), *others]:
                if self._any_item(draft, ref)["status"] != "removed":
                    return False
            for spec in args.get("edit") or []:
                item = self._any_item(draft, spec.get("item_ref"))
                for key, value in spec.items():
                    if key == "item_ref":
                        continue
                    current = item.get(key)
                    if key in {"source", "place"}:
                        current = (item.get("field_origins") or {}).get("source_kind" if key == "source" else "place")
                        if key == "place" and value is not None:
                            value = meal_intent.clean_place(value)
                    if key == "description":
                        value = _clean_description(value)
                    elif key == "quantity":
                        value = _number(value, key)
                    elif key == "unit" and value is not None:
                        value = str(value).strip().casefold()
                    if current != value:
                        return False
        except (DraftInputError, AttributeError, TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _invalidate(item) -> None:
        item["resolution"] = None
        item["needs"] = None
        item["question"] = None
        item["estimate_consent"] = None

    def _apply_fields(self, item, spec, origin: str) -> None:
        origins = dict(item.get("field_origins") or {})
        if "description" in spec:
            item["description"] = _clean_description(spec["description"])
            origins["description"] = origin
        if "quantity" in spec:
            item["quantity"] = _number(spec["quantity"], "quantity")
            origins["quantity"] = origin
        if "unit" in spec:
            unit = (str(spec["unit"]).strip().casefold() or None) if spec["unit"] is not None else None
            if unit is not None and not re.fullmatch(r"[a-z]{1,20}", unit):
                raise DraftInputError("unit must be a word like oz, g, cup or slice")
            item["unit"] = unit
            origins["unit"] = origin
        if "size" in spec:
            size = spec["size"]
            if size is not None and size not in SIZES:
                raise DraftInputError("size must be small, medium or large")
            item["size"] = size
            origins["size"] = origin
        if "preparation" in spec:
            preparation = spec["preparation"]
            if preparation is not None and preparation not in PREPARATIONS:
                raise DraftInputError("preparation must be raw or cooked")
            item["preparation"] = preparation
            origins["preparation"] = origin
        self._route(item, origins, spec)
        item["field_origins"] = origins
        self._invalidate(item)

    @staticmethod
    def _route(item, origins, spec) -> None:
        """The intent layer: restaurant, brand or homemade, decided before any
        lookup (meal_intent). The model's call wins; otherwise the words decide;
        a description edit re-infers unless the model chose the lane."""
        kind = spec.get("source", origins.get("source_kind") if origins.get("source_decided") == "model" else None)
        if kind is not None and kind not in meal_intent.KINDS:
            raise DraftInputError("source must be restaurant, packaged or homemade")
        try:
            place = meal_intent.clean_place(spec["place"]) if "place" in spec else (
                origins.get("place") if origins.get("source_decided") == "model" else None)
        except ValueError as exc:
            raise DraftInputError(str(exc)) from None
        if place and kind is None:
            kind = "restaurant" if meal_intent.chain_in(place) else "packaged"
        if kind == "packaged" and not place:
            kind = None  # a packaged lane needs the maker's name; let the words decide
        lane, place, decided = meal_intent.route(item["description"], kind, place)
        for key in ("source_kind", "place", "source_decided"):
            origins.pop(key, None)
        if lane:
            origins.update(source_kind=lane, source_decided=decided, **({"place": place} if place else {}))

    async def _apply_edits(self, draft, args) -> None:
        if args.get("intent"):
            if args["intent"] not in {"consume", "plan"}:
                raise DraftInputError("intent must be consume or plan")
            draft["intent"] = args["intent"]
        if args.get("meal_type"):
            draft["meal_type"] = domain.normalize_meal(args["meal_type"])
            draft["meal_type_origin"] = "user"
        only = args.get("only") or []
        for ref in only:
            self._any_item(draft, ref)  # unknown refs are rejected before anything changes
        removed = list(args.get("remove") or [])
        if only:
            # "Just log the tuna": every other item, counted or not, is removed by the user.
            removed += [item_ref(item) for item in draft["items"] if item_ref(item) not in only]
        for ref in removed:
            self._user_remove(self._any_item(draft, ref))
        for ref in args.get("skip") or []:
            item = self._any_item(draft, ref)
            if item["status"] == "included":
                self._not_count(item, "skipped")
        for spec in args.get("edit") or []:
            if not isinstance(spec, Mapping) or "item_ref" not in spec:
                raise DraftInputError("Each edit needs an item_ref")
            fields = {k: v for k, v in spec.items() if k != "item_ref"}
            if not fields or set(fields) - {"description", "quantity", "unit", "size", "preparation", "source", "place"}:
                raise DraftInputError("Edits may change description, quantity, unit, size, preparation, source or place")
            item = self._item(draft, spec["item_ref"])
            if item.get("kind") == "usual" and item.get("resolution") and set(fields) == {"quantity"}:
                # Opaque saved preset: "make that two" scales its stored panel.
                base = item["resolution"].get("per_serving") or {k: item["resolution"].get(k) for k in MACROS}
                count = _number(fields["quantity"], "quantity")
                item["resolution"] = {**item["resolution"], "per_serving": base,
                                      **{k: (round(float(base[k]) * count, 2) if base.get(k) is not None else None)
                                         for k in MACROS}}
                item["resolution"]["fingerprint"] = _fingerprint(item["resolution"])
                item["quantity"] = count
                continue
            if item.get("kind") == "usual":
                if item.get("resolution"):
                    raise DraftInputError("A saved preset can only change its quantity; add separate foods instead")
                item["kind"] = "food"  # the user is naming what the usual actually was
            self._apply_fields(item, fields, "user")
        for ref in args.get("consent_estimate") or []:
            item = self._item(draft, ref)
            if item.get("needs") == "estimate_consent" and item.get("resolution"):
                item["estimate_consent"] = item["resolution"].get("fingerprint")
                item["needs"] = None
                item["question"] = None
        for spec in args.get("remember_preparation") or []:
            if not isinstance(spec, Mapping) or spec.get("preparation") not in PREPARATIONS:
                raise DraftInputError("remember_preparation needs item_ref and raw or cooked")
            item = self._item(draft, spec.get("item_ref"))
            await self.store.set_food_default(self._identity_key(item), spec["preparation"])
            if item.get("preparation") != spec["preparation"]:
                self._apply_fields(item, {"preparation": spec["preparation"]}, "user")
        added = args.get("add") or []
        if added:
            # Answering "what is in your usual?" replaces the placeholder.
            for item in draft["items"]:
                if item["status"] == "included" and item.get("kind") == "usual" and item.get("needs") == "contents":
                    item["status"] = "removed"
        for spec in added:
            if not isinstance(spec, Mapping):
                raise DraftInputError("Each added food must be an object")
            if set(spec) - {"description", "quantity", "unit", "size", "preparation", "usual", "source", "place"}:
                raise DraftInputError("Added foods take description, quantity, unit, size, preparation, "
                                      "source, place, usual")
            if spec.get("usual"):
                await self._add_usual(draft, spec)
                continue
            # Adding back a food that was not counted replaces that record, so
            # the receipt never lists it as both logged and not counted.
            name = normalize_food_name(spec.get("description") or "")
            for previous in draft["items"]:
                if not_counted(previous) and normalize_food_name(previous["description"]) == name:
                    self._user_remove(previous)
            item = self._new_item(draft)
            self._apply_fields(item, {k: v for k, v in spec.items() if k != "usual"}, "user")
            draft["items"].append(item)
        if sum(item["status"] == "included" for item in draft["items"]) > MAX_ITEMS:
            raise DraftInputError(f"A meal can hold at most {MAX_ITEMS} foods")

    @staticmethod
    def _new_item(draft) -> dict[str, Any]:
        return {"id": str(uuid4()), "position": len(draft["items"]), "kind": "food",
                "status": "included", "description": "food", "quantity": None, "unit": None,
                "preparation": None, "size": None, "field_origins": {}, "needs": None,
                "question": None, "resolution": None, "estimate_consent": None,
                "template_item_id": None}

    @staticmethod
    def _identity_key(item) -> str:
        return " ".join(_PREP_WORD.sub(" ", item["description"]).split())

    async def _add_usual(self, draft, spec) -> None:
        label = _clean_description(spec.get("description"))
        label = re.sub(r"^usual\s+", "", label, flags=re.IGNORECASE).strip() or label
        draft["usual_label"] = label[:80]
        saved = await self.store.resolve_usual(f"usual {label}") or await self.store.resolve_usual(label)
        if saved and saved["items"]:
            for template in saved["items"]:
                item = self._new_item(draft)
                item.update(description=template["description"], unit=template.get("unit"),
                            quantity=float(template["quantity"]) if template.get("quantity") is not None else None,
                            preparation=template.get("preparation"), size=template.get("size"),
                            template_item_id=str(template["id"]),
                            field_origins={"all": "saved_usual"})
                draft["items"].append(item)
            return
        item = self._new_item(draft)
        item.update(kind="usual", description=label)
        if saved:
            # Legacy flat preset: usable as one opaque item, never split.
            preset = saved["preset"]
            item["resolution"] = {"name": preset["name"], "macro_source": f"Meal Preset: {preset['name']}",
                                  **{k: float(preset[k]) if preset.get(k) is not None else None for k in MACROS}}
            item["resolution"]["fingerprint"] = _fingerprint(item["resolution"])
            item["field_origins"] = {"all": "saved_preset"}
        else:
            item["needs"] = "contents"
            item["question"] = (f"I don't have your usual {label} saved yet. What do you normally get, "
                                "including sizes, drink and sauces?")
        draft["items"].append(item)

    # ------------------------------------------------------------------ #
    # Resolution (each item at most once per change)
    # ------------------------------------------------------------------ #

    async def _lookup(self, item, gate: asyncio.Semaphore | None = None) -> dict[str, Any] | None:
        """One resolver call for an item; a weight is only priced with a panel of
        the same raw/cooked basis. Provider failure keeps the item, never the meal."""
        try:
            if gate is None:
                found = await self.resolve(_food_query(item))
            else:
                async with gate:
                    found = await self.resolve(_food_query(item))
        except Exception as exc:
            logger.warning("meal draft item resolution failed: %s", type(exc).__name__)
            return None
        if found is None or found.get("portion_error"):
            return found
        basis = str(found.get("basis") or "").casefold()
        if item.get("unit") in MASS_UNITS and (
                (item.get("preparation") == "cooked" and "raw" in basis)
                or (item.get("preparation") == "raw" and "cooked" in basis)):
            return None
        return found

    async def _refresh(self, item) -> dict[str, Any] | None:
        """Fetch an unstorable panel again with the identity it was priced by
        (the core food for a generic-core estimate), through the same basis
        check. None when the source no longer returns that exact panel."""
        resolution = item.get("resolution") or {}
        query = {**item, "description": resolution["core_identity"]} if resolution.get("core_identity") else item
        found = await self._lookup(query)
        if not found or found.get("portion_error") or _fingerprint(found) != resolution.get("fingerprint"):
            return None
        return found

    @staticmethod
    def _accept(item, found, **extra) -> None:
        item["resolution"] = {**_persistable(found), **extra}
        # Keep live numbers for this turn even when they may not be stored.
        item["_live"] = {k: found.get(k) for k in MACROS}
        item["needs"] = item["question"] = None

    async def _settle_asked(self, item) -> None:
        """Ask once: an item still open on a later turn for the question it was
        already asked is not asked again. Raw/cooked is assumed only when the
        resolver has a source for exactly one basis; otherwise it is not counted."""
        if item.get("needs") == "preparation":
            sourced = {}
            for basis in sorted(PREPARATIONS):
                found = await self._lookup({**item, "preparation": basis})
                if found is not None and not found.get("portion_error"):
                    sourced[basis] = found
            if len(sourced) == 1:
                (basis, found), = sourced.items()
                item["preparation"] = basis
                item["field_origins"] = {**(item.get("field_origins") or {}), "preparation": "assumed"}
                self._accept(item, found)
                return
        self._not_count(item, self._open_reason(item))

    @staticmethod
    def _normalize_legacy(draft) -> None:
        """Drafts saved by the previous release: runs before any lane, so
        "that's fine" on a meal waiting for consent keeps its estimate."""
        for item in draft["items"]:
            if item["status"] != "included":
                continue
            if item.get("needs") == "estimate_consent" and item.get("resolution"):
                # Option A: estimates are included and disclosed; no consent wait.
                item["needs"] = item["question"] = None
            elif item.get("needs") == "identity":
                # The brand/dish question is gone; the item is resolved again.
                item["needs"] = item["question"] = None

    async def _resolve_items(self, draft) -> None:
        self._normalize_legacy(draft)
        if draft.get("_settle", True):
            for item in draft["items"]:
                if item["status"] == "included" and self._asked_before(item, draft.get("_turn_ref")):
                    await self._settle_asked(item)
        # Only items whose fields changed (edits clear `needs`) or that wait on
        # raw/cooked are looked at. Resolved and still-unanswered items are not
        # looked up again.
        pending = [item for item in draft["items"] if item["status"] == "included"
                   and item.get("kind") == "food" and item.get("resolution") is None
                   and item.get("needs") in (None, "preparation")]
        for item in pending:
            if needs_weight_basis(item):
                remembered = await self.store.fetch_food_default(self._identity_key(item))
                if remembered in PREPARATIONS:
                    item["preparation"] = remembered
                    item["field_origins"] = {**(item.get("field_origins") or {}), "preparation": "saved_default"}
        gate = asyncio.Semaphore(4)

        typical_on = typical_estimates_enabled()

        async def one(item):
            if needs_weight_basis(item):
                item["needs"] = "preparation"
                item["question"] = f"Was that {portion_text(item)} of {item['description']} weighed raw or cooked?"
                return
            origins = item.get("field_origins") or {}
            if typical_on and item.get("quantity") is None and not item.get("unit") and not item.get("size") \
                    and origins.get("all") not in {"saved_usual", "saved_preset"}:
                # "eggs", "bacon": no amount at all means a typical serving, not one piece.
                count = dish_estimates.typical_count(item["description"])
                if count:
                    item["quantity"] = float(count)
                    item["field_origins"] = origins = {**origins, "quantity": "typical_serving"}
            typical = dish_estimates.estimate(item) if typical_on else None
            lane = origins.get("source_kind") if typical_on else None
            if typical and typical.get("dish") and (lane == "homemade" or (lane is None and typical.get("listed"))):
                # Home cooking (or a dish the user described by its contents): a
                # menu or web number for the dish name can't know this one, so it
                # is built from a typical recipe. Never looked up by name.
                self._accept(item, typical)
                return
            found = await self._lookup(item, gate)
            if lane in {"restaurant", "packaged"} and (found is None or found.get("portion_error")):
                # "McDonald's breakfast burrito with eggs and sausage": menus list the
                # item by its name, so search the place plus the dish name alone.
                core = dish_estimates.dish_name(item["description"])
                if core and core != item["description"].casefold():
                    trial = await self._lookup({**item, "description": core}, gate)
                    if trial is not None and not trial.get("portion_error"):
                        found = trial
            if lane in {"restaurant", "packaged"}:
                # Restaurant and packaged food is looked up as that place's item
                # (menu/label data, then an online estimate). Only when nothing at
                # all is found is a typical recipe used, and the receipt says so.
                if typical and (found is None or found.get("portion_error")):
                    self._accept(item, typical, place_not_found=origins.get("place") or lane)
                    return
            elif typical and (found is None or found.get("portion_error")
                              or found.get("estimate_provenance") == "server_web_estimate"):
                # No source for that amount, or only a web guess: a typical serving
                # (marked est.) instead of asking or leaving it out.
                self._accept(item, typical)
                return
            if found is not None and found.get("portion_error"):
                origins = item.get("field_origins") or {}
                item["needs"] = "portion"
                if any(origins.get(k) in {"user", "saved_usual"} for k in ("quantity", "unit", "size")) \
                        or origins.get("all") == "saved_usual":
                    # The user already gave the amount; asking "how much" again
                    # loops forever. The source can't use that amount: ask for
                    # servings once (unanswered, it is not counted).
                    amount = portion_text(item) or "that amount"
                    item["field_origins"] = {**origins, "unmeasurable": amount}
                    item["question"] = (f"I found {item['description']}, but its nutrition source can't be "
                                        f"measured as {amount}. How many servings was it?")
                else:
                    item["question"] = f"How much {item['description']} was it?"
                return
            core = generic_core(item["description"])
            if core and (found is None or found.get("estimate_provenance") == "server_web_estimate"):
                # "sticky rice with gyoza sauce" -> "sticky rice" at the same
                # portion; a sourced core beats a web guess at the whole dish.
                trial = await self._lookup({**item, "description": core[0]}, gate)
                if trial is not None and not trial.get("portion_error") and (
                        found is None or trial.get("estimate_provenance") != "server_web_estimate"):
                    self._accept(item, trial, estimate_provenance="generic_core_fallback",
                                 core_identity=core[0], core_note=f"{core[1]} not counted",
                                 **({"core_provenance": trial["estimate_provenance"]}
                                    if trial.get("estimate_provenance") else {}))
                    return
            if found is None:
                # Never invent numbers: no source means not counted, never a block.
                self._not_count(item, "no nutrition source found")
                return
            # Option A: a web estimate is included and disclosed, not held for consent.
            self._accept(item, found)

        await asyncio.gather(*(one(item) for item in pending))

    @staticmethod
    def _decide_state(draft) -> None:
        for item in draft["items"]:
            if item["status"] != "included":
                continue
            if not item.get("needs") and not item.get("resolution"):
                # Never "ready" without numbers: no numbers means not counted.
                MealDrafts._not_count(item, "no nutrition source found")
            elif draft.get("_settle", True) and MealDrafts._asked_before(item, draft.get("_turn_ref")):
                # Edited but still open for the question it already had on an
                # earlier turn (e.g. the same unusable amount again): never ask twice.
                MealDrafts._not_count(item, MealDrafts._open_reason(item))
        included = [item for item in draft["items"] if item["status"] == "included"]
        open_items = [item for item in included if item.get("needs")]
        left_out = [item for item in draft["items"] if not_counted(item)]
        if open_items:
            # One question per reply: the first open item. It is recorded as
            # asked, so a later turn without an answer never repeats it.
            first = open_items[0]
            origins = first.get("field_origins") or {}
            if origins.get("asked") != first["needs"]:
                # The turn it was first asked on; a re-statement keeps that turn.
                first["field_origins"] = {**origins, "asked": first["needs"],
                                          "asked_turn": draft.get("_turn_ref")}
            draft["state"] = "needs_detail"
            draft["pending_question"] = (first.get("question") or "What details are missing?")[:1000]
        elif included:
            draft["state"], draft["pending_question"] = "ready", None
        elif left_out:
            draft["state"] = "collecting"
            draft["pending_question"] = ("Nothing was logged: I couldn't count "
                                         + MealDrafts._not_counted_text(left_out)
                                         + f". {NOT_COUNTED_HINT.capitalize()}.")[:1000]
        else:
            draft["state"], draft["pending_question"] = "collecting", "What did you eat?"

    @staticmethod
    def _not_counted_text(items) -> str:
        return "; ".join(f"{item_text(item)} ({not_counted(item)})" for item in items)

    @staticmethod
    def _not_counted_brief(draft) -> dict[str, Any]:
        left_out = [{"item_ref": item_ref(item), "text": item_text(item), "reason": not_counted(item)}
                    for item in draft["items"] if not_counted(item)]
        return {"not_counted": left_out} if left_out else {}

    # ------------------------------------------------------------------ #
    # Commit
    # ------------------------------------------------------------------ #

    def _values(self, draft, live: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        included = [item for item in draft["items"] if item["status"] == "included"]
        components = []
        for item in included:
            resolution = dict(item["resolution"] or {})
            for key in MACROS:
                if key not in resolution and item["id"] in live:
                    resolution[key] = live[item["id"]].get(key)
            components.append({"item": item, "resolution": resolution})
        totals = {key: (None if any(c["resolution"].get(key) is None for c in components)
                        else round(sum(float(c["resolution"][key]) for c in components), 2))
                  for key in MACROS}
        name = " + ".join(self._counted_text(c["item"]) for c in components)
        if draft.get("usual_label") and all((c["item"].get("field_origins") or {}).get("all") in
                                            {"saved_usual", "saved_preset"} for c in components):
            name = f"Usual {draft['usual_label']}"
            counts = [c["item"].get("quantity") for c in components]
            if len(components) == 1 and counts[0] not in (None, 1, 1.0):
                name += f" x{float(counts[0]):g}"
        sources = [str(c["resolution"].get("macro_source") or "") for c in components]
        source = sources[0] if len(sources) == 1 else "Composite: " + "; ".join(sources)
        allow_estimate = any("estimate" in s.casefold() for s in sources) and all(
            c["resolution"].get("estimate_provenance") in ESTIMATE_PROVENANCE
            for c in components if "estimate" in str(c["resolution"].get("macro_source") or "").casefold())
        meal = draft.get("meal_type") or default_meal_type(self.clock())
        draft["meal_type"] = meal  # recorded on the committed draft by the store
        values = self.validate_values(name[:200], *(totals[k] for k in MACROS), source, meal,
                                      draft["log_date"].isoformat(), allow_estimate=allow_estimate)
        metadata = [{"description": self._counted_text(c["item"]), "source": c["resolution"].get("macro_source"),
                     "attribution": c["resolution"].get("attribution"),
                     "estimate_provenance": c["resolution"].get("estimate_provenance"),
                     **({"estimate_note": c["resolution"]["core_note"]} if c["resolution"].get("core_note") else {}),
                     "carbohydrate_definition": c["resolution"].get("carbohydrate_definition"),
                     "field_origins": c["item"].get("field_origins")} for c in components]
        left_out = [{"description": item_text(item), "reason": not_counted(item)}
                    for item in draft["items"] if not_counted(item)]
        return values, {"origin": "meal_draft", "draft_id": str(draft["id"]), "components": metadata,
                        **({"not_counted": left_out} if left_out else {})}

    @staticmethod
    def _counted_text(item) -> str:
        """What the numbers describe: the core food for a generic-core estimate."""
        core = (item.get("resolution") or {}).get("core_identity")
        text = item_text({**item, "description": core}) if core else item_text(item)
        origins = item.get("field_origins") or {}
        place = origins.get("place")
        if place and origins.get("source_kind") in {"restaurant", "packaged"} \
                and not meal_intent.names_place(item["description"], place):
            text += f" ({place})"
        return text

    async def _commit(self, draft, live: dict[str, dict[str, Any]]) -> dict[str, Any]:
        from store import DraftConflict  # local import keeps this module store-agnostic
        # Unstorable panels were refreshed before the save (handle), so `live`
        # carries every number this commit needs.
        try:
            values, metadata = self._values(draft, live)
        except (domain.MacroError, ValueError, TypeError) as exc:
            logger.warning("meal draft values rejected: %s", exc)
            return {"status": "failed", "draft_ref": str(draft["id"]),
                    "confirmation": "I couldn't put together usable numbers for that meal, so I didn't log it."}
        meal_type_origin = "user" if draft.get("meal_type") else "time_of_day"
        request_hash = hashlib.sha256(json.dumps({**values, "day": values["day"].isoformat()},
                                                 sort_keys=True, default=str).encode()).hexdigest()
        try:
            stored = await self.store.commit_meal_draft(str(draft["id"]), int(draft["revision"]),
                                                        request_hash, values, metadata)
        except DraftConflict:
            return {"status": "conflict", "draft_ref": str(draft["id"]),
                    "confirmation": "That meal changed before I could save it. Check Today, then tell me again."}
        except Exception as exc:
            if type(exc).__name__ == "CalorieOnlyWriteBlocked":
                return {"status": "needs_detail", "draft_ref": str(draft["id"]),
                        "confirmation": "Some nutrients are unknown, and I can't save unknown values yet."}
            logger.warning("meal draft commit outcome uncertain: %s", type(exc).__name__)
            # The draft stays 'ready' at the same revision: the next attempt
            # reuses the same idempotency key and replays instead of re-writing.
            return {"status": "unknown", "draft_ref": str(draft["id"]),
                    "confirmation": "The save outcome isn't verified yet. Check Today; if it's missing, say so and I'll finish this same meal."}
        replayed = bool(stored.pop("_operation_replayed", False))
        entry = stored["entry"]
        readback = await self.store.fetch_entry_revision(str(entry["id"]))
        if readback is None or int(readback["entry_revision"]) != int(entry["entry_revision"]):
            return {"status": "unknown", "draft_ref": str(draft["id"]),
                    "confirmation": "The save outcome isn't verified yet. Check Today before logging it again."}
        return self._receipt(draft, stored, readback, replayed, meal_type_origin)

    async def _recover(self, draft) -> dict[str, Any]:
        entry = await self.store.fetch_entry_revision(str(draft["committed_entry_id"]))
        if entry is None:
            return {"status": "conflict", "draft_ref": str(draft["id"]),
                    "confirmation": "That meal was logged and later removed."}
        if int(entry["entry_revision"]) != int(draft["committed_entry_revision"]):
            # The entry was amended since: this draft's items are no longer what
            # is logged, so never rebuild a receipt from them.
            return {"status": "conflict", "draft_ref": str(draft["id"]),
                    "confirmation": "That meal was changed since. Check Today for what is logged now."}
        stored = {"outcome": "committed", "day_total": None}
        return self._receipt(draft, stored, entry, True, None)

    def _receipt(self, draft, stored, entry, replayed, meal_type_origin) -> dict[str, Any]:
        included = [item for item in draft["items"] if item["status"] == "included"]
        verb = "Updated" if stored["outcome"] == "amended" else "Logged"
        macros = (f"{entry['calories']:.0f} kcal, {entry['protein']:.0f}g protein, "
                  f"{entry['carbs']:.0f}g carbs, {entry['fat']:.0f}g fat"
                  if all(entry.get(k) is not None for k in ("protein", "carbs", "fat"))
                  else f"{entry['calories']:.0f} kcal; some nutrients unknown")
        # Logged items (verified first, then estimates with their source),
        # the totals, then what was not counted.
        if draft.get("usual_label") and all((item.get("field_origins") or {}).get("all") in
                                            {"saved_usual", "saved_preset"} for item in included):
            listed = entry["name"]
        else:
            def listed_text(item) -> str:
                parts = [self._counted_text(item)]
                if (item.get("field_origins") or {}).get("preparation") == "assumed":
                    parts.append(f"(assumed {item['preparation']})")
                if estimate_label(item.get("resolution") or {}):
                    parts.append(estimate_label(item["resolution"]))
                return " ".join(parts)
            verified = [listed_text(item) for item in included if not estimate_label(item.get("resolution") or {})]
            estimated = [listed_text(item) for item in included if estimate_label(item.get("resolution") or {})]
            listed = " + ".join(verified + estimated)
        text = f"{verb} as {entry['meal']}: {listed}. Total: {macros}."
        day_total = stored.get("day_total")
        if day_total:
            text += f" Today: {day_total['calories']:.0f} kcal, {day_total['protein']:.0f}g protein."
        notes = []
        for item in included:
            origins = item.get("field_origins") or {}
            resolution = item.get("resolution") or {}
            if origins.get("preparation") == "saved_default":
                notes.append(f"used your usual {item['preparation']} weight for {item['description']}")
            # A count ("1 apple") is not a size: the standard-size assumption is disclosed.
            stated = any(origins.get(k) == "user" for k in ("unit", "size"))
            if origins.get("quantity") == "typical_serving":
                notes.append(f"{item['description']}: assumed {item['quantity']:g}, a typical serving")
            elif resolution.get("estimate_provenance") == TYPICAL:
                # Always say what a typical-serving estimate was built from, so it can be corrected.
                note = f"{item['description']}: {resolution.get('assumption')}"
                if resolution.get("place_not_found"):
                    missing = resolution["place_not_found"]
                    note = (f"{item['description']}: no published numbers found for {missing}, so this is a "
                            f"typical recipe; " + str(resolution.get("assumption")))
                if resolution.get("not_counted_parts"):
                    note += "; not counted: " + ", ".join(resolution["not_counted_parts"])
                notes.append(note)
            elif resolution.get("assumption") and not stated and origins.get("all") not in {"saved_usual", "saved_preset"}:
                notes.append(f"{item['description']}: {resolution['assumption']}")

        if notes:
            joined = "; ".join(notes)
            # Only the first letter: names like McDonald's keep their capitals.
            text += " " + joined[:1].upper() + joined[1:] + "."
        left_out = [item for item in draft["items"] if not_counted(item)]
        if left_out:
            text += f" Not counted: {self._not_counted_text(left_out)}; {NOT_COUNTED_HINT}."
        offer = None
        if draft.get("usual_label") and not all((item.get("field_origins") or {}).get("all") == "saved_usual"
                                                 for item in included):
            offer = f"Want me to remember these exact items as your usual {draft['usual_label']}?"
        return {"status": "replayed" if replayed else stored["outcome"], "draft_ref": str(draft["id"]),
                "entry_id": str(entry["id"]), "entry_revision": int(entry["entry_revision"]),
                "logged": entry, "day_total": day_total, "confirmation": text,
                **({"offer": offer} if offer else {})}

    # ------------------------------------------------------------------ #
    # Saved usual (separate from logging)
    # ------------------------------------------------------------------ #

    async def _save_usual(self, args) -> dict[str, Any]:
        spec = args["save_usual"]
        if not isinstance(spec, Mapping) or not spec.get("draft_ref"):
            raise DraftInputError("save_usual needs the draft_ref of the meal to remember")
        draft = await self.store.fetch_meal_draft(str(spec["draft_ref"]))
        if draft is None or draft["state"] not in {"ready", "committed"}:
            return {"status": "not_found", "confirmation": "I need a finished meal before I can save it as your usual."}
        label = _clean_description(spec.get("label") or draft.get("usual_label") or "")
        items = [item for item in draft["items"] if item["status"] == "included"]
        if any(item.get("kind") != "food" for item in items):
            return {"status": "needs_detail", "confirmation": "I need the actual items before I can save a usual."}
        live = {}
        for item in items:
            if (item.get("resolution") or {}).get("refresh"):
                found = await self._refresh(item)
                if found is None:
                    # A usual is the user's exact items; never save it short one.
                    return {"status": "failed",
                            "confirmation": f"I couldn't re-check the nutrition for {item_text(item)}, "
                                            "so I didn't save that usual. Try again in a moment."}
                live[item["id"]] = {k: found.get(k) for k in MACROS}
        try:
            values, _ = self._values(draft, live)
        except (domain.MacroError, ValueError, TypeError) as exc:
            logger.warning("usual values rejected: %s", exc)
            return {"status": "failed",
                    "confirmation": "I couldn't put together usable numbers for that usual, so I didn't save it."}
        saved = await self.store.save_usual(
            label=f"Usual {label}", items=items, totals={k: values[k] for k in MACROS},
            macro_source=values["macro_source"], meal=values["meal"])
        return {"status": "saved_usual", "template_revision": saved["template_revision"],
                "confirmation": f"Saved your usual {label}: " + ", ".join(item_text(i) for i in items)
                                + ". That didn't log anything new."}

    # ------------------------------------------------------------------ #

    @staticmethod
    def _summary(draft) -> dict[str, Any]:
        return {"draft_ref": str(draft["id"]), "state": draft["state"], "intent": draft["intent"],
                "revision": int(draft["revision"]),
                "items": [{"item_ref": item_ref(item), "text": item_text(item),
                           **({"needs": item["needs"]} if item.get("needs") else {})}
                          for item in draft["items"] if item["status"] == "included"],
                **MealDrafts._not_counted_brief(draft)}

    def _reply(self, draft) -> dict[str, Any]:
        summary = self._summary(draft)
        if draft["state"] == "ready":  # a plan: resolved, never logged
            left_out = [item for item in draft["items"] if not_counted(item)]
            return {"status": "planned", **summary,
                    "confirmation": "Noted, not logged: " + " + ".join(i["text"] for i in summary["items"])
                                    + ". Tell me when you've eaten it."
                                    + (f" Not counted: {self._not_counted_text(left_out)}." if left_out else "")}
        question = draft.get("pending_question") or "What did you eat?"
        return {"status": "needs_detail", **summary, "question": question, "confirmation": question}


MEAL_DRAFT_TOOL = {
    "name": "update_meal",
    "description": (
        "The ONLY food logging tool. Send what the user said as structured items; the server keeps "
        "the meal, finds nutrition, asks only for missing details, and logs it once when complete. "
        "New meal: omit draft_ref and send add. Answer to a question about an open meal: send its "
        "draft_ref and edit the item_ref the question was about (never re-add the whole meal). "
        "Correct an already logged meal ('make that two', 'actually it was cooked'): amend_ref = its "
        "draft_ref plus edit/remove. Log a repeat ('same as yesterday', 'another one'): repeat_ref. "
        "My usual X: add {description:X, usual:true}. Future/planned food: intent plan. "
        "Unknown foods are estimated by the server (marked est.) or left out as not counted; they never "
        "block the meal. A dish with its contents is ONE item: put the contents in its description "
        "('breakfast burrito with eggs, sausage and cheese'), small/medium/large in size; the server "
        "estimates a typical serving of that recipe. Separate foods are separate items. "
        "Tag where each food came from: source restaurant (+ place, e.g. McDonald's) for restaurant, "
        "fast-food or takeout food; packaged (+ place = its maker) for a store-bought product; homemade for "
        "home cooking or a generic dish. Infer it from the whole conversation; omit if unclear. Open meal lanes: log_now:true logs what is ready now ('log it', 'log what you "
        "have', 'whatever', 'estimate it', 'that's fine'); only:[item_ref] for 'just log X' or 'only the X' "
        "(everything else is removed); skip:[item_ref] for 'skip X' or 'leave X out'. "
        "Never send macro numbers. Examples: "
        '{"add":[{"description":"93/7 ground beef","quantity":3,"unit":"oz"},{"description":"green apple","quantity":1}]} '
        '{"add":[{"description":"eggs","source":"homemade"},{"description":"breakfast burrito with sausage and cheese","size":"large","source":"homemade"}]} '
        '{"add":[{"description":"Big Mac","source":"restaurant","place":"McDonald\'s"},{"description":"medium fries","source":"restaurant","place":"McDonald\'s"}]} '
        '{"draft_ref":"<ref>","edit":[{"item_ref":"item1","preparation":"raw"}]} '
        '{"draft_ref":"<ref>","log_now":true} '
        '{"add":[{"description":"chick fil a","usual":true}]}'),
    "input_schema": {"type": "object", "properties": {
        "draft_ref": {"type": "string", "maxLength": 64},
        "amend_ref": {"type": "string", "maxLength": 64},
        "repeat_ref": {"type": "string", "maxLength": 64},
        "intent": {"type": "string", "enum": ["consume", "plan"]},
        "meal_type": {"type": "string", "enum": list(domain.MEALS)},
        "add": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "object", "properties": {
            "description": {"type": "string", "minLength": 1, "maxLength": 200},
            "quantity": {"type": "number", "exclusiveMinimum": 0, "maximum": 5000},
            "unit": {"type": "string", "maxLength": 20},
            "size": {"type": "string", "enum": ["small", "medium", "large"]},
            "preparation": {"type": "string", "enum": ["raw", "cooked"]},
            "source": {"type": "string", "enum": ["restaurant", "packaged", "homemade"]},
            "place": {"type": "string", "minLength": 1, "maxLength": 60},
            "usual": {"type": "boolean"}},
            "required": ["description"], "additionalProperties": False}},
        "edit": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "object", "properties": {
            "item_ref": {"type": "string", "maxLength": 16},
            "description": {"type": "string", "minLength": 1, "maxLength": 200},
            "quantity": {"type": "number", "exclusiveMinimum": 0, "maximum": 5000},
            "unit": {"type": "string", "maxLength": 20},
            "size": {"type": "string", "enum": ["small", "medium", "large"]},
            "preparation": {"type": "string", "enum": ["raw", "cooked"]},
            "source": {"type": "string", "enum": ["restaurant", "packaged", "homemade"]},
            "place": {"type": "string", "minLength": 1, "maxLength": 60}},
            "required": ["item_ref"], "additionalProperties": False}},
        "remove": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "string", "maxLength": 16}},
        "only": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "string", "maxLength": 16}},
        "skip": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "string", "maxLength": 16}},
        "log_now": {"type": "boolean"},
        "consent_estimate": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "string", "maxLength": 16}},
        "remember_preparation": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
            "item_ref": {"type": "string", "maxLength": 16},
            "preparation": {"type": "string", "enum": ["raw", "cooked"]}},
            "required": ["item_ref", "preparation"], "additionalProperties": False}},
        "save_usual": {"type": "object", "properties": {
            "draft_ref": {"type": "string", "maxLength": 64},
            "label": {"type": "string", "minLength": 1, "maxLength": 80}},
            "required": ["draft_ref"], "additionalProperties": False},
        "cancel": {"type": "boolean"}},
        "additionalProperties": False},
}

MEAL_DRAFT_INSTRUCTIONS = """FOOD: use update_meal for anything the user ate, is eating or plans to eat.
Send their words as items (description, quantity, unit, size, preparation); never translate food
into search terms and never send nutrition numbers. Keep identity words like 93/7, brands and flavors.
Call update_meal at once; do not ask your own food questions first. If it returns a question, ask
exactly that question (the server sends its question or receipt as the reply). When the user answers,
call update_meal with the SAME draft_ref and edit only the item_ref the question named.
The server estimates foods it can't verify (marked est. with the source) and leaves out anything it
has no source for as "not counted"; neither blocks the meal and you never ask about them yourself.
No measurements is fine: never ask for weights, sizes or amounts. A dish and what was in it is ONE
item with the contents in its description ("salad with bacon and cheese", "breakfast burrito with
eggs, sausage and cheese"), plus size small/medium/large only if the user said it; the server
estimates a typical serving of that recipe and says what it assumed. To correct a dish ("it also had
bacon", "it was a large one"), edit that item's description or size.
WHERE IT CAME FROM (set on every food you can, from the whole conversation; never ask the user):
- source "restaurant" + place: ordered at a restaurant, chain or takeout ("Big Mac", "a Chipotle
  bowl", "I'm at Chick-fil-A" earlier then "nuggets and fries"). The server looks up that menu item.
- source "packaged" + place = its maker: a store-bought product ("Kodiak waffles", "Chobani yogurt").
- source "homemade": cooked at home or a generic dish with no place ("I made a burrito", "salad with
  bacon and cheese", "eggs"). The server builds it from a typical recipe instead of searching online.
- Not sure: omit source. "Actually it was from Panera" -> edit that item with source and place.
Lanes for an open meal, always with its SAME draft_ref:
- "log it", "log what you have", "that's it" -> log_now:true (open items become not counted).
- "just log X", "only the X", "skip the rest" -> only:[X's item_ref] (every other item is removed).
- "remove X", "I didn't have the X" -> remove:[that item_ref].
- "skip X", "leave X out", "forget the X" -> skip:[that item_ref].
- "any brand", "whatever", "estimate it", "that's fine", "close enough" about an open item ->
  log_now:true (the server already uses any estimate it has).
- To add a food that was not counted, describe it: add it (open meal) or amend_ref + add (logged meal). MEAL CONTEXT below lists open meals and today's/yesterday's logged
meals with their refs; use it for short answers and corrections. A correction to a logged meal uses
amend_ref (it changes that one entry, never adds a second); "another one"/"same as yesterday" uses
repeat_ref (a NEW entry). If several meals fit a reference, ask which one. Only a clear statement
like "I always weigh it cooked" uses remember_preparation; "cooked this time" is just an edit.
If update_meal returns status unknown, do NOT send add again: ask the user to check Today; to
recover, call update_meal with only that same draft_ref. Never log the same meal twice in one turn.
consent_estimate is only for an older meal still waiting on a web estimate. save_usual only after the user
explicitly says yes to remembering a usual. Report only the returned confirmation or question,
plus any returned offer as a question. Never claim a meal was logged unless status is committed,
amended or replayed. For a nutrition question that is not about something eaten, use lookup_food."""
