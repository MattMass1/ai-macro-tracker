# Voice actions verification

## Requirements and current evidence

| Requirement | Evidence | Remaining proof |
| --- | --- | --- |
| Speak with GPT-Live | Full authenticated local-app canary processed synthetic PCM and returned 2,342,400 audio bytes, executed real tools, and closed normally | Physical microphone and speaker confirmation |
| Read today's consumed/remaining macros | Real speech canary spoke correct remaining 1,800 calories and 120 g protein after saving preset; actual database readback | Production authenticated voice readback |
| Log meals with verified macros | Full speech canary saved verified synthetic preset (200 calories, 30 g protein); separate repeated-call test inserted only once | Production readback |
| Find replacement exercises | Real provider called `get_today_session` and `get_library` before replacement | Production exercise library flow |
| Change today's exercise without changing routine | Full speech canary saved Dumbbell Bench Press for today, kept Fly and permanent routine unchanged; separate next-day test uses original routine | Production migration and request |
| Log spoken sets/reps/weights | Full speech canary persisted all five sets: four at 120 lb × 10 reps and one at 130 lb × 8 reps; voice confirmed all five saved | Production readback |
| Edit today's planned sets/reps/rest | Full speech canary saved 4 sets × 10 reps with 90 seconds rest; snapshot/library validation and canonical native fixture tests passed | Production readback |
| iPhone access and refreshed state | 60 simulator tests; final signed physical build installed on connected iPhone | Final launch blocked by locked phone; user reports microphone, speech and production writes |
| Isolation and retries | Local PostgreSQL two-tenant verification; repeated provider call ID single insert; independently reproduced shutdown race now prevents post-close writes | Production baseline and canary account |

## Automated and independent review

- Root ran final full backend suite: 361 passed in 1.73 seconds.
- Final simulator result independently inspected: 60 passed, zero failures/skips, `ios-tests-prescription.xcresult`.
- Signed build and `codesign --verify --deep --strict` succeeded outside the sandbox. Existing project team and identity were used, with no Keychain security changes.
- `devicectl` installed `com.biz21.macrotracker` without uninstalling the existing app. The earlier candidate launched successfully; the final prescription-display candidate installed successfully but its launch was denied because the phone was locked.
- Final app executable SHA-256: `ef39ed141e84b5a34a2eb64529cf38596e23d0e660cb5e9487ff26f599e083c3`; debug library SHA-256: `638416436d76c288b140e1871a1588e4b1d728606f786fcde871ced1ce95dfbf`.
- Independent Codex review identified and rechecked fixes for close/backpressure action cancellation, stale-token logout, inaccurate WebSocket 403 messaging, and date-boundary/concurrent workout edits. The reviewer independently ran 51 live tests and inspected the 60-test native result and rendered planned-prescription row. No remaining concrete code blockers were reported in the reviewed scope.
- Repository-required cross-family Claude review was not performed: automatic approval review rejected transmitting private repository source to Claude without explicit user permission. Independent Codex review is recorded as a fallback, not equivalent cross-family completion.
- No credentials found by scoped source signature scan. Gitignored configuration remains unstaged.

## Database and rollback

An isolated loopback-only PostgreSQL instance was used, with synthetic accounts only. Baseline migrations 000 and 001 were applied before 002. The new migration preserved the baseline routine data. A second migration run performed no changes. No production database was read or written during these checks.

Final combined evidence: `live-persistence-canary-final-result.json`, synthetic account `9b2dfb98-e7d4-48c1-906d-a3154458d250`. It exercised synthetic spoken input through the mounted authenticated app, actual GPT-Live delegation, real local PostgreSQL writes, and database readback. Five `app.data_changed` events reached the client. Other tenants were unchanged. An earlier attempt stopped because its harness incorrectly required a separate read tool after a write already returned fresh totals; the final harness checks persisted outcomes and passed.

A compatible rollback must retain the exact 002 migration file, ledger entry, and table/data while reverting runtime changes. An old migration inventory rejects the newer database. Do not delete data or migration history to force rollback.

The final canary verified this against the actual migrated database: current inventory and rollback inventory retaining 002 are compatible; baseline-only inventory is incompatible with unknown migration 002. Retained migration SHA-256: `ba7bf701157d32e60b2588321bcece4f9d0b1b7b14e7c2d92935a3419ffe20a1`.

## Release status

The updated iPhone app is installed. The new backend actions are not deployed yet. Render dashboard GitHub sign-in was blocked by automatic approval review because OAuth could grant account access. The user must sign in or explicitly authorize the intended sign-in; any new access grant must be reviewed separately.

The goal remains incomplete until deployment, real authenticated production actions, and physical audio verification succeed. Evidence files are under `/Users/mmasson/voice-actions-evidence/`; provider/database canaries contain only synthetic data.
