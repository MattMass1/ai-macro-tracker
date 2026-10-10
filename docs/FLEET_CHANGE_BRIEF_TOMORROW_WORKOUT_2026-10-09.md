# Tomorrow-only workout changes

- Request: Matthew narrowed the feature to changing tomorrow's workout only. Recurring routine changes and arbitrary future dates are out of scope.
- Owner: current Codex builder, isolated branch `feat/tomorrow-workout`.
- Backend scope: Canvas tool/prompt, existing day-plan confirmation and guarded store write, workout-plan/outlook reads, synthetic regressions, and a shared JSON fixture. No schema migration, provider/model/env/cron change.
- Design: `propose_tomorrow_workout(workout_type)` resolves the user's words against saved routine days and copies that day's exact exercise prescription. A dated native preview must be confirmed. Persist only tomorrow's `daily_workout_plans` row; recurring rotation, today's session, and logged sets remain unchanged. Reuse revision/context checks, operation readback, and uncertain-write reconciliation. Both text and ElevenLabs voice use the same handler.
- Dates: tomorrow means the next logging day in the app's existing timezone/4am policy. Date is computed server-side, not supplied by the model. Approvals expire when their preparation logging day ends; the store rechecks this after locking.
- Contract: add optional `tomorrow` to `/api/plan`, containing date/type/exercise names only when a day override exists. Existing rotation slots keep their original meaning. The coach outlook exposes this dated override separately. A canonical fixture must exercise server serialization and Swift decoding.
- iOS scope: separate commit for optional tomorrow payload decoding and a read-only Tomorrow card. Existing generic preview/confirmation already works on installed clients. Native UI changes require Xcode build/testing before release.
- Data: no existing production workout changes during development or canary. Use synthetic stores for confirm/cancel/replay/tenancy tests. Live canary may stage and cancel a preview only. Normal chat/usage records may be added. Snapshot live workout plan before/after.
- Prompt inventory: add one tomorrow-only proposal tool and explicit routing instructions. Existing today-only, routine-edit, food, model and search behavior remain unchanged.
- Review: independent correctness/tenancy review (Codex fallback while Fable is capped), plus independent release verification. Rollback by reverting application commits; additive response fields can be ignored and saved day rows are preserved.
- Verification: test copied prescription, tomorrow date, confirmation/cancellation, tenant isolation, stale context, rollover under lock, uncertain replay, activation next day, current-day/rotation invariants, full backend tests, Swift fixture/build, and read-only production preview/cancel canary.
- Status: implementation pending.
