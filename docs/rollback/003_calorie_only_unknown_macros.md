# Migration 003: honest calorie-only entries

Status: proposed, NOT approved/applied to production. Required before releasing the calorie-only write path. Only nutrition_entries protein/carbs/fat/fiber become nullable; no existing row, default, table, user or value is changed. Calories remain required. Rollup numbers are known-only subtotals; API/native explicitly label incompleteness and never infer missing nutrient amounts.

## Release safeguards built into the code (fail closed)

Pushing this code does not apply 003 and does not enable calorie-only writes. Each step below is a separate owner decision.

1. **Migration hold.** `server/src/migrations.py` lists 003 in `APPROVAL_REQUIRED`. The Render pre-deploy runner applies 000–002 as before but **holds 003** (and anything after it) unless the service env contains `APPROVED_MIGRATIONS=003_calorie_only_unknown_macros.sql:<sha256>` with the exact checksum of the file being deployed. A missing, malformed or stale checksum keeps it held. The runner prints `migrations held (owner approval required): ...`. A held tail does not make the service unready; `/api/readiness` reports it under `held`.
2. **Compatibility gate.** A calorie-only (NULL-nutrient) write requires all of:
   - operator switch `CALORIE_ONLY_WRITES_ENABLED=true` (exact word; unset by default);
   - the calling client sends `X-Nutrient-Contract: nullable-v1` (only builds with optional FoodEntry nutrients send it; HTTP and both voice websockets);
   - if `CALORIE_ONLY_WRITES_USER_IDS` is set, the authenticated user is on that comma-separated canary list (malformed list = closed);
   - the live schema actually has the four columns nullable (checked from `information_schema`, so it also closes after a NOT NULL rollback).
   Otherwise voice `log_meal` asks for protein/carbs/fat as before and writes nothing. The store layer re-checks the same gate inside the write transaction (`CalorieOnlyWriteBlocked`), so no other path can store NULL nutrients early.
3. **Catalog isolation.** Calorie-only rows never become food catalog evidence, and `scripts/backfill_food_catalog.py` skips rows with any NULL nutrient.

Note: the client header proves only that the *writing* device reads NULLs. Another device on the same account may still run an old build. Keep the switch off (or limit it with the canary list) until every device on the enabled accounts runs a nullable-aware build.

## Rollout order (each step needs separate owner approval)

1. Push/deploy code with the switch unset. 003 is held. Verify readiness `held: ["003_calorie_only_unknown_macros.sql"]` and that voice calorie-only requests still ask.
2. Ship the nullable-aware iOS build (sends `nullable-v1`) to the target devices; confirm on device.
3. Run `python scripts/nutrient_rollback.py` (read-only preflight) against the target DB; record the output.
4. Set `APPROVED_MIGRATIONS=003_calorie_only_unknown_macros.sql:<sha256>` and redeploy so the runner applies 003. Re-run the preflight: columns nullable, 0 unknown rows.
5. Enable `CALORIE_ONLY_WRITES_ENABLED=true` with `CALORIE_ONLY_WRITES_USER_IDS=<synthetic or owner canary>`; run the live voice checks; widen only after review.

Preflight evidence still required (independent reviewer and owner approval):
- Verify exact immutable migration checksum through src/migrations.py, current applied 001/002 checksums, no drift, database/service identity, and deploy SHA. Do not expose credentials.
- Record schema-only baseline and scoped synthetic row/totals baseline. Preserve owner data; do not clone/read owner entries for QA.
- Exercise upgrade and rollback on isolated PostgreSQL, then synthetic live account only after approval. (Automated isolated-PostgreSQL coverage: `server/tests/test_release_safeguards.py` with `MMMACROS_TEST_PG_ADMIN_URL` set to a throwaway local server.)
- Verify native null decoding, unknown-macro display and refresh on a real device. No owner-device claims from simulator.

## Rollback

`python scripts/nutrient_rollback.py` (default) is READ-ONLY: it reports ledger state, column nullability, the unknown-row COUNT (no row contents), the switch state, the rollback SQL checksum and which rollback is safe.

1. **Forward-disable (always first, always safe).** Unset `CALORIE_ONLY_WRITES_ENABLED` (or set `false`) and redeploy/restart. Voice returns to asking for nutrients. Existing NULLs stay NULL and readable by nullable-aware builds. No data or schema change.
2. **Restore NOT NULL (only if zero unknown rows ever existed or remain).** Separate owner approval required. Run
   `python scripts/nutrient_rollback.py --restore-not-null --confirm-checksum <sha256 of server/rollback/003_restore_nutrients_not_null.sql>`.
   It refuses unless the checksum matches, the switch is off, and 003 is applied. In ONE transaction under an ACCESS EXCLUSIVE lock it re-counts unknown rows, aborts with no change if any exist, restores NOT NULL, and records itself in `schema_rollbacks`. It never edits or deletes `schema_migrations` (the 003 row and checksum stay), so the runner does not re-apply 003. Re-enabling calorie-only writes later needs a new reviewed forward migration.
   The rollback SQL lives in `server/rollback/`, outside `server/migrations/`, so the deploy runner can never apply it.
3. **After any NULL entry exists:** do NOT replace NULL with 0, delete entries, copy owner data, or force a constraint restoration. Retain data/schema and use forward-disable. Do not deploy pre-003 code or a pre-003 native binary over accounts with unknown-nutrient rows: it can reject decoding or misstate totals. Returning to old code requires a new owner-approved data/compatibility plan.

This document is not an approval or evidence of a backup, migration run or live success.
