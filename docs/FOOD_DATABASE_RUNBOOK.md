# Food database migration and backfill runbook

This change is additive. It does not delete or rewrite legacy nutrition values or
`macro_source` text. Run these commands only against an approved restored or
throwaway database before any reviewed deployment.

Use the restored database URL explicitly in every command and save each output
as release evidence. Do not run these commands against production during review.

1. Enable pipeline failure propagation before running any recorded command:
   `set -o pipefail` (run the commands from Bash). Capture the executable
   legacy-only baseline:
   `psql "$RESTORED_DATABASE_URL" -v ON_ERROR_STOP=1 -f server/scripts/food_catalog_pre_migration.sql > pre-migration.txt`.
2. Apply ordered migrations:
   `DATABASE_URL="$RESTORED_DATABASE_URL" server/.venv/bin/python server/src/migrations.py`.
3. Run the seed and backfill previews. They print aggregate projections only:
   `python server/scripts/seed_verified_foods.py` and
   `python server/scripts/backfill_food_catalog.py --database-url "$RESTORED_DATABASE_URL"`.
4. Apply the trusted seed, then the legacy backfill:
   `python server/scripts/seed_verified_foods.py --apply --database-url "$RESTORED_DATABASE_URL"` and
   `python server/scripts/backfill_food_catalog.py --apply --database-url "$RESTORED_DATABASE_URL"`.
5. Reapply both commands. Seed `new_items` and backfill `logical_rows_changed`
   must both be `0`.
6. Capture post-migration checks:
   `psql "$RESTORED_DATABASE_URL" -v ON_ERROR_STOP=1 -f server/scripts/food_catalog_invariants.sql > post-migration.txt`.
   Legacy counts and both rollup deltas must equal the baseline. Cross-tenant,
   unlinked, duplicate-key, and catalog cross-item counts must be zero.
7. Run migrations a second time; it must report `migrations applied: 0`.
8. Record `sha256sum server/migrations/*.sql`, the dry-run/apply/reapply JSON,
   both invariant outputs, and the restored database identifier for independent review.

The migration runner reads only immutable files under `server/migrations`, holds
a PostgreSQL advisory lock, applies each file in its own transaction, and refuses
unknown files or checksum drift. Normal server
startup only verifies compatibility. `/api/readiness` is bearer-token protected
and performs read-only connectivity and migration checks. Render intentionally
uses public `/health` for process liveness only; Ops must verify both Render's
liveness result and protected `/api/readiness` before go-live.

Render officially propagates the service environment to `preDeployCommand`.
The migration runner nevertheless fails nonzero when `DATABASE_URL` is missing,
malformed, unreachable, or migration-incompatible. Its deploy preflight prints
only `database=<name>` and a coarse `host_class`; it never prints a hostname,
username, password, query string, or full URL. Treat that line plus the final
`migrations applied: N` line as the pre-deploy assertion.

Rollback is an application revert. Leave additive catalog tables and columns in
place and ignored. Never delete legacy history or drop the new schema as part of
routine rollback.

Every migration, seed, backfill, and invariant command must exit successfully
before continuing. The backfill script does not catch database or row failures;
an exception exits nonzero and must stop the drill.

`POST /api/log` accepts an optional `Idempotency-Key` header. Clients that omit it
retain the legacy behavior. With a key, the operation claim, nutrition insert,
catalog link/snapshot, rollup refresh, and canonical response snapshot commit in
one transaction. A same-body retry replays that snapshot; reusing a key for a
different canonical request returns HTTP 409.
