# Voice actions delivery brief

## Authorized outcome

The user requests working iPhone GPT-Live conversation that can log meals, read today's consumed and remaining macros, recommend replacement exercises, edit today's workout on request, and log spoken sets/reps/weights. A substitution for today must not silently rewrite future workouts. Ambiguous exercise direction or weights require a short spoken clarification. Successful writes must be grounded in persisted tool results.

## Ownership and scope

- Orchestrator/release investigation: root agent. Owns this brief and release evidence; does not author feature code.
- Backend writer: backend agent. Owns server source/tests and any needed shared contract fixture. No iOS edits.
- iOS writer: ios agent. Owns ios source/tests and shared fixture consumption. No server edits.
- Quality/data safety: independent Claude review, with independent Codex review if Claude is unavailable; inability to perform cross-family review is reported explicitly.
- Release verification: separate verifier after implementation.

## Design

Reuse the existing GPT-Live audio bridge and application-owned authenticated coach tools. Enable server-side execution of delegated tools using the documented GPT-Live delegation contract. Keep GPT-Live 1 and the existing backend model. Preserve tenant binding, bounded queues, event allowlisting, and clean session termination. Never execute speculative transcript fragments as writes. Deduplicate provider tool calls and serialize mutations. Preserve meal source validation and existing chat behavior.

Expose today's saved workout separately from permanent routine changes, with minimal exercise edits and explicit old/new exercise names. Log each reported set including its actual weight and reps. Ask about missing units/weight rather than fabricating them. Return fresh totals and saved workout state after actions.

The iPhone reuses the native voice implementation from feature/gpt-live-coach, rebased on current master. Voice completion refreshes app data. A server-only `app.data_changed` event with no personal data may trigger refresh during a live session; its canonical fixture is shared across Python and Swift. Voice copy describes the supported actions rather than read-only coaching.

## Data and operational impact

No deletion of user records. Production test writes use an explicitly created test account; real user writes occur only from their actual requested actions. Establish baselines before any schema or production-data operation and verify isolation/idempotency. API credentials stay in server configuration and are never printed. No unrelated model, cron, or environment changes. Baseline release is 8734965; native voice source baseline is 3317792.

## Verification and release

Backend tests must cover actual delegated actions, repeated calls, failure reporting, user isolation, today-only swaps, sets/reps/weights, and macro updates. Run Swift tests and signed physical-device build. Use a real provider canary and test-account action flow, verify saved results, then install and launch the exact iPhone candidate. Confirm microphone input and spoken output on the device; simulator/fake-provider tests alone are insufficient. Backend and iOS use separate commits after review.

Rollback must be migration-compatible: revert the voice action runtime changes while retaining the byte-identical `002_daily_workout_sessions.sql` file, its migration ledger entry, and its unused table/data. A plain revert to 8734965 is invalid because the old migration checker rejects unknown migrations. Verify the rollback candidate's migration inventory against the applied schema before deploying it. Do not drop the table or remove migration history to force compatibility.

Status: implementation and independent code review complete; live persistence verification and production release gates are tracked in `VOICE_ACTIONS_VERIFICATION.md`.
