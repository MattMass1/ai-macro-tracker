# Migration 003: honest calorie-only entries

Status: proposed, NOT approved/applied to production. Required before releasing the calorie-only write path. Only nutrition_entries protein/carbs/fat/fiber become nullable; no existing row, default, table, user or value is changed. Calories remain required. Rollup numbers are known-only subtotals; API/native explicitly label incompleteness and never infer missing nutrient amounts.

Preflight (independent reviewer and owner approval required):
- Verify exact immutable migration checksum through src/migrations.py, current applied 001/002 checksums, no drift, database/service identity, and deploy SHA. Do not expose credentials.
- Record schema-only baseline and scoped synthetic row/totals baseline. Preserve owner data; do not clone/read owner entries for QA.
- Exercise upgrade and rollback on isolated PostgreSQL, then synthetic live account only after approval. Required concurrency tests exercise plan locks on real Postgres too.
- Verify native null decoding, unknown-macro display and refresh; old binaries with nonoptional FoodEntry fields cannot read nullable meals. Do NOT enable production calorie-only logging until the owner-approved client rollout is ready. No owner-device claims from simulator.
- Migration runner is all-pending; a master push may auto-deploy and apply it. Do not push this candidate before the separate migration/rollout gate.

Rollback:
- Prefer a forward hotfix that disables calorie-only writes and preserves NULLs/read support. Do not deploy pre-003 code or a pre-003 native binary over accounts with unknown-nutrient rows: it can reject decoding or misstate totals.
- Before any NULL entry exists, restoring NOT NULL can run inside a transaction only after explicit null-count=0 invariant check under table lock and separate approval. Preserve all migration ledger evidence; use a new reviewed rollback migration, never edit checksums/history ad hoc.
- After a NULL entry exists, do NOT replace NULL with 0, delete entries, copy owner data, or force a constraint restoration. Retain data/schema and use the forward-disable rollback. Returning to old code requires a new owner-approved data/compatibility plan.

This document is not an approval or evidence of a backup, migration run or live success.
