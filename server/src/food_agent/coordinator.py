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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import domain
from food_agent import confirm
from food_agent.contracts import (MASS_UNITS, VOLUME_UNITS, AgentTurn, DraftItemState, NutrientPanel,
                                  PendingConfirm, Preview, PreviewLine, Receipt, ResolvedFood, ToolOutcome, content_hash, sum_panels)
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
OVERSIZED = "This meal is too large to show completely. Please split it into smaller meals before confirming."
COULD_NOT_VERIFY = ("I could not verify it saved. I'll check it first thing on your next message, "
                    "so don't log it again.")


@dataclass(frozen=True)
class FoodTurnResult:
    """One turn's result for the canvas (WP6).

    ``handed_off=True`` means the message is not food: the caller runs its old
    path. ``reply`` may still be non-empty then (a reconciliation note from an
    earlier unverified commit) and should be shown first.

    ``shown_draft_ids``: drafts whose preview this reply shows besides ``preview``
    (the "which one?" question). Every other preview binding is cleared at the
    end of the turn, so a later yes/no can only act on what was shown LAST.
    """

    reply: str
    handed_off: bool
    preview: Preview | None
    receipt: Receipt | None
    outcome: ToolOutcome
    draft_id: str | None = None
    question: str | None = None
    shown_draft_ids: tuple[str, ...] = ()
    # One-tap answers for this reply. Each one is sent as an ordinary message,
    # so a tap has exactly the authority of typing the same words.
    quick_replies: tuple[str, ...] = ()


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
        except Exception as exc:  # the store is unreachable: claim nothing
            logger.warning("food agent could not read drafts: %s", type(exc).__name__)
            return _result("I couldn't reach your log just now. Try again in a moment.", "error", "unknown")
        shown_before = [d for d in recent if d.state in EDITABLE_STATES and d.pending_confirm() is not None]
        # R-1, check one (runs FIRST, fails closed): a preview is confirmable only if no
        # user turn has come after it. Every turn that does not consume a binding writes
        # a durable insert-only "seen" marker for it before any other work.
        try:
            live = [d for d in shown_before if not await self.drafts.is_seen(d.id, d.revision)]
            reshows_all = decision == "yes" and len(live) > 1  # "which one?" shows them all again
            # A binding a yes/no consumes is marked too: one use only.
            for draft in live if not reshows_all else []:
                await self.drafts.mark_seen(draft.id, draft.revision)
        except Exception as exc:
            logger.warning("food agent could not record the last shown preview: %s", type(exc).__name__)
            return _result("I couldn't reach your log just now, so nothing changed. Try again in a moment.",
                           "error", "not_applied")
        result: FoodTurnResult | None = None
        try:
            result = await self._turn(message, decision, recent, now, session_ref, {d.id for d in live})
            # Canvas' existing reply/clarification contract is 1000 characters.
            # Invalidate BEFORE returning, including compound reconciliation replies.
            if len(result.reply) > 1000 and (result.preview or result.shown_draft_ids):
                ids = set(result.shown_draft_ids)
                if result.preview:
                    ids.add(result.preview.draft_id)
                for draft_id in ids:
                    draft = await self.drafts.get(draft_id)
                    if draft:
                        await self.drafts.mark_seen(draft.id, draft.revision)
                        await self.drafts.save(draft.with_(pending=None), draft.revision)
                result = _result("This meal is too large to show completely. Please split it into smaller meals before confirming.",
                                 "needs_detail", "not_applied")
            return await self._with_quick_replies(result)
        finally:
            # Check two: clear every binding this turn did not show, even if the turn raised.
            await self._settle_bindings(shown_before, result)

    async def _with_quick_replies(self, result: FoodTurnResult) -> FoodTurnResult:
        """Attach one-tap answers. Buttons are a convenience only: any failure means none."""
        try:
            replies = await self._quick_replies(result)
        except Exception as exc:
            logger.info("food agent quick replies skipped: %s", type(exc).__name__)
            return result
        return replace(result, quick_replies=replies) if replies else result

    async def _quick_replies(self, result: FoodTurnResult) -> tuple[str, ...]:
        if result.handed_off or result.outcome.status in {"error", "conflict", "unknown"}:
            return ()
        if result.preview is not None:
            return ("Save it" if result.reply.endswith("Save the change?") else "Log it", "Cancel")
        question = result.question
        if not question or not result.draft_id or result.shown_draft_ids:
            return ()
        if question.endswith("raw or cooked?"):
            return ("Raw", "Cooked")
        draft = await self.drafts.get(result.draft_id)
        if draft is None or draft.state not in EDITABLE_STATES:
            return ()
        item = next((i for i in draft.items if i.resolved is not None and i.multiplier is None), None)
        if item is None:
            return ()
        return portion_choices(item.resolved.serving_basis) + ("Cancel",)  # type: ignore[union-attr]

    async def _settle_bindings(self, shown_before: list[AgentDraft], result: FoodTurnResult | None) -> None:
        """B1: a preview stays confirmable only if this turn's reply showed it.

        Any other binding is cleared in the DB (survives restarts). A draft that
        changed during the turn fails the CAS and keeps whatever the turn wrote.
        A failed clear is safe: the seen marker already makes the binding unusable.
        """
        keep: set[str] = set()
        if result is not None:
            keep.update(result.shown_draft_ids)
            if result.preview is not None:
                keep.add(result.preview.draft_id)
        for draft in shown_before:
            if draft.id in keep:
                continue
            try:
                await self.drafts.save(draft.with_(state="collecting", pending=None), draft.revision)
            except Exception as exc:
                logger.warning("food agent could not clear a preview binding: %s", type(exc).__name__)

    async def _turn(self, message: str, decision: str, recent: list[AgentDraft], now: datetime,
                    session_ref: str | None, live: set[str]) -> FoodTurnResult:
        try:
            reconciled = await self._reconcile(recent, now, decision, session_ref)
        except Exception as exc:
            logger.warning("food agent could not reconcile: %s", type(exc).__name__)
            return _result("I couldn't reach your log just now. Try again in a moment.", "error", "unknown")
        if reconciled is not None:
            if decision != "other" or reconciled.outcome.effect == "unknown":
                return reconciled  # "yes" again after "could not verify": the check IS the answer
            try:
                recent = await self.drafts.recent(now - self.recent_window)
            except Exception:
                return reconciled
            rest = await self._handle(message, decision, recent, now, session_ref, live)
            combined = " ".join(part for part in (reconciled.reply, rest.reply) if part)
            if rest.preview and len(combined) > 1000:
                return rest
            return FoodTurnResult(
                reply=combined,
                handed_off=rest.handed_off, preview=rest.preview, receipt=reconciled.receipt,
                outcome=rest.outcome if rest.outcome.effect != "not_attempted" else reconciled.outcome,
                draft_id=rest.draft_id or reconciled.draft_id, question=rest.question)
        return await self._handle(message, decision, recent, now, session_ref, live)

    async def _handle(self, message: str, decision: str, recent: list[AgentDraft], now: datetime,
                      session_ref: str | None, live: set[str]) -> FoodTurnResult:
        # The active draft (a preview or question pending) comes first; clearing a
        # stale binding touches updated_at, so recency alone cannot decide.
        open_ = sorted((d for d in recent if d.state in EDITABLE_STATES), key=lambda d: d.pending is None)
        # Only a preview that was the LAST thing shown may be confirmed or cancelled.
        bound = [d for d in open_ if d.pending_confirm() is not None and d.id in live
                 and d.pending_confirm().session_ref == session_ref]
        unseen = [d for d in open_ if d.pending_confirm() is not None
                  and d.pending_confirm().session_ref != session_ref]
        if decision == "yes" and not bound and unseen:
            return await self._reshow(unseen[0], now, session_ref=session_ref)
        if decision == "yes" and len(bound) + len(unseen) > 1:
            return self._ask_which(bound + unseen)
        if decision == "yes" and len(bound) == 1:
            return await self._confirm(bound[0], now, session_ref)
        if decision == "yes" and len(bound) > 1:
            return self._ask_which(bound)
        # S1: a yes/no with no preview showing answers a question: the model interprets it.
        if decision == "no" and len(bound) == 1:
            return await self._cancel(bound[0])

        try:
            turn = await self.interpret(message, self._context(open_, recent, now))
            if not isinstance(turn, AgentTurn):
                turn = AgentTurn.model_validate_json(json.dumps(turn))
        except Exception as exc:
            logger.warning("food agent interpreter failed: %s", type(exc).__name__)
            if not open_:
                # Nothing of ours is open: let the old path answer (it has no food-write tools).
                return _result("", "error", handed_off=True)
            return _result("Sorry, I couldn't read that. Can you say it again?", "error")

        actions = list(turn.actions)
        types = [a.type for a in actions]
        if "not_food" in types:
            # A short reply ("Yes", "Do 1 slice") while a draft is waiting on a question
            # answers that draft; it never leaves for the old coach path.
            waiting = next((d for d in open_ if d.question()), None)
            if waiting is not None and (decision in ("yes", "no") or len(message.split()) <= 4):
                question = waiting.question() or "Tell me again what you had?"
                return _result(question, "needs_detail", draft_id=waiting.id, question=question)
            return _result("", handed_off=True)
        if "cancel" in types and not EDIT_TYPES.intersection(types):
            target = bound[0] if len(bound) == 1 else (open_[0] if open_ else None)
            return await self._cancel(target) if target else _result("There's nothing to cancel.")
        if EDIT_TYPES.intersection(types):
            return await self._edit(actions, open_, recent, now, session_ref)
        confirm_action = next((a for a in actions if a.type == "confirm"), None)
        if confirm_action is not None:
            return await self._model_confirm(confirm_action, decision, bound, open_, now, session_ref)
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
        asking = next((d for d in open_ if d.question()), None)
        return {
            "now": now.isoformat(),
            # The user may be answering this (even with just "yes" or "no").
            "last_question": None if asking is None else {"draft_ref": asking.id, "question": asking.question()},
            "open_drafts": [{"draft_ref": d.id, "revision": d.revision,
                             "awaiting_yes": d.pending_confirm() is not None, "question": d.question(),
                             "items": [self._item_context(i) for i in d.items]} for d in open_],
            "last_logged": None if last is None else {"items": [self._item_context(i) for i in last.items]},
        }

    # ------------------------------------------------------------ confirmation

    def _ask_which(self, bound: list[AgentDraft]) -> FoodTurnResult:
        names = [" and ".join(item_label(i) for i in d.items) for d in bound]
        reply = f"You have {len(bound)} meals waiting to log: {_join(names)}. Which one should I log?"
        if len(reply) > 1000:
            return _result("Several meals are waiting. Which food should I show you first?", "needs_detail")
        return _result(reply, "needs_detail", question=reply, shown_draft_ids=tuple(d.id for d in bound))

    async def _model_confirm(self, action, decision: str, bound: list[AgentDraft], open_: list[AgentDraft],
                             now: datetime, session_ref: str | None) -> FoodTurnResult:
        """The model thinks the user confirmed. That is a hint: it never commits."""
        if len(bound) > 1:
            chosen = next((d for d in bound if d.id == str(action.draft_ref).lower()), None)
            if chosen is None:
                return self._ask_which(bound)
            for other in bound:  # unbind the others; their items stay as they were
                if other.id != chosen.id:
                    await self.drafts.save(other.with_(pending=None), other.revision)
            return await self._reshow(chosen, now, session_ref=session_ref)
        if len(bound) == 1:
            # This turn marked the old binding seen: show it again under a fresh revision.
            return await self._reshow(bound[0], now, prefix="Nothing changed yet.", session_ref=session_ref)
        ready = next((d for d in open_ if d.question() is None and items_ready(d.items)), None)
        if ready is not None:  # its preview was not the last thing shown: show it again, never commit
            return await self._reshow(ready, now, session_ref=session_ref)
        return _result("There's nothing waiting to log right now.")

    async def _confirm(self, draft: AgentDraft, now: datetime, session_ref: str | None) -> FoodTurnResult:
        binding = draft.pending_confirm()
        assert binding is not None
        draft, questions = await self._refresh(draft)
        if questions:
            return _result(questions[0], "needs_detail", "not_applied", question=questions[0])
        current_hash = content_hash(list(draft.items))
        if binding.revision != draft.revision or binding.content_hash != current_hash:
            return await self._reshow(draft, now, prefix="That meal changed since I showed it.",
                                      session_ref=session_ref)
        pinned = draft.with_(meal_type=draft.meal_type or _default_meal_type(now))
        try:
            plan = build_commit(pinned, current_hash)
        except (DraftNotReady, ValueError) as exc:
            logger.info("draft not committable: %s", exc)
            return await self._reshow(draft, now, session_ref=session_ref)
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
                done = await self._reconcile_one(fresh, now, session_ref, reacknowledge=True)
                if done is not None:
                    return done
            if fresh is not None and fresh.state in EDITABLE_STATES:
                return await self._reshow(fresh, now, prefix="That meal changed since I showed it.",
                                          session_ref=session_ref)
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
            return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
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

    async def _abandon_unlanded(self, draft: AgentDraft, intent: CommitIntent) -> FoodTurnResult | None:
        """N1: "no" after an unverified save that never reached the DB cancels it instead of retrying.

        Safe against a late arrival of the original request: the commit re-checks
        ``state='ready'`` under a row lock, and a canceled draft fails that check.
        """
        if await self.drafts.fetch_operation(op_key(draft.id, intent.revision)) is not None:
            return None
        if not await self.drafts.cancel(draft.id, draft.revision):
            return None  # it committed meanwhile: reconcile normally
        return _result("OK, not logged.", "ok", "not_applied", draft_id=draft.id)

    async def _reconcile(self, recent: list[AgentDraft], now: datetime, decision: str,
                         session_ref: str | None) -> FoodTurnResult | None:
        """Finish or verify an earlier commit whose outcome was not verified. Same key, same hash."""
        for draft in await self.drafts.recovery():  # owner-scoped, independent of conversational recency
            intent = draft.commit_intent()
            if intent is None:
                continue
            if draft.state == "committed":
                if draft.committed_operation_key != op_key(draft.id, intent.revision):
                    continue
                if await self.drafts.is_acknowledged(draft.id, intent.revision):
                    continue  # older unresolved intents must remain discoverable
                return await self._reconcile_one(draft, now, session_ref)
            if draft.state == "ready" and draft.revision == intent.revision:
                if decision == "no":
                    abandoned = await self._abandon_unlanded(draft, intent)
                    if abandoned is not None:
                        return abandoned
                    draft = await self.drafts.get(draft.id) or draft
                return await self._reconcile_one(draft, now, session_ref)
        return None

    async def _reconcile_one(self, draft: AgentDraft, now: datetime, session_ref: str | None, *,
                             reacknowledge: bool = False) -> FoodTurnResult | None:
        intent = draft.commit_intent()
        if intent is None:
            return None
        operation = await self.drafts.fetch_operation(op_key(draft.id, intent.revision))
        original = ((operation or {}).get("stored_response") or {}).get("entry")
        if original:
            values = {k: original.get(k) for k in ("name", "meal", "macro_source", "calories", "protein", "carbs", "fat", "fiber")}
            values["day"] = draft.log_date
            from food_agent.drafts import request_hash_of
            if request_hash_of(values) != intent.request_hash:
                return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
            unknown = tuple(k for k in ("calories", "protein", "carbs", "fat", "fiber") if values[k] is None)
            plan = CommitPlan(values, {}, intent.request_hash, unknown, NutrientPanel(**{k: values[k] for k in
                              ("calories", "protein", "carbs", "fat", "fiber")}))
        else:
            if draft.state == "committed":
                # Operation retention expired: never create a second entry or forget recovery facts.
                return _result(COULD_NOT_VERIFY, "unknown", "unknown", draft_id=draft.id)
            draft, questions = await self._refresh(draft)
            try:
                plan = None if questions else build_commit(draft, intent.content_hash)
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
            # Bind the fresh preview to the session receiving this reply, never the draft's creator.
            return await self._reshow(draft, now, prefix="That meal changed before it was saved.",
                                      session_ref=session_ref)
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
        if refs:  # refs are unique across the user's recent drafts
            holder = next((d for d in open_ if refs <= {i.item_ref for i in d.items}), None)
            if holder is not None:
                return holder, False
        if current is not None:
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
            if target.is_amend:
                # S2: removing a whole logged meal is not an amend; say plainly nothing changed.
                return _result("That meal is already saved, so I didn't change it. To take it off, delete it "
                               "in Today.", "needs_detail", "not_applied", draft_id=None if is_new else target.id)
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
                                     content_hash=content_hash(items), session_ref=session_ref).to_pending_question()
            changed = target.with_(items=tuple(items), state="collecting", pending=binding)
            trial = self._preview(changed.with_(revision=new_revision), now)
            if trial is not None and len(self._preview_text(changed, trial, notes)) > 1000:
                question = OVERSIZED
                changed = changed.with_(state="needs_detail", pending=question)
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
            return await self._reshow(saved, now, session_ref=session_ref)
        preview = self._preview(saved, now)
        if preview is None:
            return await self._reshow(saved, now, session_ref=session_ref)
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
            found = portion_multiplier(portion, item.resolved.serving_basis, identity)
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
            # Changes ("no tomato", "extra patty") keep the item: it is counted as the
            # standard recipe and the preview says so (food_agent.modifications).
            if item.resolved is not None and item.resolved.panel.calories is not None:
                if item.multiplier is None and not questions:
                    found = portion_multiplier(item.portion, item.resolved.serving_basis, item.identity)
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
                found = portion_multiplier(item.portion, result.food.serving_basis, item.identity)
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

    async def _refresh(self, draft: AgentDraft) -> tuple[AgentDraft, list[str]]:
        questions: list[str] = []
        items = await self._resolve(list(draft.items), [], questions)
        return draft.with_(items=tuple(items)), questions

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
        from food_agent.source_policy import attribution_texts
        source_notes = [text for item in draft.items if item.resolved for text in
                        attribution_texts([item.resolved.attribution, item.resolved.component_attributions])]
        from food_agent.modifications import adjustment_note
        adjusted = adjustment_note(draft.items)
        for note in dict.fromkeys([*notes, *([adjusted] if adjusted else []), *source_notes]):
            text += f" {note[0].upper()}{note[1:]}."
        text += " Save the change?" if draft.is_amend else " Log it?"
        return text

    async def _reshow(self, draft: AgentDraft, now: datetime, *, session_ref: str | None,
                      prefix: str = "") -> FoodTurnResult:
        """Bind a fresh preview to the draft's CURRENT content and show it. Never commits.

        ``session_ref`` is required: the binding always names the session that receives
        this reply, so only that session's yes can confirm what it was shown.
        """
        lead = f"{prefix} " if prefix else ""
        draft, questions = await self._refresh(draft)
        if questions:
            return _result(questions[0], "needs_detail", question=questions[0])
        if not items_ready(draft.items) or self._calorie_questions(draft.items):
            question = draft.question() or (self._calorie_questions(draft.items) or [
                "Tell me again what you had?"])[0]
            return _result(f"{lead}{question}", "needs_detail", draft_id=draft.id, question=question)
        binding = PendingConfirm(draft_id=draft.id, revision=draft.revision + 1,
                                 content_hash=content_hash(list(draft.items)),
                                 session_ref=session_ref).to_pending_question()
        candidate = draft.with_(state="collecting", pending=binding)
        trial = self._preview(candidate.with_(revision=draft.revision + 1), now)
        if trial is not None and len(lead + self._preview_text(candidate, trial, [])) > 1000:
            await self.drafts.save(draft.with_(state="needs_detail", pending=OVERSIZED), draft.revision)
            return _result(OVERSIZED, "needs_detail", "not_applied", question=OVERSIZED)
        saved = await self.drafts.save(candidate, draft.revision)
        if saved is None or saved.pending_confirm() is None or saved.pending_confirm().revision != saved.revision:
            return _result(f"{lead}That meal changed just now. Check Today.", "conflict", "not_applied")
        preview = self._preview(saved, now)
        if preview is None:
            return _result(f"{lead}Tell me again what you had?", "needs_detail", draft_id=saved.id)
        return _result(f"{lead}{self._preview_text(saved, preview, [])}", draft_id=saved.id, preview=preview)


def items_ready(items: Sequence[DraftItemState]) -> bool:
    return bool(items) and all(i.resolved is not None and i.multiplier is not None for i in items)


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
    from meal_drafts import default_meal_type  # reuse the existing slot rule
    return default_meal_type(now)


__all__ = ["FoodAgent", "FoodTurnResult", "nutrient_text", "items_ready"]
