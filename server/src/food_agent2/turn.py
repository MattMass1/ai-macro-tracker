"""The coordinator: interpret, apply, resolve, then preview or ask or commit.

A turn runs in that order, and the rules that matter live here:

* **Questions come from code.** Only two exist: how much, and raw or cooked on a
  weighed food. Each has a key, and a key in ``Waiting.asked`` is never asked again.
  Whatever is still unknown on a later turn is named as not counted.
* **Ready food never waits.** An unknown food stays in the draft unresolved and is
  named in the reply. The rest previews and commits. A slow web estimate only
  holds the reply when nothing else is ready to show.
* **Commit authority is code.** Only the user's own plain yes
  (:mod:`food_agent.confirm`), to the preview shown last in this session, with the
  stored revision and a recomputed content hash both matching.
* **Writes are one operation.** An intent is stored before the commit, the commit
  is idempotent by draft revision, the row is read back, and the receipt comes from
  that row. An unverified outcome is reconciled by the same key on the next turn
  and is never sent again as a new write.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import domain
from food_agent import confirm
from food_agent.contracts import (MASS_UNITS, NUTRIENT_KEYS, VOLUME_UNITS, DraftItemState, FoodIdentity, NutrientPanel,
                                  Portion, Preview, PreviewLine, Receipt, ResolvedFood, ToolOutcome, content_hash,
                                  sum_panels)
from food_agent.drafts import (AgentDraft, CommitIntent, CommitPlan, DraftCommitBlocked, DraftCommitConflict,
                               DraftNotReady, DraftPort, build_commit, identity_text, item_label, new_draft, op_key,
                               request_hash_of)
from food_agent.find_food import is_estimate
from food_agent.modifications import adjustment_note
from food_agent.portion import multiplier as portion_multiplier
from food_agent.portion import parse_serving_text
from food_agent.ports import MealRecaller
from food_agent.recall import _phrase
from food_agent.source_policy import attribution_texts
from food_agent2 import match
from food_agent2.contracts import Supplied, Turn, Waiting, only_stated
from food_agent2.retrieval import Found, Retrieval, Scope, user_words

logger = logging.getLogger(__name__)

EDIT_TYPES = frozenset({"add_items", "revise_item", "remove_item", "recall_meal", "settle"})
EDITABLE_STATES = ("collecting", "needs_detail")
RECENT_WINDOW = timedelta(hours=24)
NEW_MEAL_AFTER = timedelta(minutes=20)
MAX_DRAFT_ITEMS = 24
WEB_SOFT_SECONDS = 4.0
SHORT_REPLY_WORDS = 4
REPLY_LIMIT = 1000
COULD_NOT_VERIFY = ("I could not verify it saved. I'll check it first thing on your next message, "
                    "so don't log it again.")
TOO_LONG = "This meal is too large to show completely. Please split it into smaller meals before confirming."
CANNOT_READ = "Sorry, I couldn't read that. Can you say it again?"
ADD_IT = "Tell me what it was to add it."


@dataclass(frozen=True)
class TurnResult:
    """One turn's result. Same shape the canvas reads from any food agent."""

    reply: str
    handed_off: bool
    preview: Preview | None
    receipt: Receipt | None
    outcome: ToolOutcome
    draft_id: str | None = None
    question: str | None = None
    shown_draft_ids: tuple[str, ...] = ()
    quick_replies: tuple[str, ...] = ()


def _result(reply: str, status: str = "ok", effect: str = "not_attempted", **kw: Any) -> TurnResult:
    return TurnResult(reply=reply, handed_off=kw.pop("handed_off", False), preview=kw.pop("preview", None),
                      receipt=kw.pop("receipt", None),
                      outcome=ToolOutcome(status=status, effect=effect, safe_message=reply[:300] or status), **kw)


def _fmt(value: float) -> str:
    return f"{round(float(value), 1):g}"


def nutrient_text(panel: NutrientPanel) -> str:
    """Protein first. Unknown is said as unknown, never 0."""
    parts = []
    for key, unit in (("protein", " g protein"), ("calories", " kcal"), ("carbs", " g carbs"), ("fat", " g fat")):
        value = getattr(panel, key)
        parts.append(f"{key} unknown" if value is None else f"{_fmt(value)}{unit}")
    if panel.fiber is None:
        parts.append("fiber unknown")
    elif panel.fiber > 0:
        parts.append(f"{_fmt(panel.fiber)} g fiber")
    return ", ".join(parts)


