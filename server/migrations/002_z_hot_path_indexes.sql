-- Hot-path indexes from the 2026-10-10 efficiency scan. Additive only.
--
-- Numbered 002_z so it sorts BEFORE the gated 003 (owner approval pending):
-- the runner tolerates held migrations only as an unbroken tail, and an
-- ordinary file numbered after 003 blocked the whole store (prod outage,
-- 2026-10-10). Anything that must ship before 003 is approved goes here.
--
-- food_identifiers: every provider-sourced meal log looks up
--   WHERE lower(x.provider)=lower($1) AND x.external_id=$2
-- The existing UNIQUE(provider, identifier_type, external_id) cannot serve a
-- predicate on lower(provider) with identifier_type absent, so it was a scan.
CREATE INDEX IF NOT EXISTS food_identifiers_provider_external_idx
    ON food_identifiers (lower(provider), external_id);

-- nutrition_entries: every day read orders by created_at DESC within
-- (user_id, day); the old index served the filter but left a sort.
CREATE INDEX IF NOT EXISTS nutrition_entries_user_day_created_idx
    ON nutrition_entries (user_id, day, created_at DESC);
