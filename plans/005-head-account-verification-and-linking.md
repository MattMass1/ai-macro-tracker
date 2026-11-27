# Plan 005: Verify & Safely Link `massmatt516@me.com` Head Account (Production Runbook)

> **Operator instructions**: Run Step 1 (read-only) first against your Render Postgres database. **NEVER run Step 2 unless Step 1 confirms a split account.** Honor `mmmacros-backend/AGENTS.md` Hard Rule 1: **Matt's data is never deleted. Ever.** Always take a `pg_dump` backup before any write.

## Status

- **Repository / Target**: `MattMass1/mmmacros-backend` production Render Postgres (`DB_SCHEMA`)
- **Priority**: P1
- **Effort**: S
- **Risk**: LOW (Step 1 is read-only) / MED (Step 2 writes `accounts` & `devices` inside an explicit rollbackable transaction — only if needed)
- **Depends on**: none
- **Category**: operations / account-integrity
- **Planned at**: commit `ed8c683`, 2026-11-27

## Why this matters

In `mmmacros-backend/src/storage/accounts.ts:29-43`, `signInWithApple` searches `accounts` strictly by `(provider = 'apple', subject = apple.subject)`. On first Apple sign-in, it unconditionally creates a **brand-new `randomUUID()` row in `users`** with zero prior meals, presets, or targets—there is no email-based linking to an older pre-Apple head `users.id`. Additionally, if iOS defaulted to "Hide My Email", `accounts.email` stores an `@privaterelay.appleid.com` address rather than `massmatt516@me.com`.

## Step 1: Read-Only Account Audit (Run This First)

Replace `<SCHEMA>` with your `DB_SCHEMA` value from Render environment variables and run in `psql`:

```sql
SELECT
  u.id AS user_id,
  u.display_name,
  u.created_at,
  a.provider,
  a.email AS apple_email,
  a.subject AS apple_sub_prefix,
  a.last_sign_in_at,
  (SELECT count(*) FROM <SCHEMA>.devices d WHERE d.user_id = u.id AND d.revoked_at IS NULL) AS active_devices,
  (SELECT count(*) FROM <SCHEMA>.nutrition_entries n WHERE n.user_id = u.id AND n.deleted_at IS NULL) AS chat_meals,
  (SELECT count(*) FROM <SCHEMA>.manual_entries m WHERE m.user_id = u.id AND m.deleted_at IS NULL) AS manual_meals,
  (SELECT count(*) FROM <SCHEMA>.meal_presets p WHERE p.user_id = u.id AND p.active = true) AS saved_meals,
  (SELECT count(*) FROM <SCHEMA>.macro_targets t WHERE t.user_id = u.id) AS targets,
  (SELECT count(*) FROM <SCHEMA>.user_profile up WHERE up.user_id = u.id) AS has_profile
FROM <SCHEMA>.users u
LEFT JOIN <SCHEMA>.accounts a ON a.user_id = u.id
ORDER BY u.created_at ASC;
```

### Decision Tree
- **Case A — Healthy (single account):** One row has your `apple_email` (`massmatt516@me.com` or your Apple private relay email) AND holds all your `chat_meals`, `manual_meals`, `saved_meals`, `targets`, and `has_profile = 1`. **STOP — nothing to fix.**
- **Case B — Split account:**
  - Row 1 (`HEAD_USER_ID`): older `created_at`, `apple_email IS NULL`, holds your historical meals/presets/targets.
  - Row 2 (`NEW_APPLE_USER_ID`): newer `created_at`, has `apple_email` (`massmatt516@me.com` or private relay), and has 0 (or only a few recent) meals.
  - **Do NOT tap "Delete account" in the iPhone app** (that would delete any rows under `NEW_APPLE_USER_ID`). Proceed to Step 2.

## Step 2: Safe, Non-Destructive Linking Procedure (ONLY for Case B)