def _join(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _sentence(note: str) -> str:
    return f"{note[0].upper()}{note[1:]}."


def is_ready(item: DraftItemState, unknown_ok: bool) -> bool:
    """Counted in this meal. Unknown nutrients only count where they can be saved as unknown."""
    if item.resolved is None or item.multiplier is None:
        return False
    panel = item.resolved.panel
    if panel.calories is None:
        return False
    return unknown_ok or all(getattr(panel, key) is not None for key in NUTRIENT_KEYS)


def portion_choices(basis: Any) -> tuple[str, ...]:
    """Typical amounts for a "how much?" question, in words the interpreter already reads."""
    if basis.basis == "per_100ml" or basis.unit in VOLUME_UNITS or getattr(basis, "ml", None):
        return ("8 fl oz", "12 fl oz", "16 fl oz")
    if basis.basis == "per_100g" or basis.unit in MASS_UNITS or basis.grams:
        return ("4 oz", "6 oz", "8 oz")
    return ("Half", "1 serving", "2 servings")


def _same_values(row: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    if str(row.get("name")) != str(values["name"]):
        return False
    for key in ("calories", "protein", "carbs", "fat", "fiber"):
        saved, sent = row.get(key), values.get(key)
        if (saved is None) != (sent is None):
            return False
        if saved is not None and abs(float(saved) - float(sent)) > 0.01:
            return False
    return True


def _default_meal_type(now: datetime) -> str:
    from meal_drafts import default_meal_type  # noqa: PLC0415 (the existing slot rule)
    return default_meal_type(now)


def merge_supplied(item: DraftItemState, supplied: Supplied, source: ResolvedFood | None,
                   unknown_ok: bool) -> DraftItemState:
    """The user's numbers win. Any gap stays unknown, or where unknown can't be saved comes from the
    matched source, scaled so calories stay the user's."""
    values: dict[str, float | None] = dict(supplied.given())
    filled = False
    missing = [key for key in NUTRIENT_KEYS if key not in values]
    if source is not None and source.panel.calories and missing and (not unknown_ok or "calories" in missing):
        amount = item.multiplier or 1.0
        base = {key: None if getattr(source.panel, key) is None else getattr(source.panel, key) * amount
                for key in NUTRIENT_KEYS}
        stated = values.get("calories")
        ratio = stated / base["calories"] if stated and base["calories"] else 1.0
        for key in missing:
            if base[key] is not None:
                values[key] = round(base[key] * ratio, 1)
                filled = True
    name = source.display_name if source is not None else user_words(item.identity)
    origin = f"User-supplied: {name}"
    if filled and source is not None:
        origin += f"; gaps from {source.macro_source}"
    digest = hashlib.sha256(f"{name}|{sorted(values.items())}".encode()).hexdigest()[:24]
    food = ResolvedFood(
        food_id=f"user:{digest}", provider="user", display_name=name[:200], macro_source=origin[:200],
        serving_basis=parse_serving_text(item.portion.text or "1 serving"),
        panel=NutrientPanel(**{key: values.get(key) for key in NUTRIENT_KEYS}),
        cache_allowed=(not filled) or source is None or source.cache_allowed,
        attribution={"supplied": supplied.given(), "filled": filled,
                     **({"matched_name": source.display_name} if filled and source is not None else {})})
    return item.model_copy(update={"resolved": food, "multiplier": 1.0})


@dataclass
class Plan:
    """Everything one turn wants done to a draft."""

    settle: bool = False
    use_web: bool = False
    supplied: dict[str, Supplied] | None = None
    forced: tuple[str, str] | None = None  # (question key, question text) from a recall miss
    skip: frozenset[str] = frozenset()     # refs left unresolved this turn
    notes: tuple[str, ...] = ()


class FoodAgent2:
    """``FoodAgent2(drafts, interpret, retrieval, recaller, clock)``; call :meth:`turn` once per user message."""

    def __init__(self, drafts: DraftPort, interpret: Any, retrieval: Retrieval, recaller: MealRecaller | None = None,
                 clock: Callable[[], datetime] | None = None, *, new_meal_after: timedelta = NEW_MEAL_AFTER,
                 recent_window: timedelta = RECENT_WINDOW, web_soft_seconds: float = WEB_SOFT_SECONDS):
        self.drafts = drafts
        self.interpret = interpret
        self.retrieval = retrieval
        self.recaller = recaller
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.new_meal_after = new_meal_after
        self.recent_window = recent_window
        self.web_soft_seconds = web_soft_seconds

    # ------------------------------------------------------------ entry point

    async def turn(self, message: str, *, session_ref: str | None = None, turn_ref: str | None = None) -> TurnResult:
        now = self.clock()
        decision = confirm.classify(message)
        try:
            recent = await self.drafts.recent(now - self.recent_window)
        except Exception as exc:  # noqa: BLE001 - the store is unreachable: claim nothing
            logger.warning("food agent could not read drafts: %s", type(exc).__name__)
            return _result("I couldn't reach your log just now. Try again in a moment.", "error", "unknown")
        shown = [d for d in recent if d.state in EDITABLE_STATES and Waiting.read(d.pending).stage == "preview"]
        # A preview is confirmable only if no user turn came after it. The marker is durable and
        # written first, so this holds even if everything after it fails.
        try:
            live = [d for d in shown if not await self.drafts.is_seen(d.id, d.revision)]
            for draft in live:
                await self.drafts.mark_seen(draft.id, draft.revision)
        except Exception as exc:  # noqa: BLE001
            logger.warning("food agent could not record the last shown preview: %s", type(exc).__name__)
            return _result("I couldn't reach your log just now, so nothing changed. Try again in a moment.",
                           "error", "not_applied")
        result: TurnResult | None = None
        try:
            result = await self._turn(message, decision, recent, now, session_ref, {d.id for d in live})
            return await self._with_quick_replies(result)
        finally:
            await self._retire(shown, result)

    async def _retire(self, shown: list[AgentDraft], result: TurnResult | None) -> None:
        """A preview stays confirmable only if this reply showed it. Its asked list is kept."""
        keep = {result.preview.draft_id} if result is not None and result.preview is not None else set()
        for draft in shown:
            if draft.id in keep:
                continue
            state = Waiting.read(draft.pending)
            idle = Waiting(asked=state.asked, assumed=state.assumed, revision=draft.revision).to_pending()
            try:
                await self.drafts.save(draft.with_(state="collecting", pending=idle), draft.revision)
            except Exception as exc:  # noqa: BLE001 - safe: the seen marker already retired it
                logger.warning("food agent could not clear a preview: %s", type(exc).__name__)

    async def _with_quick_replies(self, result: TurnResult) -> TurnResult:
        """One-tap answers. A tap is an ordinary message. Any failure just means no buttons."""
        try:
            if result.handed_off or result.outcome.status in {"error", "conflict", "unknown"}:
                return result
            if result.preview is not None:
                first = "Save it" if result.reply.endswith("Save the change?") else "Log it"
                return replace(result, quick_replies=(first, "Cancel"))
            if not result.question or not result.draft_id:
                return result
            if result.question.endswith("raw or cooked?"):
                return replace(result, quick_replies=("Raw", "Cooked"))
            draft = await self.drafts.get(result.draft_id)
            item = next((i for i in draft.items if i.resolved is not None and i.multiplier is None), None) \
                if draft is not None else None
            if item is not None:
                return replace(result, quick_replies=(*portion_choices(item.resolved.serving_basis), "Cancel"))
        except Exception as exc:  # noqa: BLE001
            logger.info("food agent quick replies skipped: %s", type(exc).__name__)
        return result

    async def _turn(self, message: str, decision: str, recent: list[AgentDraft], now: datetime,
                    session_ref: str | None, live: set[str]) -> TurnResult:
        try:
            reconciled = await self._reconcile(now, decision, session_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("food agent could not reconcile: %s", type(exc).__name__)
            return _result("I couldn't reach your log just now. Try again in a moment.", "error", "unknown")
        if reconciled is None:
            return await self._handle(message, decision, recent, now, session_ref, live)
        if decision != "other" or reconciled.outcome.effect == "unknown":
            return reconciled  # "yes" again after "could not verify": the check is the answer
        try:
            recent = await self.drafts.recent(now - self.recent_window)
        except Exception:  # noqa: BLE001
            return reconciled
        rest = await self._handle(message, decision, recent, now, session_ref, live)
        combined = " ".join(part for part in (reconciled.reply, rest.reply) if part)
        if rest.preview is not None and len(combined) > REPLY_LIMIT:
            return rest
        return replace(rest, reply=combined[:REPLY_LIMIT], receipt=reconciled.receipt,
                       outcome=rest.outcome if rest.outcome.effect != "not_attempted" else reconciled.outcome)

    async def _handle(self, message: str, decision: str, recent: list[AgentDraft], now: datetime,
                      session_ref: str | None, live: set[str]) -> TurnResult:
        open_ = sorted((d for d in recent if d.state in EDITABLE_STATES),
                       key=lambda d: Waiting.read(d.pending).stage == "idle")
        showing = [d for d in open_ if Waiting.read(d.pending).stage == "preview"]
        bound = [d for d in showing if d.id in live and Waiting.read(d.pending).session_ref == session_ref]
        elsewhere = [d for d in showing if Waiting.read(d.pending).session_ref != session_ref]
        if decision == "yes" and bound:
            return await self._confirm(bound[0], now, session_ref)
        if decision == "yes" and elsewhere:  # shown in another session: show it here, never commit
            return await self._advance(elsewhere[0], False, list(elsewhere[0].items), Plan(), now, session_ref, recent)
        if decision == "no" and bound:
            return await self._cancel(bound[0])

        try:
            turn = await self.interpret(message, self._context(open_, recent, now))
            if not isinstance(turn, Turn):
                raise TypeError("interpreter returned something else")
        except Exception as exc:  # noqa: BLE001
            logger.warning("food agent interpreter failed: %s", type(exc).__name__)
            if not open_:
                return _result("", "error", handed_off=True)
            if not open_[0].items:
                return _result(CANNOT_READ, "error")
            # Show the meal again, never a repeated apology: a plain yes still works while the model is down.
            return await self._advance(open_[0], False, list(open_[0].items), Plan(), now, session_ref, recent)

        actions = list(turn.actions)
        types = {action.type for action in actions}
        current = open_[0] if open_ else None
        if "not_food" in types:
            stage = Waiting.read(current.pending).stage if current is not None else "idle"
            if current is not None and (stage == "question" or decision != "other"
                                        or len(message.split()) <= SHORT_REPLY_WORDS):
                # A reply to the open meal, even an unhelpful one, never leaves for the old path.
                return await self._advance(current, False, list(current.items), Plan(), now, session_ref, recent)
            return _result("", handed_off=True)
        if "cancel" in types and not EDIT_TYPES & types:
            return await self._cancel(current) if current is not None else _result("There's nothing to cancel.")
        if EDIT_TYPES & types:
            return await self._edit(actions, message, open_, recent, now, session_ref)
        if current is not None and current.items:
            return await self._advance(current, False, list(current.items), Plan(), now, session_ref, recent)
        return _result("There's nothing waiting to log right now.")

    # ------------------------------------------------------------ context

    @staticmethod
    def _item_context(item: DraftItemState) -> dict[str, Any]:
        return {"item_ref": item.item_ref, "identity": item.identity.model_dump(mode="json"),
                "portion": item.portion.model_dump(mode="json"),
                "modifications": [m.model_dump(mode="json") for m in item.modifications],
                "matched": item.resolved.display_name if item.resolved else None}

    def _context(self, open_: list[AgentDraft], recent: list[AgentDraft], now: datetime) -> dict[str, Any]:
        """Only the current user's own drafts (the store is owner-scoped)."""
        last = next((d for d in recent if d.state == "committed"), None)
        asking = next((d for d in open_ if Waiting.read(d.pending).question), None)
        return {
            "now": now.isoformat(),
            "last_question": Waiting.read(asking.pending).question if asking is not None else None,
            "open_drafts": [{"awaiting_yes": Waiting.read(d.pending).stage == "preview",
                             "question": Waiting.read(d.pending).question,
                             "items": [self._item_context(i) for i in d.items]} for d in open_],
            "last_logged": None if last is None else {"items": [self._item_context(i) for i in last.items]},
        }

    # ------------------------------------------------------------ edits

    def _target(self, actions: list[Any], open_: list[AgentDraft], recent: list[AgentDraft], now: datetime,
                session_ref: str | None) -> tuple[AgentDraft | None, bool]:
        refs = {a.item_ref for a in actions if a.type in ("revise_item", "remove_item")}
        if refs:
            holder = next((d for d in open_ if refs <= {i.item_ref for i in d.items}), None)
            if holder is not None:
                return holder, False
            last = next((d for d in recent if d.state == "committed"), None)
            if last is not None and last.committed_entry_id and refs <= {i.item_ref for i in last.items}:
                # Fixing an item of the meal just logged amends THAT entry; never a second meal.
                return new_draft(log_date=last.log_date, session_ref=session_ref, items=last.items,
                                 meal_type=last.meal_type, meal_type_origin=last.meal_type_origin,
                                 parent_draft_id=last.id, amends_entry_id=last.committed_entry_id,
                                 amends_entry_revision=last.committed_entry_revision), True
            return None, False
        current = open_[0] if open_ else None
        if current is not None and now - (current.updated_at or now) <= self.new_meal_after:
            return current, False
        if not any(a.type in ("add_items", "recall_meal") for a in actions):
            return (current, False) if current is not None else (None, False)
        return new_draft(log_date=domain.effective_date(now), session_ref=session_ref), True

    @staticmethod
    def _next_ref(items: Sequence[DraftItemState], taken: set[str]) -> str:
        """``i<n>``, unique across recent drafts so an open item and a logged item never share one."""
        refs = {i.item_ref for i in items} | taken
        numbers = [int(ref[1:]) for ref in refs if re.fullmatch(r"i\d{1,6}", ref)]
        return f"i{max(numbers, default=0) + 1}"

    @staticmethod
    def _revise(item: DraftItemState, action: Any) -> DraftItemState:
        """Null fields keep. A new identity looks up again with the SAME portion; a new portion
        rescales the SAME food with no lookup."""
        identity = item.identity
        if action.identity is not None:
            identity = action.identity.apply_to(identity, action.clear_fields)
        elif action.clear_fields:
            identity = identity.model_copy(update={key: None for key in action.clear_fields})
        portion = action.portion if action.portion is not None else item.portion
        modifications = list(action.modifications) if action.modifications is not None else list(item.modifications)
        if identity != item.identity or item.resolved is None:
            return DraftItemState(item_ref=item.item_ref, identity=identity, portion=portion,
                                  modifications=modifications)
        food = item.resolved
        if food.provider == "user":  # the user's total stands; only the label follows the new amount
            food = food.model_copy(update={"serving_basis": parse_serving_text(portion.text or "1 serving")})
            return item.model_copy(update={"portion": portion, "modifications": modifications, "resolved": food})
        found = portion_multiplier(portion, food.serving_basis, identity) if portion != item.portion else item.multiplier
        return DraftItemState(item_ref=item.item_ref, identity=identity, portion=portion, modifications=modifications,
                              resolved=food, multiplier=float(found) if found is not None else None)

    async def _edit(self, actions: list[Any], message: str, open_: list[AgentDraft], recent: list[AgentDraft],
                    now: datetime, session_ref: str | None) -> TurnResult:
        target, is_new = self._target(actions, open_, recent, now, session_ref)
        if target is None:
            if any(a.type in ("revise_item", "remove_item") for a in actions):
                return _result("I couldn't find that item. What should I change?", "needs_detail")
            return _result("There's nothing waiting to log right now.")
        items = list(target.items)
        taken = {i.item_ref for d in recent for i in d.items}
        plan = Plan(supplied={})
        asked = set(Waiting.read(target.pending).asked)
        notes: list[str] = []
        skip: set[str] = set()
        for action in actions:
            if action.type == "add_items":
                for spec in action.items:
                    self._add(items, spec, message, plan, taken)
            elif action.type == "revise_item":
                index = next((n for n, i in enumerate(items) if i.item_ref == action.item_ref), None)
                if index is None:
                    return _result("I couldn't find that item. What should I change?", "needs_detail")
                items[index] = self._revise(items[index], action)
                stated = only_stated(action.supplied, message)
                if stated is not None:
                    plan.supplied[action.item_ref] = stated
            elif action.type == "remove_item":
                items = [i for i in items if i.item_ref != action.item_ref]
            elif action.type == "settle":
                plan.settle = True
                plan.use_web = plan.use_web or action.use_web
            elif action.type == "recall_meal":
                await self._recall(action, items, notes, plan, asked, skip, taken)
        plan.notes, plan.skip = tuple(notes), frozenset(skip)
        if not items:
            if not is_new:
                await self.drafts.cancel(target.id, target.revision)
            if target.is_amend:
                return _result("That meal is already saved, so I didn't change it. To take it off, delete it in Today.",
                               "needs_detail", "not_applied")
            return _result("Removed. Nothing left to log.")
        if len(items) > MAX_DRAFT_ITEMS:
            return _result("That's more items than I can keep in one meal. Can you split it?", "needs_detail")
        return await self._advance(target, is_new, items, plan, now, session_ref, recent)

    def _add(self, items: list[DraftItemState], spec: Any, message: str, plan: Plan, taken: set[str]) -> None:
        """A food restated before confirming is the same food, not a second one."""
        same = next((i for i in items if match.tokens(identity_text(i)) == match.tokens(
            " ".join(p for p in (spec.identity.brand, spec.identity.name, spec.identity.variant) if p))
            and i.portion.kind == spec.portion.kind and i.portion.amount == spec.portion.amount), None)
        stated = only_stated(spec.supplied, message)
        if same is None:
            same = DraftItemState(item_ref=self._next_ref(items, taken), identity=spec.identity, portion=spec.portion,
                                  modifications=list(spec.modifications))
            items.append(same)
        if stated is not None:
            plan.supplied[same.item_ref] = stated

    async def _recall(self, action: Any, items: list[DraftItemState], notes: list[str], plan: Plan, asked: set[str],
                      skip: set[str], taken: set[str]) -> None:
        found = None
        if self.recaller is not None:
            try:
                found = await self.recaller.recall(action.query, action.when_hint)
            except Exception as exc:  # noqa: BLE001
                logger.warning("food agent recall failed: %s", type(exc).__name__)
        if found is not None and found.status == "found" and found.items:
            for item in found.items:
                items.append(item.model_copy(update={"item_ref": self._next_ref(items, taken)}))
            if found.note:
                notes.append(found.note)
            return
        phrase = _phrase(action.query) or action.query
        key = f"recall:{'-'.join(sorted(match.tokens(phrase)))}"[:60]
        existing = next((i for i in items if match.tokens(i.identity.name) == match.tokens(phrase)), None)
        if existing is None:
            existing = DraftItemState(item_ref=self._next_ref(items, taken), portion=Portion(kind="unspecified", text=""),
                                      identity=FoodIdentity(name=phrase[:120], kind="other"))
            items.append(existing)
        if key not in asked:
            skip.add(existing.item_ref)
            plan.forced = (key, f"I don't see that in your log. Where was the {phrase} from, or what was in it, "
                                "and roughly how much of each?")

    # ------------------------------------------------------------ resolve

    async def _restore(self, item: DraftItemState, scope: Scope) -> DraftItemState:
        """Provider panels are not kept in drafts. Fetch the same food again, by id where there is one."""
        food = await self.retrieval.refresh(item, scope)
        return item.model_copy(update={"resolved": food} if food is not None else {"resolved": None, "multiplier": None})

    async def _resolve_all(self, items: list[DraftItemState], plan: Plan, scope: Scope,
                           unknown_ok: bool) -> list[DraftItemState]:
        supplied = dict(plan.supplied or {})
        restored: list[DraftItemState] = []
        for item in items:
            food = item.resolved
            if food is not None and food.panel.calories is None:
                if food.provider == "user":  # its gaps came from a source that can't be stored
                    supplied.setdefault(item.item_ref, Supplied(**food.attribution.get("supplied", {})))
                    item = item.model_copy(update={"resolved": None, "multiplier": None})
                else:
                    item = await self._restore(item, scope)
            restored.append(item)

        def needs_lookup(item: DraftItemState) -> bool:
            if item.item_ref in plan.skip:
                return False
            stated = supplied.get(item.item_ref)
            if stated is not None:
                return item.resolved is None and not (unknown_ok and "calories" in stated.given())
            return item.resolved is None

        waiting = [item for item in restored if needs_lookup(item)]
        results: dict[str, Found] = {}
        if plan.use_web:
            found = await asyncio.gather(*(self.retrieval.find_on_web(i.identity, i.portion, scope) for i in waiting))
        else:
            found = await asyncio.gather(*(self.retrieval.find(i.identity, i.portion, scope) for i in waiting))
        results.update(zip((i.item_ref for i in waiting), found))

        ready_now = any(item.resolved is not None for item in restored) \
            or any(result.food is not None for result in results.values())
        to_web = [item for item in waiting if results[item.item_ref].status == "needs_web"]
        if to_web:
            tasks = {asyncio.create_task(self.retrieval.find_on_web(i.identity, i.portion, scope)): i for i in to_web}
            done, pending = await asyncio.wait(tasks, timeout=self.web_soft_seconds if ready_now else None)
            for task in done:
                results[tasks[task].item_ref] = task.result()
            for task in pending:  # the shared lookup keeps running and lands in the web cache
                task.cancel()
                results[tasks[task].item_ref] = Found("not_counted", reason="still looking it up")
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        out = []
        for item in restored:
            result = results.get(item.item_ref)
            if result is not None and result.food is not None:
                mult = portion_multiplier(item.portion, result.food.serving_basis, item.identity)
                item = item.model_copy(update={"resolved": result.food, "multiplier": float(mult) if mult else None})
            stated = supplied.get(item.item_ref)
            if stated is not None:
                item = merge_supplied(item, stated, item.resolved, unknown_ok)
            out.append(item)
        return out

    # ------------------------------------------------------------ decide and reply

    async def _advance(self, target: AgentDraft, is_new: bool, items: list[DraftItemState], plan: Plan, now: datetime,
                       session_ref: str | None, recent: list[AgentDraft]) -> TurnResult:
        state = Waiting.read(target.pending)
        unknown_ok = await self.drafts.unknown_nutrients_writable()
        items = await self._resolve_all(items, plan, self.retrieval.scope(), unknown_ok)

        asked, assumed = list(state.asked), list(state.assumed)
        question: tuple[str, str] | None = plan.forced
        for index, item in enumerate(items):
            if item.item_ref in plan.skip or item.resolved is None or item.resolved.provider == "user":
                continue
            basis_key = f"basis:{item.item_ref}"
            if basis_key not in assumed and match.needs_raw_or_cooked(item.identity, item.portion):
                if plan.settle:
                    assumed.append(basis_key)
                elif basis_key not in asked:
                    amount = " ".join(item.portion.text.split()) or f"{item.portion.amount:g} {item.portion.unit}"
                    question = question or (basis_key, f"Was the {amount} of {item.identity.name} raw or cooked?")
                    items[index] = item.model_copy(update={"multiplier": None})
                    continue
                else:
                    items[index] = item.model_copy(update={"multiplier": None})
                    continue
            if item.multiplier is None:
                amount_key = f"amount:{item.item_ref}"
                if plan.settle:
                    items[index] = item.model_copy(update={"multiplier": 1.0})
                    assumed.append(amount_key)
                elif amount_key not in asked:
                    name, basis = item.resolved.display_name, item.resolved.serving_basis
                    says_more = basis.text and basis.text.strip().casefold() not in ("serving", "1 serving")
                    serving = f" One serving is {basis.text}." if basis.basis == "per_serving" and says_more else ""
                    question = question or (amount_key, f"How much {name} did you have?{serving}"[:300])

        if question is not None:
            asked.append(question[0])
        ready = [i for i in items if is_ready(i, unknown_ok)]
        if target.is_amend and len(ready) < sum(1 for i in target.items if i.resolved is not None and
                                                i.item_ref in {x.item_ref for x in items}):
            # Nothing the saved meal counts is ever dropped by a failed refresh or an unfound change.
            before = {i.item_ref: i for i in target.items}
            changed = [user_words(i.identity) for i in items if i.item_ref in before
                       and before[i.item_ref].resolved is not None and not is_ready(i, unknown_ok)
                       and i.identity != before[i.item_ref].identity]
            if changed:
                return _result(f"I couldn't find {_join(changed)}, so I left the meal as it was.",
                               "error", "not_applied")
            return _result("I couldn't reload everything in that meal, so I left it as it was. Try again in a moment.",
                           "error", "not_applied")
        asked_keys, assumed_keys = tuple(dict.fromkeys(asked)), tuple(dict.fromkeys(assumed))
        revision = 1 if is_new else target.revision + 1

        if question is not None:
            waiting = Waiting(stage="question", revision=revision, session_ref=session_ref, question=question[1],
                              asked=asked_keys, assumed=assumed_keys)
            saved = await self._save(target, is_new, items, "needs_detail", waiting)
            if saved is None:
                return _result("That meal just changed somewhere else. Tell me again what to change?", "conflict",
                               "not_applied")
            return _result(question[1], "needs_detail", draft_id=saved.id, question=question[1])

        if not ready:
            waiting = Waiting(stage="idle", revision=revision, asked=asked_keys, assumed=assumed_keys)
            saved = await self._save(target, is_new, items, "collecting", waiting)
            if saved is None:
                return _result("That meal just changed somewhere else. Tell me again what to change?", "conflict",
                               "not_applied")
            names = self._not_counted(items, unknown_ok)
            ask = "What was it, so I can add it?" if len(names) == 1 else "What were they, so I can add them?"
            return _result(f"Not counted: {_join(names)}. {ask}", "needs_detail", "not_applied", draft_id=saved.id)

        digest = content_hash(ready)
        waiting = Waiting(stage="preview", revision=revision, content_hash=digest, session_ref=session_ref,
                          asked=asked_keys, assumed=assumed_keys)
        panels = [i.scaled_panel() for i in ready]
        lines = [PreviewLine(item_ref=i.item_ref, display_name=i.resolved.display_name, portion_text=i.portion.text,
                             nutrients=p) for i, p in zip(ready, panels)]
        preview = Preview(draft_id=target.id, revision=revision, content_hash=digest, lines=lines,
                          totals=sum_panels(panels), created_at=now)
        text = self._preview_text(target, items, ready, preview, plan.notes, assumed_keys, unknown_ok)
        if len(text) > REPLY_LIMIT:
            stored = Waiting(stage="idle", revision=revision, asked=asked_keys, assumed=assumed_keys)
            await self._save(target, is_new, items, "needs_detail", stored)
            return _result(TOO_LONG, "needs_detail", "not_applied")
        saved = await self._save(target, is_new, items, "collecting", waiting)
        if saved is None:
            return _result("That meal just changed somewhere else. Tell me again what to change?", "conflict",
                           "not_applied")
        if saved.revision != revision:
            return _result("That meal changed just now. Check Today.", "conflict", "not_applied")
        return _result(text, draft_id=saved.id, preview=preview)

    async def _save(self, target: AgentDraft, is_new: bool, items: list[DraftItemState], state: str,
                    waiting: Waiting) -> AgentDraft | None:
        changed = target.with_(items=tuple(items), state=state, pending=waiting.to_pending())
        try:
            return await (self.drafts.create(changed) if is_new else self.drafts.save(changed, target.revision))
        except Exception as exc:  # noqa: BLE001
            logger.warning("food agent draft save failed: %s", type(exc).__name__)
            return None

    @staticmethod
    def _not_counted(items: Sequence[DraftItemState], unknown_ok: bool) -> list[str]:
        return list(dict.fromkeys(item_label(i) for i in items if not is_ready(i, unknown_ok)))

    @staticmethod
    def _notes(ready: Sequence[DraftItemState], assumed: Sequence[str]) -> list[str]:
        out: list[str] = []
        for item in ready:
            food = item.resolved
            if food is None:
                continue
            if food.provider == "user":
                out.append("using your numbers, with the rest scaled from "
                           f"{food.attribution.get('matched_name')}" if food.attribution.get("filled")
                           else f"using your numbers for {food.display_name}")
                continue
            matched = food.attribution.get("matched_name") or food.display_name
            shown = identity_text(item) if food.attribution.get("matched_name") is None else food.display_name
            if food.provider != "history" and match.differs(shown, matched):
                out.append(f"matched to {matched}")
            if f"amount:{item.item_ref}" in assumed:
                basis = food.serving_basis
                out.append(f"assumed {'100 g' if basis.basis != 'per_serving' else '1 serving'} of {food.display_name}")
            if f"basis:{item.item_ref}" in assumed:
                out.append(f"assumed {item.identity.name} as listed by the source")
        return out

    def _preview_text(self, target: AgentDraft, items: Sequence[DraftItemState], ready: Sequence[DraftItemState],
                      preview: Preview, notes: Sequence[str], assumed: Sequence[str], unknown_ok: bool) -> str:
        parts = [f"{item_label(i)}{' (est.)' if is_estimate(i.resolved) else ''}: {nutrient_text(line.nutrients)}"
                 for i, line in zip(ready, preview.lines)]
        text = "; ".join(parts)
        if len(parts) > 1:
            text += f". Total: {nutrient_text(preview.totals)}"
        text += "."
        credits = [credit for item in ready for credit in attribution_texts(
            [item.resolved.attribution, item.resolved.component_attributions])]
        adjusted = adjustment_note(ready)
        for note in dict.fromkeys([*notes, *self._notes(ready, assumed), *([adjusted] if adjusted else []), *credits]):
            text += f" {_sentence(note)}"
        missing = self._not_counted(items, unknown_ok)
        if missing:
            text += f" Not counted: {_join(missing)}. {ADD_IT}"
        return text + (" Save the change?" if target.is_amend else " Log it?")

    # ------------------------------------------------------------ confirm and commit

    async def _rehydrate(self, draft: AgentDraft, scope: Scope, unknown_ok: bool) -> AgentDraft:
        """Restore what was shown. Unresolved items stay unresolved: a confirmation adds no food."""
        keep_out = frozenset(i.item_ref for i in draft.items if i.resolved is None)
        return draft.with_(items=tuple(await self._resolve_all(list(draft.items), Plan(skip=keep_out), scope, unknown_ok)))

    async def _confirm(self, draft: AgentDraft, now: datetime, session_ref: str | None) -> TurnResult:
        binding = Waiting.read(draft.pending)
        unknown_ok = await self.drafts.unknown_nutrients_writable()
        current = await self._rehydrate(draft, self.retrieval.scope(), unknown_ok)
        ready = [i for i in current.items if is_ready(i, unknown_ok)]
        digest = content_hash(ready) if ready else None
        if binding.revision != draft.revision or binding.content_hash != digest:
            return await self._advance(draft, False, list(current.items),
                                       Plan(notes=("that meal changed since I showed it",)), now, session_ref, [])
        pinned = current.with_(meal_type=current.meal_type or _default_meal_type(now))
        try:
            plan = build_commit(pinned.with_(items=tuple(ready)), digest)
        except (DraftNotReady, ValueError) as exc:
            logger.info("draft not committable: %s", exc)
            return await self._advance(draft, False, list(current.items), Plan(), now, session_ref, [])
        intent = CommitIntent(draft_id=draft.id, revision=draft.revision + 1, content_hash=digest,
                              request_hash=plan.request_hash)
        staged = await self.drafts.save(pinned.with_(state="ready", pending=intent.to_pending_question()),
                                        draft.revision)
        if staged is None:
            # Lost the race: a duplicate delivery of this same yes may have won it.
            fresh = await self.drafts.get(draft.id)
            if fresh is not None and fresh.commit_intent() is not None:
                done = await self._reconcile_one(fresh, now, session_ref)
                if done is not None:
                    return done
            return _result("That meal changed before I could save it. Check Today.", "conflict", "not_applied")
        return await self._commit(staged, intent, plan)

    async def _commit(self, draft: AgentDraft, intent: CommitIntent, plan: CommitPlan) -> TurnResult:
        try:
            stored = await self.drafts.commit(draft.id, intent.revision, intent.request_hash, plan.values, plan.metadata)
        except DraftCommitConflict:
            try:  # definitely not applied: release the intent so it is never retried
                await self.drafts.save(draft.with_(state="collecting", pending=None), draft.revision)
            except Exception:  # noqa: BLE001
                logger.warning("food agent could not release a refused commit")
            return _result("That meal changed before I could save it, so I didn't save it. Check Today.",
                           "conflict", "not_applied", draft_id=draft.id)
        except DraftCommitBlocked:
            try:
                await self.drafts.save(draft.with_(state="collecting", pending=None), draft.revision)
            except Exception:  # noqa: BLE001
                logger.warning("food agent could not release a blocked commit")
            return _result("Some nutrients are unknown and I can't save unknown values yet, so nothing was saved.",
                           "needs_detail", "not_applied", draft_id=draft.id)
        except Exception as exc:  # noqa: BLE001 - a lost response: the effect is unknown
            logger.warning("food agent commit outcome unknown: %s", type(exc).__name__)
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
        entry = (stored or {}).get("entry") or {}
        return await self._readback(draft, intent, plan, entry.get("id"), entry.get("entry_revision"), reconciling=False)

    async def _readback(self, draft: AgentDraft, intent: CommitIntent, plan: CommitPlan, entry_id: Any,
                        entry_revision: Any, *, reconciling: bool) -> TurnResult:
        if not entry_id or entry_revision is None:
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
        try:
            row = await self.drafts.fetch_entry(str(entry_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("food agent readback failed: %s", type(exc).__name__)
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
        if row is None:
            if reconciling:
                await self._ack(draft, intent, entry_id)
                return _result("That meal was saved earlier but isn't in your log now. Check Today.", "conflict",
                               "unknown", draft_id=draft.id)
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
        if int(row.get("entry_revision") or 0) != int(entry_revision) or not _same_values(row, plan.values):
            if reconciling and int(row.get("entry_revision") or 0) > int(entry_revision):
                await self._ack(draft, intent, entry_id)
                receipt = Receipt.from_saved_row(row, verified=False, unknown_nutrients=plan.unknown)
                return _result("That meal was saved and has been changed since. Check Today.", "conflict", "unknown",
                               receipt=receipt, draft_id=draft.id)
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
        receipt = Receipt.from_saved_row(row, verified=True, unknown_nutrients=plan.unknown)
        await self._ack(draft, intent, entry_id)
        verb = "Updated" if draft.is_amend else "Logged"
        text = f"{verb} {receipt.name}: {nutrient_text(receipt.nutrients)}."
        saved_refs = {item.get("item_ref") for item in plan.metadata.get("items", [])}
        # A reconciled commit has no metadata, and its provider panels are not in the draft, so an
        # empty panel here must never read as "not counted".
        missing = [item_label(i) for i in draft.items if i.item_ref not in saved_refs] if saved_refs else \
            [item_label(i) for i in draft.items if i.resolved is None or i.multiplier is None]
        if missing:
            text += f" Not counted: {_join(missing)}. {ADD_IT}"
        return _result(text, "ok", "applied_verified", receipt=receipt, draft_id=draft.id)

    async def _ack(self, draft: AgentDraft, intent: CommitIntent, entry_id: Any) -> None:
        try:
            await self.drafts.acknowledge(draft.id, intent.revision, intent.request_hash, str(entry_id))
        except Exception as exc:  # noqa: BLE001 - harmless: the next turn verifies and reports again
            logger.warning("food agent ack failed: %s", type(exc).__name__)

    async def _cancel(self, draft: AgentDraft) -> TurnResult:
        if not await self.drafts.cancel(draft.id, draft.revision):
            return _result("That meal changed just now. Check Today.", "conflict", "not_applied")
        return _result("OK, I left the saved meal as it was." if draft.is_amend else "OK, not logged.", draft_id=draft.id)

    # ------------------------------------------------------------ reconciliation

    async def _reconcile(self, now: datetime, decision: str, session_ref: str | None) -> TurnResult | None:
        """Finish or verify an earlier commit whose outcome was not verified. Same key, same hash."""
        for draft in await self.drafts.recovery():
            intent = draft.commit_intent()
            if intent is None:
                continue
            if draft.state == "committed":
                if draft.committed_operation_key != op_key(draft.id, intent.revision):
                    continue
                if await self.drafts.is_acknowledged(draft.id, intent.revision):
                    continue
                return await self._reconcile_one(draft, now, session_ref)
            if draft.state == "ready" and draft.revision == intent.revision:
                if decision == "no" and await self.drafts.fetch_operation(op_key(draft.id, intent.revision)) is None \
                        and await self.drafts.cancel(draft.id, draft.revision):
                    return _result("OK, not logged.", "ok", "not_applied", draft_id=draft.id)
                return await self._reconcile_one(await self.drafts.get(draft.id) or draft, now, session_ref)
        return None

    async def _reconcile_one(self, draft: AgentDraft, now: datetime, session_ref: str | None) -> TurnResult | None:
        intent = draft.commit_intent()
        if intent is None:
            return None
        operation = await self.drafts.fetch_operation(op_key(draft.id, intent.revision))
        original = ((operation or {}).get("stored_response") or {}).get("entry")
        plan: CommitPlan | None
        if original:
            values = {key: original.get(key) for key in ("name", "meal", "macro_source", "calories", "protein", "carbs",
                                                         "fat", "fiber")}
            values["day"] = draft.log_date
            if request_hash_of(values) != intent.request_hash:
                return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
            unknown = tuple(key for key in NUTRIENT_KEYS if values[key] is None)
            plan = CommitPlan(values, {}, intent.request_hash, unknown,
                              NutrientPanel(**{key: values[key] for key in NUTRIENT_KEYS}))
        else:
            if draft.state == "committed":
                # Operation retention expired: never create a second entry.
                return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
            unknown_ok = await self.drafts.unknown_nutrients_writable()
            current = await self._rehydrate(draft, self.retrieval.scope(), unknown_ok)
            ready = tuple(i for i in current.items if is_ready(i, unknown_ok))
            try:
                plan = build_commit(current.with_(items=ready), intent.content_hash) if ready else None
            except (DraftNotReady, ValueError):
                plan = None
            draft = current
        if draft.state == "committed":
            operation = await self.drafts.fetch_operation(op_key(draft.id, intent.revision))
            entry = ((operation or {}).get("stored_response") or {}).get("entry") or {}
            return await self._readback(draft, intent, plan, entry.get("id") or draft.committed_entry_id,
                                        entry.get("entry_revision") or draft.committed_entry_revision, reconciling=True)
        if draft.state != "ready" or draft.revision != intent.revision:
            return None
        if plan is None or plan.request_hash != intent.request_hash:
            # Never commit something other than what the user said yes to.
            released = draft.with_(state="collecting", pending=None)
            await self.drafts.save(released, draft.revision)
            fresh = await self.drafts.get(draft.id)
            if fresh is None:
                return _result("That meal changed before it was saved. Check Today.", "conflict", "not_applied")
            return await self._advance(fresh, False, list(draft.items),
                                       Plan(notes=("that meal changed before it was saved",)), now, session_ref, [])
        return await self._commit(draft, intent, plan)


__all__ = ["FoodAgent2", "TurnResult", "nutrient_text", "is_ready", "merge_supplied"]
