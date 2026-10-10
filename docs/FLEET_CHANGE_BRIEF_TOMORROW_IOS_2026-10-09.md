# Tomorrow workout display

- Owner/writer: Codex, following the tomorrow-only backend brief; independent Codex review while Fable is capped.
- Scope: optional `tomorrow` decoding, read-only Workouts card using existing Design B components, shared JSON decoding regressions, Xcode test resource registration. No logging controls or scheduling inputs on this card.
- Contract owner: same builder, canonical fixture `docs/agent-canvas/fixtures/tomorrow-workout.json` tested by server serialization and Swift decoding. Older server responses remain decodable.
- User behavior: after native Confirm, the existing canvas action refresh loads `/api/plan`; show tomorrow's explicit date, workout name and exercises. Existing today session and rotation stay separate.
- Allowed paths: iOS models, WorkoutsView, model tests and project resource registration. No provider, signing, credentials or app architecture changes.
- Verification: focused decoding tests and Xcode build; independent source review and release/device verification. No real workout rows changed for a UI test.
- Rollback: revert this iOS commit. Existing clients still show the generic tomorrow preview and confirmation.
