# Migration 004: durable conversational meal drafts

Status: built and automated-tested on a throwaway local PostgreSQL. NOT applied to production. NOT enabled.

## What changes for the user (once enabled)

- The coach keeps the meal you're describing in the database, one item per food. A short answer ("raw") fixes only the item the question was about. Nothing is re-guessed, and food that's already resolved is never looked up again.
- It asks only for what's missing, and asks independent questions together. There is no forced "one question, then assume a standard serving". A food named with no amount ("a banana") still uses a standard size, and the receipt says so.
- A complete meal logs once, with no "are you sure?". Corrections to a logged meal update that one entry ("make that two"). "Another one" or "same as yesterday" creates a new entry.
- "My usual X": if a saved usual exists, its items are expanded. If not, the coach asks once what it contains, logs it, then offers to remember it. Saving happens only on your yes, and saving never logs anything.
- "I always weigh it cooked" is remembered privately per food. "Cooked this time" applies only to that meal.
- Planned food ("I'll have it later") is never logged.

## Release safeguards (fail closed)

1. **Migration hold.** `004_meal_drafts.sql` is in `APPROVAL_REQUIRED`. It also sits behind the still-held 003, so the runner applies neither until both are approved, or until 003 is resolved and 004 is approved by checksum: `APPROVED_MIGRATIONS=...,004_meal_drafts.sql:<sha256>`.
2. **Operator switch.** `MEAL_DRAFTS_ENABLED=true` (exact word). Optionally `MEAL_DRAFTS_USER_IDS=<uuid,...>` as a canary list; a malformed list allows nobody.
3. **Live schema check.** Even with the switch on, the canvas uses the old `log_meal` path unless the 004 tables exist.

All three must agree before anything changes; otherwise chat behaves exactly as today. No iOS change is required: the reply, `MealReceipt` and `FoodClarification` surfaces are unchanged.

## Data safety

- Additive only: 4 new tables, 2 new unique indexes on existing primary-key pairs, and 3 nullable or defaulted columns (`nutrition_entries.entry_revision` default 1, `nutrition_entries.meal_draft_id`, `meal_presets.template_revision`). No existing value changes.
- Every new row has a NOT NULL owner. Draft items, preset items and aliases reference their parent by `(user_id, id)`, so the database itself rejects a cross-user link (tested).
- Draft commits do not create shared-catalog aliases, so private meal names never become global food names.
- Provider panels not licensed for caching (FatSecret with `FATSECRET_CACHE_ALLOWED=false`) are not stored in drafts. They are re-fetched at commit, and a changed panel triggers a re-check instead of a silent save.
- Commits go through the existing `run_idempotent`, keyed by draft revision. A retry after a lost response returns the original receipt. An amend checks the entry's revision under a row lock.

## Rollback

1. **Forward-disable (always safe):** unset `MEAL_DRAFTS_ENABLED`. Chat returns to the legacy path. Logged meals are ordinary `nutrition_entries` rows and stay visible everywhere.
2. Leave the tables in place. Dropping them is unnecessary and would destroy draft and usual history; do it only with separate owner approval.

## Rollout order (each step needs owner approval)

1. Push and deploy with the switch unset (004 held). Verify `/api/readiness` lists 004 under `held`.
2. Approve and apply 004 (decide 003 first, since 004 is held behind it).
3. Set `MEAL_DRAFTS_ENABLED=true` and `MEAL_DRAFTS_USER_IDS=<synthetic test user>`. Run the beef/apple, usual, amend and repeat checks on that account.
4. Add Matthew's ID to the canary list, then widen.

## Known limits of this first slice

- The meal slot defaults from time of day (4:00-10:29 Breakfast, until 2:59pm Lunch, until 8:59pm Dinner, otherwise Snack) unless said. A correction keeps the slot the meal was logged in unless you name a new one.
- Size words map to curated per-food weights only (small/medium/large exist for some generics, such as apple and banana). Restaurant items rely on the existing lookup.
- No UI for open drafts yet. Drafts are reachable through chat context, covering 12 hours of open drafts and today's and yesterday's logged meals.
- `/api/chat` (legacy web) and MCP `log_meal` are unchanged.
