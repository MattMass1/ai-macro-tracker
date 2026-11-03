"""The food agent coordinator: one structured model call per turn, then plain code.

Write path (AGENT.md section 4): draft, then a preview bound to
``(draft_id, revision, content_hash)``, then the user's own unambiguous yes to
that exact preview, then ONE commit through ``Store.commit_meal_draft``
(idempotent by ``meal-draft:{id}:{rev}``), then readback. The receipt is built
from the saved row only.

Commit authority is code, never the model: :mod:`food_agent.confirm` must call
the user's raw message a plain yes, exactly one open preview may exist, and its
revision and recomputed content hash must still match the stored binding.

Unknown outcome: before the commit the draft is moved to ``ready`` with a
durable :class:`~food_agent.drafts.CommitIntent`. If the commit or readback
cannot be verified the turn says so, and the NEXT turn reconciles first, by the
same operation key and request hash, before doing anything else.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import domain
from food_agent import confirm
from food_agent.contracts import (AgentTurn, DraftItemState, NutrientPanel, PendingConfirm, Preview, PreviewLine,
                                  Receipt, ResolvedFood, ToolOutcome, content_hash, sum_panels)
from food_agent.drafts import (AgentDraft, CommitIntent, CommitPlan, DraftCommitBlocked, DraftCommitConflict,
                               DraftNotReady, DraftPort, build_commit, identity_text, item_label, new_draft,
                               op_key, portion_label)
from food_agent.ports import FoodFinder, Interpreter, MealRecaller
from food_agent.portion import multiplier as portion_multiplier

logger = logging.getLogger(__name__)

EDIT_TYPES = frozenset({"add_items", "revise_item", "remove_item", "recall_meal"})
EDITABLE_STATES = ("collecting", "needs_detail")
RECENT_WINDOW = timedelta(hours=24)
NEW_MEAL_AFTER = timedelta(minutes=20)
MAX_DRAFT_ITEMS = 24

COULD_NOT_VERIFY = ("I could not verify it saved. I'll check it first thing on your next message, "
                    "so don't log it again.")


@dataclass(frozen=True)
class FoodTurnResult:
    """One turn's result for the canvas (WP6).

    ``handed_off=True`` means the message is not food: the caller runs its old
    path. ``reply`` may still be non-empty then (a reconciliation note from an
    earlier unverified commit) and should be shown first.
    """

    reply: str
    handed_off: bool
    preview: Preview | None
    receipt: Receipt | None
    outcome: ToolOutcome
    draft_id: str | None = None
    question: str | None = None


def _outcome(status: str, effect: str, message: str) -> ToolOutcome:
    return ToolOutcome(status=status, effect=effect, safe_message=message[:300] or status)


def _result(reply: str, status: str = "ok", effect: str = "not_attempted", **kw: Any) -> FoodTurnResult:
    return FoodTurnResult(reply=reply, handed_off=kw.pop("handed_off", False), preview=kw.pop("preview", None),
                          receipt=kw.pop("receipt", None), outcome=_outcome(status, effect, reply), **kw)


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


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold().replace("'", "")))


def _match_note(item: DraftItemState, food: ResolvedFood, note: str | None) -> str | None:
    if note:
        return note
    return None if _words(identity_text(item)) == _words(food.display_name) else f"matched to {food.display_name}"


def _join(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


class FoodAgent:
    """``FoodAgent(drafts, interpret, finder, recaller, clock)``; call :meth:`turn` once per user message."""

    def __init__(self, drafts: DraftPort, interpret: Interpreter, finder: FoodFinder,
                 recaller: MealRecaller | None = None, clock: Callable[[], datetime] | None = None, *,
                 new_meal_after: timedelta = NEW_MEAL_AFTER, recent_window: timedelta = RECENT_WINDOW):
        self.drafts = drafts
        self.interpret = interpret
        self.finder = finder
        self.recaller = recaller
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.new_meal_after = new_meal_after
        self.recent_window = recent_window

    # ------------------------------------------------------------ entry point

    async def turn(self, message: str, *, session_ref: str | None = None,
                   turn_ref: str | None = None) -> FoodTurnResult:
        now = self.clock()
        decision = confirm.classify(message)
        try:
            recent = await self.drafts.recent(now - self.recent_window)
            reconciled = await self._reconcile(recent, now)
        except Exception as exc:  # the store is unreachable: claim nothing
            logger.warning("food agent could not read drafts: %s", type(exc).__name__)
            return _result("I couldn't reach your log just now. Try again in a moment.", "error", "unknown")
        if reconciled is not None:
            if decision != "other":
                return reconciled  # "yes" again after "could not verify": the check IS the answer
            try:
                recent = await self.drafts.recent(now - self.recent_window)
            except Exception:
                return reconciled
            rest = await self._handle(message, decision, recent, now, session_ref)
            return FoodTurnResult(
                reply=" ".join(part for part in (reconciled.reply, rest.reply) if part),
                handed_off=rest.handed_off, preview=rest.preview, receipt=reconciled.receipt,
                outcome=rest.outcome if rest.outcome.effect != "not_attempted" else reconciled.outcome,
                draft_id=rest.draft_id or reconciled.draft_id, question=rest.question)
        return await self._handle(message, decision, recent, now, session_ref)

    async def _handle(self, message: str, decision: str, recent: list[AgentDraft], now: datetime,
                      session_ref: str | None) -> FoodTurnResult:
        open_ = [d for d in recent if d.state in EDITABLE_STATES]
        bound = [d for d in open_ if d.pending_confirm() is not None]
        if decision == "yes" and len(bound) == 1:
            return await self._confirm(bound[0], now)
        if decision == "yes" and len(bound) > 1:
            return self._ask_which(bound)
        if decision == "yes":
            asking = next((d for d in open_ if d.question()), None)
            if asking is not None:
                question = asking.question() or ""
                return _result(f"I still need one thing first. {question}", "needs_detail",
                               draft_id=asking.id, question=question)
        if decision == "no" and len(bound) == 1:
            return await self._cancel(bound[0])

        try:
            turn = await self.interpret(message, self._context(open_, recent, now))
            if not isinstance(turn, AgentTurn):
                turn = AgentTurn.model_validate_json(json.dumps(turn))
        except Exception as exc:
            logger.warning("food agent interpreter failed: %s", type(exc).__name__)
            return _result("Sorry, I couldn't read that. Can you say it again?", "error")

        actions = list(turn.actions)
        types = [a.type for a in actions]
        if "not_food" in types:
            return _result("", handed_off=True)
        if "cancel" in types and not EDIT_TYPES.intersection(types):
            target = bound[0] if len(bound) == 1 else (open_[0] if open_ else None)
            return await self._cancel(target) if target else _result("There's nothing to cancel.")
        if EDIT_TYPES.intersection(types):
            return await self._edit(actions, open_, recent, now, session_ref)
        confirm_action = next((a for a in actions if a.type == "confirm"), None)
        if confirm_action is not None:
            return await self._model_confirm(confirm_action, decision, bound, now)
        ask = next((a for a in actions if a.type == "ask"), None)
        if ask is not None:
            return _result(ask.question, "needs_detail", question=ask.question)
        return _result("Sorry, I couldn't read that. Can you say it again?", "error")

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
        return {
            "now": now.isoformat(),
            "open_drafts": [{"draft_ref": d.id, "revision": d.revision,
                             "awaiting_yes": d.pending_confirm() is not None, "question": d.question(),
                             "items": [self._item_context(i) for i in d.items]} for d in open_],
            "last_logged": None if last is None else {"items": [self._item_context(i) for i in last.items]},
        }

    # ------------------------------------------------------------ confirmation

    def _ask_which(self, bound: list[AgentDraft]) -> FoodTurnResult:
        names = [" and ".join(item_label(i) for i in d.items) for d in bound]
        reply = f"You have {len(bound)} meals waiting to log: {_join(names)}. Which one should I log?"
        return _result(reply, "needs_detail", question=reply)

    async def _model_confirm(self, action, decision: str, bound: list[AgentDraft], now: datetime) -> FoodTurnResult:
        """The model thinks the user confirmed. That is a hint: it never commits."""
        if len(bound) > 1:
            chosen = next((d for d in bound if d.id == str(action.draft_ref).lower()), None)
            if chosen is None:
                return self._ask_which(bound)
            for other in bound:  # unbind the others; their items stay as they were
                if other.id != chosen.id:
                    await self.drafts.save(other.with_(pending=None), other.revision)
            return await self._reshow(chosen, now)
        if len(bound) == 1:
            draft = bound[0]
            preview = self._preview(draft, now)
            if preview is None:
                return await self._reshow(draft, now)
            return _result(f"Nothing changed yet. {self._preview_text(draft, preview, [])}",
                           draft_id=draft.id, preview=preview)
        return _result("There's nothing waiting to log right now.")

    async def _confirm(self, draft: AgentDraft, now: datetime) -> FoodTurnResult:
        binding = draft.pending_confirm()
        assert binding is not None
        current_hash = content_hash(list(draft.items))
        if binding.revision != draft.revision or binding.content_hash != current_hash:
            return await self._reshow(draft, now, prefix="That meal changed since I showed it.")
        pinned = draft.with_(meal_type=draft.meal_type or _default_meal_type(now))
        try:
            plan = build_commit(pinned, current_hash)
        except (DraftNotReady, ValueError) as exc:
            logger.info("draft not committable: %s", exc)
            return await self._reshow(draft, now)
        if plan.unknown and not await self.drafts.unknown_nutrients_writable():
            return await self._block_unknown(draft, plan.unknown)
        intent = CommitIntent(draft_id=draft.id, revision=draft.revision + 1, content_hash=current_hash,
                              request_hash=plan.request_hash)
        staged = await self.drafts.save(pinned.with_(state="ready", pending=intent.to_pending_question()),
                                        draft.revision)
        if staged is None:
            # Lost the CAS: a duplicate delivery of this same yes may have won it.
            fresh = await self.drafts.get(draft.id)
            if fresh is not None and fresh.commit_intent() is not None:
                done = await self._reconcile_one(fresh, now, reacknowledge=True)
                if done is not None:
                    return done
            if fresh is not None and fresh.state in EDITABLE_STATES:
                return await self._reshow(fresh, now, prefix="That meal changed since I showed it.")
            return _result("That meal changed before I could save it. Check Today.", "conflict", "not_applied")
        if staged.revision != intent.revision:
            logger.error("commit intent bound to %s but draft is at %s", intent.revision, staged.revision)
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
        return await self._commit(staged, intent, plan)

    async def _commit(self, draft: AgentDraft, intent: CommitIntent, plan: CommitPlan) -> FoodTurnResult:
        try:
            stored = await self.drafts.commit(draft.id, intent.revision, intent.request_hash, plan.values,
                                              plan.metadata)
        except DraftCommitConflict:
            # Definitely not applied by this key. Release the intent so the next
            # turn does not retry it forever; the items stay for a fresh preview.
            try:
                await self.drafts.save(draft.with_(state="collecting", pending=None), draft.revision)
            except Exception:
                pass
            return _result("That meal changed before I could save it, so I didn't save it. Check Today.",
                           "conflict", "not_applied", draft_id=draft.id)
        except DraftCommitBlocked:
            return await self._block_unknown(draft, plan.unknown)
        except Exception as exc:  # lost response, connection error: the effect is unknown
            logger.warning("food agent commit outcome unknown: %s", type(exc).__name__)
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
        entry = (stored or {}).get("entry") or {}
        return await self._readback(draft, intent, plan, entry.get("id"), entry.get("entry_revision"),
                                    reconciling=False)

    async def _readback(self, draft: AgentDraft, intent: CommitIntent, plan: CommitPlan, entry_id: Any,
                        entry_revision: Any, *, reconciling: bool) -> FoodTurnResult:
        if not entry_id or entry_revision is None:
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
        try:
            row = await self.drafts.fetch_entry(str(entry_id))
        except Exception as exc:
            logger.warning("food agent readback failed: %s", type(exc).__name__)
            row = None
        if row is None:
            if reconciling:  # the operation completed durably, yet the row is gone
                await self._ack(draft, intent, entry_id)
                return _result("That meal was saved earlier but isn't in your log now. Check Today.",
                               "conflict", "unknown", draft_id=draft.id)
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
        if int(row.get("entry_revision") or 0) != int(entry_revision) or not _same_values(row, plan.values):
            if reconciling and int(row.get("entry_revision") or 0) > int(entry_revision):
                await self._ack(draft, intent, entry_id)
                receipt = Receipt.from_saved_row(row, verified=False, unknown_nutrients=plan.unknown)
                return _result("That meal was saved and has been changed since. Check Today.", "conflict",
                               "unknown", receipt=receipt, draft_id=draft.id)
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
        receipt = Receipt.from_saved_row(row, verified=True, unknown_nutrients=plan.unknown)
        await self._ack(draft, intent, entry_id)
        verb = "Updated" if draft.is_amend else "Logged"
        return _result(f"{verb} {receipt.name}: {nutrient_text(receipt.nutrients)}.", "ok", "applied_verified",
                       receipt=receipt, draft_id=draft.id)

    async def _ack(self, draft: AgentDraft, intent: CommitIntent, entry_id: Any) -> None:
        try:
            await self.drafts.acknowledge(draft.id, intent.revision, intent.request_hash, str(entry_id))
        except Exception as exc:  # harmless: the next turn re-verifies and reports again
            logger.warning("food agent ack failed: %s", type(exc).__name__)

    # ------------------------------------------------------------ reconciliation

    async def _reconcile(self, recent: list[AgentDraft], now: datetime) -> FoodTurnResult | None:
        """Finish or verify an earlier commit whose outcome was not verified. Same key, same hash."""
        for draft in recent:  # newest first
            intent = draft.commit_intent()
            if intent is None:
                continue
            if draft.state == "committed":
                if draft.committed_operation_key != op_key(draft.id, intent.revision):
                    continue
                if await self.drafts.is_acknowledged(draft.id, intent.revision):
                    break  # everything older was reconciled before this one
                return await self._reconcile_one(draft, now)
            if draft.state == "ready" and draft.revision == intent.revision:
                return await self._reconcile_one(draft, now)
        return None

    async def _reconcile_one(self, draft: AgentDraft, now: datetime, *,
                             reacknowledge: bool = False) -> FoodTurnResult | None:
        intent = draft.commit_intent()
        if intent is None:
            return None
        try:
            plan = build_commit(draft, intent.content_hash)
        except (DraftNotReady, ValueError):
            plan = None
        if draft.state == "committed":
            if plan is None:
                return None
            operation = await self.drafts.fetch_operation(op_key(draft.id, intent.revision))
            entry = ((operation or {}).get("stored_response") or {}).get("entry") or {}
            entry_id = entry.get("id") or draft.committed_entry_id
            entry_revision = entry.get("entry_revision") or draft.committed_entry_revision
            return await self._readback(draft, intent, plan, entry_id, entry_revision, reconciling=True)
        if draft.state != "ready" or draft.revision != intent.revision:
            return None
        if plan is None or plan.request_hash != intent.request_hash:
            # Never commit something other than what the user said yes to.
            return await self._reshow(draft, now, prefix="That meal changed before it was saved.")
        return await self._commit(draft, intent, plan)

    # ------------------------------------------------------------ cancel / block

    async def _cancel(self, draft: AgentDraft) -> FoodTurnResult:
        if not await self.drafts.cancel(draft.id, draft.revision):
            return _result("That meal changed just now. Check Today.", "conflict", "not_applied")
        if draft.is_amend:
            return _result("OK, I left the saved meal as it was.", draft_id=draft.id)
        return _result("OK, not logged.", draft_id=draft.id)

    async def _block_unknown(self, draft: AgentDraft, unknown: Sequence[str]) -> FoodTurnResult:
        missing = _join([key for key in unknown])
        names = _join([item_label(i) for i in draft.items if any(
            getattr(i.scaled_panel(), key, 0) is None for key in unknown)] or [item_label(draft.items[0])])
        question = (f"{names} has no {missing} data, and I can't save unknown values yet. "
                    "Is there a brand or product with full nutrition I should use?")
        await self.drafts.save(draft.with_(state="needs_detail", pending=question[:1000]), draft.revision)
        return _result(question, "needs_detail", "not_applied", draft_id=draft.id, question=question)

    # ------------------------------------------------------------ edits

    def _target(self, actions: list[Any], open_: list[AgentDraft], recent: list[AgentDraft], now: datetime,
                session_ref: str | None) -> tuple[AgentDraft | None, bool]:
        refs = {a.item_ref for a in actions if a.type in ("revise_item", "remove_item")}
        adds = any(a.type in ("add_items", "recall_meal") for a in actions)
        current = open_[0] if open_ else None
        if current is not None:
            current_refs = {i.item_ref for i in current.items}
            if refs and refs <= current_refs:
                return current, False
            if not refs and adds:
                age = now - (current.updated_at or now)
                if age <= self.new_meal_after:
                    return current, False
        if refs:
            last = next((d for d in recent if d.state == "committed"), None)
            if last is not None and last.committed_entry_id and refs <= {i.item_ref for i in last.items}:
                # Fixing an item of the meal just logged amends THAT entry; never a second meal.
                return new_draft(log_date=last.log_date, session_ref=session_ref, items=last.items,
                                 meal_type=last.meal_type, meal_type_origin=last.meal_type_origin,
                                 parent_draft_id=last.id, amends_entry_id=last.committed_entry_id,
                                 amends_entry_revision=last.committed_entry_revision), True
            return None, False
        return new_draft(log_date=domain.effective_date(now), session_ref=session_ref), True

    @staticmethod
    def _next_ref(items: Sequence[DraftItemState], taken: set[str] = frozenset()) -> str:
        """``i<n>``, unique across the user's recent drafts so an open draft's item
        and an item of the meal just logged never share a ref."""
        refs = {i.item_ref for i in items} | set(taken)
        numbers = [int(ref[1:]) for ref in refs if re.fullmatch(r"i\d{1,6}", ref)]
        return f"i{max(numbers, default=0) + 1}"

    async def _edit(self, actions: list[Any], open_: list[AgentDraft], recent: list[AgentDraft],
                    now: datetime, session_ref: str | None) -> FoodTurnResult:
        target, is_new = self._target(actions, open_, recent, now, session_ref)
        if target is None:
            return _result("I couldn't find that item. What should I change?", "needs_detail")
        items = list(target.items)
        taken = {i.item_ref for d in recent for i in d.items}
        recall_misses: list[str] = []
        notes: list[str] = []
        questions: list[str] = []
        for action in actions:
            if action.type == "add_items":
                for spec in action.items:
                    items.append(DraftItemState(item_ref=self._next_ref(items, taken), identity=spec.identity,
                                                portion=spec.portion, modifications=list(spec.modifications)))
            elif action.type == "revise_item":
                index = next((n for n, i in enumerate(items) if i.item_ref == action.item_ref), None)
                if index is None:
                    return _result("I couldn't find that item. What should I change?", "needs_detail")
                items[index] = self._revise(items[index], action, questions)
            elif action.type == "remove_item":
                items = [i for i in items if i.item_ref != action.item_ref]
            elif action.type == "recall_meal":
                recall_misses.append(await self._recall(action, items, notes, questions, taken))
        recall_only = all(a.type == "recall_meal" for a in actions if a.type in EDIT_TYPES)
        if questions and recall_only and items == list(target.items):
            # A recall that found nothing: say so honestly. No draft is created and
            # an open draft is left exactly as it was.
            status = "no_match" if "not_found" in recall_misses else "needs_detail"
            return _result(questions[0], status, draft_id=None if is_new else target.id, question=questions[0])
        if not items:
            if not is_new:
                await self.drafts.cancel(target.id, target.revision)
            return _result("Removed. Nothing left to log.", draft_id=None if is_new else target.id)
        if len(items) > MAX_DRAFT_ITEMS:
            return _result("That's more items than I can keep in one meal. Can you split it?", "needs_detail")

        items = await self._resolve(items, notes, questions)
        if not questions:
            questions.extend(self._calorie_questions(items))
        if not questions and items_ready(items):
            totals = sum_panels([i.scaled_panel() for i in items])  # type: ignore[misc]
            missing = [k for k in ("protein", "carbs", "fat", "fiber") if getattr(totals, k) is None]
            if missing and not await self.drafts.unknown_nutrients_writable():
                names = _join([item_label(i) for i in items if any(
                    getattr(i.scaled_panel(), k) is None for k in missing)])
                questions.append(f"{names} has no {_join(missing)} data, and I can't save unknown values yet. "
                                 "Is there a brand or product with full nutrition I should use?")
        if not questions:
            ask = next((a for a in actions if a.type == "ask"), None)
            if ask is not None:
                questions.append(ask.question)
        question = questions[0][:1000] if questions else None

        new_revision = 1 if is_new else target.revision + 1
        if question is None:
            binding = PendingConfirm(draft_id=target.id, revision=new_revision,
                                     content_hash=content_hash(items)).to_pending_question()
            changed = target.with_(items=tuple(items), state="collecting", pending=binding)
        else:
            changed = target.with_(items=tuple(items), state="needs_detail", pending=question)
        try:
            saved = await (self.drafts.create(changed) if is_new else self.drafts.save(changed, target.revision))
        except Exception as exc:
            logger.warning("food agent draft save failed: %s", type(exc).__name__)
            return _result("I couldn't save that just now. Try again in a moment.", "error", "not_applied")
        if saved is None:
            return _result("That meal just changed somewhere else. Tell me again what to change?",
                           "conflict", "not_applied")
        if question is not None:
            return _result(question, "needs_detail", draft_id=saved.id, question=question)
        if saved.revision != new_revision:  # the binding must name the revision the write produced
            return await self._reshow(saved, now)
        preview = self._preview(saved, now)
        if preview is None:
            return await self._reshow(saved, now)
        return _result(self._preview_text(saved, preview, notes), draft_id=saved.id, preview=preview)

    def _revise(self, item: DraftItemState, action: Any, questions: list[str]) -> DraftItemState:
        """Omitted fields preserve. Identity change re-resolves with the SAME portion; a
        portion-only change rescales the SAME food with no search."""
        identity = item.identity
        if action.identity is not None or action.clear_fields:
            patch = action.identity
            identity = patch.apply_to(identity, action.clear_fields) if patch is not None else \
                identity.model_copy(update={key: None for key in action.clear_fields})
        portion = action.portion if action.portion is not None else item.portion
        modifications = list(action.modifications) if action.modifications is not None else list(item.modifications)
        if identity != item.identity or item.resolved is None:
            return DraftItemState(item_ref=item.item_ref, identity=identity, portion=portion,
                                  modifications=modifications)
        mult = item.multiplier
        if portion != item.portion or mult is None:
            found = portion_multiplier(portion, item.resolved.serving_basis)
            mult = float(found) if found is not None else None
            if mult is None:
                questions.append(self._portion_question(item.resolved))
        return DraftItemState(item_ref=item.item_ref, identity=identity, portion=portion,
                              modifications=modifications, resolved=item.resolved, multiplier=mult)

    @staticmethod
    def _portion_question(food: ResolvedFood) -> str:
        basis = food.serving_basis.text or "one serving"
        return f"One serving of {food.display_name} is {basis}. How much did you have?"

    async def _recall(self, action: Any, items: list[DraftItemState], notes: list[str],
                      questions: list[str], taken: set[str]) -> str:
        """Add recalled items, or one honest question. Returns the recall status."""
        if self.recaller is None:
            questions.append(f"I can't look up past meals yet. What was in the {action.query}?")
            return "needs_detail"
        try:
            found = await self.recaller.recall(action.query, action.when_hint)
        except Exception as exc:
            logger.warning("food agent recall failed: %s", type(exc).__name__)
            questions.append(f"I couldn't search your log just now. What was in the {action.query}?")
            return "needs_detail"
        if found.status == "found" and found.items:
            for item in found.items:
                items.append(item.model_copy(update={"item_ref": self._next_ref(items, taken)}))
            if found.note:
                notes.append(found.note)
            return "found"
        questions.append(found.question or found.note
                         or f"I couldn't find {action.query} in your log. What was it?")
        return "not_found" if found.status != "needs_detail" else "needs_detail"

    async def _resolve(self, items: list[DraftItemState], notes: list[str],
                       questions: list[str]) -> list[DraftItemState]:
        out = []
        for item in items:
            if item.resolved is not None:
                if item.multiplier is None and not questions:
                    found = portion_multiplier(item.portion, item.resolved.serving_basis)
                    if found is None:
                        questions.append(self._portion_question(item.resolved))
                    else:
                        item = item.model_copy(update={"multiplier": float(found)})
                out.append(item)
                continue
            try:
                result = await self.finder.find(item.identity, item.portion)
            except Exception as exc:
                logger.warning("food agent finder failed: %s", type(exc).__name__)
                questions.append(f"I couldn't look up {identity_text(item)} right now. Try again in a moment?")
                out.append(item)
                continue
            if result.status == "matched" and result.food is not None:
                found = portion_multiplier(item.portion, result.food.serving_basis)
                item = DraftItemState(item_ref=item.item_ref, identity=item.identity, portion=item.portion,
                                      modifications=list(item.modifications), resolved=result.food,
                                      multiplier=float(found) if found is not None else None)
                note = _match_note(item, result.food, result.note)
                if note:
                    notes.append(note)
                if found is None:
                    questions.append(self._portion_question(result.food))
            else:
                questions.append(result.question or f"I couldn't find {identity_text(item)}. Where was it from?")
            out.append(item)
        return out

    @staticmethod
    def _calorie_questions(items: Sequence[DraftItemState]) -> list[str]:
        out = []
        for item in items:
            panel = item.scaled_panel()
            if panel is not None and panel.calories is None:
                out.append(f"I don't have calories for {item_label(item)}. "
                           "Do you know the brand or where it's from?")
        return out

    # ------------------------------------------------------------ previews

    def _preview(self, draft: AgentDraft, now: datetime) -> Preview | None:
        binding = draft.pending_confirm()
        if binding is None or not items_ready(draft.items):
            return None
        panels = [i.scaled_panel() for i in draft.items]
        if any(p is None or p.calories is None for p in panels):
            return None
        lines = [PreviewLine(item_ref=i.item_ref, display_name=i.resolved.display_name,  # type: ignore[union-attr]
                             portion_text=portion_label(i.portion) or i.portion.text, nutrients=p)
                 for i, p in zip(draft.items, panels)]
        return Preview(draft_id=draft.id, revision=binding.revision, content_hash=binding.content_hash,
                       lines=lines, totals=sum_panels(panels), created_at=now)  # type: ignore[arg-type]

    @staticmethod
    def _preview_text(draft: AgentDraft, preview: Preview, notes: Sequence[str]) -> str:
        parts = [f"{item_label(i)}: {nutrient_text(line.nutrients)}" for i, line in zip(draft.items, preview.lines)]
        body = "; ".join(parts)
        if len(parts) > 1:
            body += f". Total: {nutrient_text(preview.totals)}"
        text = body + "."
        for note in dict.fromkeys(notes):
            text += f" {note[0].upper()}{note[1:]}."
        text += " Save the change?" if draft.is_amend else " Log it?"
        return text

    async def _reshow(self, draft: AgentDraft, now: datetime, *, prefix: str = "") -> FoodTurnResult:
        """Bind a fresh preview to the draft's CURRENT content and show it. Never commits."""
        lead = f"{prefix} " if prefix else ""
        if not items_ready(draft.items) or self._calorie_questions(draft.items):
            question = draft.question() or (self._calorie_questions(draft.items) or [
                "Tell me again what you had?"])[0]
            return _result(f"{lead}{question}", "needs_detail", draft_id=draft.id, question=question)
        binding = PendingConfirm(draft_id=draft.id, revision=draft.revision + 1,
                                 content_hash=content_hash(list(draft.items))).to_pending_question()
        saved = await self.drafts.save(draft.with_(state="collecting", pending=binding), draft.revision)
        if saved is None or saved.pending_confirm() is None or saved.pending_confirm().revision != saved.revision:
            return _result(f"{lead}That meal changed just now. Check Today.", "conflict", "not_applied")
        preview = self._preview(saved, now)
        if preview is None:
            return _result(f"{lead}Tell me again what you had?", "needs_detail", draft_id=saved.id)
        return _result(f"{lead}{self._preview_text(saved, preview, [])}", draft_id=saved.id, preview=preview)


def items_ready(items: Sequence[DraftItemState]) -> bool:
    return bool(items) and all(i.resolved is not None and i.multiplier is not None for i in items)


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
    from meal_drafts import default_meal_type  # reuse the existing slot rule
    return default_meal_type(now)


__all__ = ["FoodAgent", "FoodTurnResult", "nutrient_text", "items_ready"]
