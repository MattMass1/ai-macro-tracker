# Voice quota error delivery

- Goal: explain an exhausted daily coach allowance in voice instead of reporting a service connection failure.
- Owner: current Codex builder, continuing Matthew's authorized app repair.
- Scope: backend voice webhook and focused regression tests. No iOS dependency changes, credentials, production data changes, or migrations.
- Evidence: both supplied ElevenLabs conversations transcribed user speech and called `mmmacros_agent_turn`; that tool returned HTTP 502 with `daily chat cap of 50 reached`. They ended normally after the client disconnected. The WebRTC codec warning is not evidence of a fatal connection failure.
- Design: catch the typed quota exception at the authenticated, user-bound voice route and return a spoken refusal through the existing `reply` contract. Keep the quota enforced and preserve authentication and unexpected-error behavior. A limit change is awaiting Matthew's preference.
- Contract/data impact: same reply shape already used for expired voice sessions; no domain action or additional model/search call when quota rejects the request.
- Operational impact: no model, provider, prompt, environment, or cron change.
- Review: independent correctness/data-safety reviewer required before shipping; independent release verifier required for closure. Fable is capped, so an independent Codex reviewer carries review per the creed's fallback.
- Verification: regression through the real CanvasService first reproduced HTTP 502 with the exact production error, then passed with the spoken reply. It verifies speaker identity, authentication, repeated refusals, no chat inserts, and no agent/domain-tool execution. All 19 voice tests and the full backend suite (957 passed, 4 skipped) pass. Independent read-only Codex review found no correctness or tenancy blockers and independently ran the 19 voice tests.
- Rollback: revert this change's commit; no data rollback required.
- Status: automated-verified and independently reviewed. Production spoken delivery remains unverified; no claim that the allowance has been increased.
