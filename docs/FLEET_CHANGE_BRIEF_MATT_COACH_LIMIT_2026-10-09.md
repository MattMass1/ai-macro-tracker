# Remove Matthew's daily coach cap

- Request/owner: Matthew explicitly requested removal for his account. Current Codex builder owns this backend-only change.
- Scope: `server/src/store.py`, quota regression tests, this brief. No iOS, provider, prompt, environment, schema, or cron changes.
- History: design commit `39798f3` (August 18) specified a default 50-message daily cap as a circuit breaker for trusted friends using Matt's key. Implementation commit `7d15535` (August 19, recorded writer Fable) introduced the shared store enforcement. Neither explains a calculation behind 50.
- Design: exempt only the authenticated immutable account ID `be6333cc-e4c1-48f2-adb1-5e7f14dbf7c2` recorded in FLEET_CREED. Apply the exception at the shared chat persistence boundary used by legacy typed chat, Canvas text, and voice. Other accounts retain their existing cap. Continue recording chat and model usage; do not reset or delete counters/history.
- Identity: do not use display name, admin role, or the configurable/default legacy owner ID as an entitlement. The local `.env` connects to a local development database and does not verify production identity. Production verification must exercise the already-authenticated account on Render and confirm that a request beyond the existing cap is accepted.
- Data/contract impact: no migrations or existing-row edits; no API shape change. A single live read-only coach request may add normal chat/usage records as part of the authorized account repair; no food, workout, or profile mutations are requested. Snapshot meal records before/after that canary.
- Review: independent correctness/tenancy review before release and independent Release/Ops verification after deployment, using the creed's Codex fallback while Fable is capped.
- Verification: regressions at 50 and far beyond 50, continued enforcement for unrelated and legacy-default IDs, required authentication, existing atomic locking and history insertion tests, full backend suite, deployed commit and one read-only live coach canary.
- Rollback: revert this commit; no data rollback needed and no chat history removed.
- Evidence: owner cases failed at both 50 and 50,000 before the change; all five new regressions now pass. Full backend suite: 962 passed, 4 skipped. Independent read-only reviewer found no blockers and separately passed 10 targeted quota/exemption tests.
- Status: automated-verified and independently reviewed; production deployment and canary pending.
