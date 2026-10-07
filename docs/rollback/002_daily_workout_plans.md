# Rollback: 002_daily_workout_plans

Scope: one additive table, `daily_workout_plans` (date-scoped "today only" workout
plans). No existing table, column or row is modified by the migration.

## Forward
Render `preDeployCommand: python src/migrations.py` applies the file in one
transaction under the migration advisory lock and records its checksum in
`schema_migrations`.

## Safe application rollback (preferred, no data loss)
The application tolerates the table being present and unused. To roll back the
code only, deploy a commit that still CONTAINS
`server/migrations/002_daily_workout_plans.sql` (the migration runner rejects a
database with an applied migration it does not know). Today's sessions then fall
back to the recurring routine; saved day plans are simply ignored.

## Full rollback (drops saved today-only plans)
Only with owner approval, because it deletes users' saved day plans:

```sql
BEGIN;
SELECT pg_advisory_lock(6291470021);
DROP TABLE IF EXISTS daily_workout_plans;
DELETE FROM schema_migrations WHERE filename = '002_daily_workout_plans.sql';
SELECT pg_advisory_unlock(6291470021);
COMMIT;
```

Then deploy a commit WITHOUT the 002 file. If the table is missing while new
code runs, `fetch_day_plan` catches `UndefinedTableError` and degrades to the
recurring routine (reads only); confirming a new today plan fails closed
(no write) until the migration is re-applied.

## Verification after either path
- `/health` returns ok.
- Read-only `migrations.migration_status(conn)` shows no drift/unknown entries
  (it only SELECTs from `schema_migrations`; there is no CLI check flag).
