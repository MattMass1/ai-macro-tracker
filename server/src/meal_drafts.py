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

import domain
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

Resolver = Callable[[Mapping[str, Any]], Awaitable[dict[str, Any] | None]]


class DraftInputError(ValueError):
    """The model sent an edit the server cannot apply. Nothing changed."""


def _fingerprint(resolution: Mapping[str, Any]) -> str:
    material = {k: resolution.get(k) for k in (*MACROS, "macro_source")}
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _clean_description(text: Any) -> str:
    value = " ".join(str(text or "").split()).strip(" .,")
    value = _LEADING_FILLER.sub("", value).strip()
    if not 1 <= len(value) <= 160:
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
    description = item["description"]
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
        "carbohydrate_definition") if resolution.get(key) is not None}
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
                 unresolved_question: Callable[[str], Awaitable[str]],
                 validate_values: Callable[..., dict[str, Any]],
                 clock: Callable[[], datetime] | None = None):
        self.store = store
        self.resolve = resolve
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
                    **({"question": draft["pending_question"]} if draft.get("pending_question") else {})}
        return {"open_meals": [brief(d) for d in found["open"]],
                "logged_meals": [brief(d) for d in found["committed"]]}

    # ------------------------------------------------------------------ #
    # The one tool
    # ------------------------------------------------------------------ #

    async def handle(self, args: Mapping[str, Any], *, session_ref: str | None = None) -> dict[str, Any]:
        allowed = {"draft_ref", "amend_ref", "repeat_ref", "intent", "meal_type", "add", "edit",
                   "remove", "consent_estimate", "remember_preparation", "save_usual", "cancel"}
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
        if draft["state"] == "committed" and set(args) == {"draft_ref"}:
            # Recovery after an uncertain save: report what was written.
            return await self._recover(draft)
        if draft["state"] not in OPEN_STATES:
            return {"status": "conflict", "draft_ref": str(draft["id"]),
                    "confirmation": "That meal is already logged. To change it, use amend_ref."}

        if not draft.pop("_edits_applied", False):
            await self._apply_edits(draft, args)
        await self._resolve_items(draft)
        self._decide_state(draft)
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
                # Consent covers one logged meal, not future repeats; unlicensed
                # panels were never stored, so they are fetched again.
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
        item["field_origins"] = origins
        self._invalidate(item)

    async def _apply_edits(self, draft, args) -> None:
        if args.get("intent"):
            if args["intent"] not in {"consume", "plan"}:
                raise DraftInputError("intent must be consume or plan")
            draft["intent"] = args["intent"]
        if args.get("meal_type"):
            draft["meal_type"] = domain.normalize_meal(args["meal_type"])
            draft["meal_type_origin"] = "user"
        for ref in args.get("remove") or []:
            self._item(draft, ref)["status"] = "removed"
        for spec in args.get("edit") or []:
            if not isinstance(spec, Mapping) or "item_ref" not in spec:
                raise DraftInputError("Each edit needs an item_ref")
            fields = {k: v for k, v in spec.items() if k != "item_ref"}
            if not fields or set(fields) - {"description", "quantity", "unit", "size", "preparation"}:
                raise DraftInputError("Edits may change description, quantity, unit, size or preparation")
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
            if set(spec) - {"description", "quantity", "unit", "size", "preparation", "usual"}:
                raise DraftInputError("Added foods take description, quantity, unit, size, preparation, usual")
            if spec.get("usual"):
                await self._add_usual(draft, spec)
                continue
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

    async def _resolve_items(self, draft) -> None:
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

        async def one(item):
            if needs_weight_basis(item):
                item["needs"] = "preparation"
                item["question"] = f"Was that {portion_text(item)} of {item['description']} weighed raw or cooked?"
                return
            async with gate:
                try:
                    found = await self.resolve(_food_query(item))
                except Exception as exc:  # provider failure keeps the item, never the meal
                    logger.warning("meal draft item resolution failed: %s", type(exc).__name__)
                    found = None
            if found is not None and found.get("portion_error"):
                origins = item.get("field_origins") or {}
                if any(origins.get(k) in {"user", "saved_usual"} for k in ("quantity", "unit", "size")) \
                        or origins.get("all") == "saved_usual":
                    # The user already gave the amount; asking "how much" again
                    # loops forever. The source can't use that amount, so say so
                    # and ask for something that can change the lookup.
                    item["needs"] = "identity"
                    item["question"] = (f"I found {item['description']}, but its nutrition source can't be "
                                        f"measured as {portion_text(item) or 'that amount'}. Is it a specific "
                                        "brand or dish, or how many servings was it?")
                else:
                    item["needs"] = "portion"
                    item["question"] = f"How much {item['description']} was it?"
                return
            basis = str((found or {}).get("basis") or "").casefold()
            if found is not None and item.get("unit") in MASS_UNITS and (
                    (item.get("preparation") == "cooked" and "raw" in basis)
                    or (item.get("preparation") == "raw" and "cooked" in basis)):
                found = None  # a weight is only priced with a panel of the same basis
            if found is None:
                item["needs"] = "identity"
                item["question"] = await self.unresolved_question(item_text(item))
                return
            previous_consent = item.get("estimate_consent")
            item["resolution"] = _persistable(found)
            # Keep live numbers for this turn even when they may not be stored.
            item["_live"] = {k: found.get(k) for k in MACROS}
            item["needs"] = item["question"] = None
            if found.get("estimate_provenance") == "server_web_estimate":
                if previous_consent != item["resolution"]["fingerprint"]:
                    item["estimate_consent"] = None
                    item["needs"] = "estimate_consent"
                    calories = found.get("calories")
                    host = (found.get("attribution") or {}).get("source_host") if isinstance(found.get("attribution"), Mapping) else None
                    item["question"] = (f"Closest I found for {item_text(item)}: about "
                                        f"{round(float(calories)) if calories is not None else 'unknown'} calories "
                                        f"(web estimate{', ' + host if host else ''}). Use it?")

        await asyncio.gather(*(one(item) for item in pending))

    @staticmethod
    def _decide_state(draft) -> None:
        included = [item for item in draft["items"] if item["status"] == "included"]
        for item in included:
            if not item.get("needs") and not item.get("resolution"):
                # Never "ready" without numbers.
                item["needs"] = "identity"
                item["question"] = item.get("question") or f"What exactly was the {item['description']}?"
        open_items = [item for item in included if item.get("needs")]
        if not included:
            draft["state"], draft["pending_question"] = "collecting", "What did you eat?"
        elif open_items:
            draft["state"] = "needs_detail"
            # Independent questions are asked together; one answer can settle all.
            questions = list(dict.fromkeys(item["question"] for item in open_items if item.get("question")))
            draft["pending_question"] = " ".join(questions)[:1000] or "What details are missing?"
        else:
            draft["state"], draft["pending_question"] = "ready", None

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
        name = " + ".join(item_text(c["item"]) for c in components)
        if draft.get("usual_label") and all((c["item"].get("field_origins") or {}).get("all") in
                                            {"saved_usual", "saved_preset"} for c in components):
            name = f"Usual {draft['usual_label']}"
            counts = [c["item"].get("quantity") for c in components]
            if len(components) == 1 and counts[0] not in (None, 1, 1.0):
                name += f" x{float(counts[0]):g}"
        sources = [str(c["resolution"].get("macro_source") or "") for c in components]
        source = sources[0] if len(sources) == 1 else "Composite: " + "; ".join(sources)
        allow_estimate = any("estimate" in s.casefold() for s in sources) and all(
            c["resolution"].get("estimate_provenance") in {"curated_generic_assumption", "server_web_estimate"}
            for c in components if "estimate" in str(c["resolution"].get("macro_source") or "").casefold())
        meal = draft.get("meal_type") or default_meal_type(self.clock())
        draft["meal_type"] = meal  # recorded on the committed draft by the store
        values = self.validate_values(name[:200], *(totals[k] for k in MACROS), source, meal,
                                      draft["log_date"].isoformat(), allow_estimate=allow_estimate)
        metadata = [{"description": item_text(c["item"]), "source": c["resolution"].get("macro_source"),
                     "attribution": c["resolution"].get("attribution"),
                     "estimate_provenance": c["resolution"].get("estimate_provenance"),
                     "carbohydrate_definition": c["resolution"].get("carbohydrate_definition"),
                     "field_origins": c["item"].get("field_origins")} for c in components]
        return values, {"origin": "meal_draft", "draft_id": str(draft["id"]), "components": metadata}

    async def _commit(self, draft, live: dict[str, dict[str, Any]]) -> dict[str, Any]:
        from store import DraftConflict  # local import keeps this module store-agnostic
        live = dict(live)
        # Freshly loaded drafts lose `_live`; refetch any panel we may not store.
        refresh = [item for item in draft["items"] if item["status"] == "included"
                   and (item.get("resolution") or {}).get("refresh") and not live.get(item["id"])]
        for item in refresh:
            try:
                found = await self.resolve(_food_query(item))
            except Exception as exc:
                logger.warning("meal draft refresh failed: %s", type(exc).__name__)
                found = None
            if not found or found.get("portion_error") or _fingerprint(found) != item["resolution"]["fingerprint"]:
                return {"status": "needs_detail", "draft_ref": str(draft["id"]),
                        "question": f"The nutrition source for {item_text(item)} changed. Tell me again and I'll re-check it.",
                        "confirmation": f"The nutrition source for {item_text(item)} changed. Tell me again and I'll re-check it."}
            live[item["id"]] = {k: found.get(k) for k in MACROS}
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
        stored = {"outcome": "committed", "day_total": None}
        return self._receipt(draft, stored, entry, True, None)

    def _receipt(self, draft, stored, entry, replayed, meal_type_origin) -> dict[str, Any]:
        included = [item for item in draft["items"] if item["status"] == "included"]
        verb = "Updated" if stored["outcome"] == "amended" else "Logged"
        macros = (f"{entry['calories']:.0f} kcal, {entry['protein']:.0f}g protein, "
                  f"{entry['carbs']:.0f}g carbs, {entry['fat']:.0f}g fat"
                  if all(entry.get(k) is not None for k in ("protein", "carbs", "fat"))
                  else f"{entry['calories']:.0f} kcal; some nutrients unknown")
        text = f"{verb} {entry['name']} as {entry['meal']}: {macros}."
        notes = []
        for item in included:
            origins = item.get("field_origins") or {}
            resolution = item.get("resolution") or {}
            if origins.get("preparation") == "saved_default":
                notes.append(f"used your usual {item['preparation']} weight for {item['description']}")
            # A count ("1 apple") is not a size: the standard-size assumption is disclosed.
            stated = any(origins.get(k) == "user" for k in ("unit", "size"))
            if resolution.get("assumption") and not stated and origins.get("all") not in {"saved_usual", "saved_preset"}:
                notes.append(f"{item['description']}: {resolution['assumption']}")
            if resolution.get("estimate_provenance") == "server_web_estimate":
                notes.append(f"{item['description']} is a web estimate")
        if notes:
            text += " " + "; ".join(notes).capitalize() + "."
        day_total = stored.get("day_total")
        if day_total:
            text += f" Today: {day_total['calories']:.0f} kcal, {day_total['protein']:.0f}g protein."
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
                found = await self.resolve(_food_query(item))
                live[item["id"]] = {k: (found or {}).get(k) for k in MACROS}
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
                          for item in draft["items"] if item["status"] == "included"]}

    def _reply(self, draft) -> dict[str, Any]:
        summary = self._summary(draft)
        if draft["state"] == "ready":  # a plan: resolved, never logged
            return {"status": "planned", **summary,
                    "confirmation": "Noted, not logged: " + " + ".join(i["text"] for i in summary["items"])
                                    + ". Tell me when you've eaten it."}
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
        "Never send macro numbers. Examples: "
        '{"add":[{"description":"93/7 ground beef","quantity":3,"unit":"oz"},{"description":"green apple","quantity":1}]} '
        '{"draft_ref":"<ref>","edit":[{"item_ref":"item1","preparation":"raw"}]} '
        '{"add":[{"description":"chick fil a","usual":true}]}'),
    "input_schema": {"type": "object", "properties": {
        "draft_ref": {"type": "string", "maxLength": 64},
        "amend_ref": {"type": "string", "maxLength": 64},
        "repeat_ref": {"type": "string", "maxLength": 64},
        "intent": {"type": "string", "enum": ["consume", "plan"]},
        "meal_type": {"type": "string", "enum": list(domain.MEALS)},
        "add": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "object", "properties": {
            "description": {"type": "string", "minLength": 1, "maxLength": 160},
            "quantity": {"type": "number", "exclusiveMinimum": 0, "maximum": 5000},
            "unit": {"type": "string", "maxLength": 20},
            "size": {"type": "string", "enum": ["small", "medium", "large"]},
            "preparation": {"type": "string", "enum": ["raw", "cooked"]},
            "usual": {"type": "boolean"}},
            "required": ["description"], "additionalProperties": False}},
        "edit": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "object", "properties": {
            "item_ref": {"type": "string", "maxLength": 16},
            "description": {"type": "string", "minLength": 1, "maxLength": 160},
            "quantity": {"type": "number", "exclusiveMinimum": 0, "maximum": 5000},
            "unit": {"type": "string", "maxLength": 20},
            "size": {"type": "string", "enum": ["small", "medium", "large"]},
            "preparation": {"type": "string", "enum": ["raw", "cooked"]}},
            "required": ["item_ref"], "additionalProperties": False}},
        "remove": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "string", "maxLength": 16}},
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
exactly that question. When the user answers, call update_meal with the SAME draft_ref and edit only
the item_ref the question named. MEAL CONTEXT below lists open meals and today's/yesterday's logged
meals with their refs; use it for short answers and corrections. A correction to a logged meal uses
amend_ref (it changes that one entry, never adds a second); "another one"/"same as yesterday" uses
repeat_ref (a NEW entry). If several meals fit a reference, ask which one. Only a clear statement
like "I always weigh it cooked" uses remember_preparation; "cooked this time" is just an edit.
If update_meal returns status unknown, do NOT send add again: ask the user to check Today; to
recover, call update_meal with only that same draft_ref. Never log the same meal twice in one turn.
consent_estimate only after the user agrees to that web estimate. save_usual only after the user
explicitly says yes to remembering a usual. Report only the returned confirmation or question,
plus any returned offer as a question. Never claim a meal was logged unless status is committed,
amended or replayed. For a nutrition question that is not about something eaten, use lookup_food."""