1. **Backup first** (mandatory):
   ```bash
   pg_dump "$DATABASE_URL" --schema=<SCHEMA> --format=custom --file="backup-before-account-link-$(date +%Y%m%d-%H%M%S).dump"
   ```
2. **Inspect any recent data logged on `NEW_APPLE_USER_ID`** (read-only):
   ```sql
   SELECT 'nutrition_entries' AS tbl, count(*) FROM <SCHEMA>.nutrition_entries WHERE user_id = '<NEW_APPLE_USER_ID>'
   UNION ALL SELECT 'manual_entries', count(*) FROM <SCHEMA>.manual_entries WHERE user_id = '<NEW_APPLE_USER_ID>'
   UNION ALL SELECT 'meal_presets', count(*) FROM <SCHEMA>.meal_presets WHERE user_id = '<NEW_APPLE_USER_ID>'
   UNION ALL SELECT 'user_profile', count(*) FROM <SCHEMA>.user_profile WHERE user_id = '<NEW_APPLE_USER_ID>'
   UNION ALL SELECT 'macro_targets', count(*) FROM <SCHEMA>.macro_targets WHERE user_id = '<NEW_APPLE_USER_ID>';
   ```
   - If `NEW_APPLE_USER_ID` has **zero** meals/presets you care about (only an empty shell + maybe `user_profile`), run the clean link transaction below.
   - If `NEW_APPLE_USER_ID` has recent meals you want merged into `HEAD_USER_ID`, STOP and report the row counts first so we migrate `days`/`meals`/`nutrition_entries`/`manual_entries` and re-run rollups cleanly without PK collisions on `(user_id, date)`.
3. **Transaction-guarded link (when `NEW_APPLE_USER_ID` has no conflicting meals):**
   ```sql
   BEGIN;

   -- 1. Verify HEAD_USER_ID has no existing Apple link
   SELECT count(*) AS head_apple_links FROM <SCHEMA>.accounts WHERE user_id = '<HEAD_USER_ID>';
   -- Must be 0 before continuing!

   -- 2. Re-point your Apple account identity to HEAD_USER_ID
   UPDATE <SCHEMA>.accounts
   SET user_id = '<HEAD_USER_ID>'
   WHERE user_id = '<NEW_APPLE_USER_ID>' AND provider = 'apple';

   -- 3. Re-point active device tokens from your phone to HEAD_USER_ID so you stay signed in seamlessly
   UPDATE <SCHEMA>.devices
   SET user_id = '<HEAD_USER_ID>'
   WHERE user_id = '<NEW_APPLE_USER_ID>' AND revoked_at IS NULL;

   -- 4. Copy user_profile onto HEAD_USER_ID ONLY if HEAD_USER_ID doesn't already have one
   INSERT INTO <SCHEMA>.user_profile (user_id, height_cm, weight_kg, age, sex, activity, goal, updated_at)
   SELECT '<HEAD_USER_ID>', height_cm, weight_kg, age, sex, activity, goal, updated_at
   FROM <SCHEMA>.user_profile
   WHERE user_id = '<NEW_APPLE_USER_ID>'
   ON CONFLICT (user_id) DO NOTHING;

   -- 5. Readback check before committing (re-run Step 1 query inside the transaction)
   SELECT u.id, u.display_name, a.email, a.last_sign_in_at
   FROM <SCHEMA>.users u
   JOIN <SCHEMA>.accounts a ON a.user_id = u.id
   WHERE u.id = '<HEAD_USER_ID>';

   -- Only run COMMIT after confirming the readback row matches your head account!
   -- Otherwise run ROLLBACK;
   COMMIT;
   ```
   *(Notice: the old shell row in `users` is NOT deleted—preserving Hard Rule 1.)*

## Done criteria

- [ ] Step 1 query confirms `HEAD_USER_ID` is linked to your Apple `accounts` row and has all expected `chat_meals`, `manual_meals`, `saved_meals`, `targets`, and `user_profile`.
- [ ] Opening Macro-Bit on your iPhone immediately shows your full history, saved meals, and targets without re-triggering onboarding.
