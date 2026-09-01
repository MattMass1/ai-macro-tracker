-- Additive verified-food catalog, immutable evidence, and request idempotency.
CREATE TABLE food_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  normalized_name TEXT NOT NULL,
  display_name TEXT NOT NULL,
  brand TEXT NOT NULL DEFAULT '',
  product_identity TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(normalized_name, brand, product_identity),
  CHECK (normalized_name <> ''), CHECK (display_name <> '')
);

CREATE TABLE food_aliases (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  food_item_id UUID NOT NULL REFERENCES food_items(id),
  normalized_alias TEXT NOT NULL,
  alias_text TEXT NOT NULL,
  alias_kind TEXT NOT NULL DEFAULT 'name',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(food_item_id, normalized_alias),
  CHECK (normalized_alias <> '')
);
CREATE INDEX food_aliases_exact_idx ON food_aliases(normalized_alias);

CREATE TABLE food_identifiers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  food_item_id UUID NOT NULL REFERENCES food_items(id),
  identifier_type TEXT NOT NULL,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(provider, identifier_type, external_id)
);

CREATE TABLE food_source_records (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider TEXT NOT NULL,
  external_id TEXT,
  source_reference TEXT NOT NULL,
  source_url TEXT,
  retrieved_at TIMESTAMPTZ NOT NULL,
  confidence NUMERIC,
  evidence_hash TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}',
  trusted BOOLEAN NOT NULL DEFAULT false,
  cache_allowed BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(provider, evidence_hash),
  CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE food_nutrition_observations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  observation_key TEXT NOT NULL UNIQUE,
  food_item_id UUID NOT NULL REFERENCES food_items(id),
  source_record_id UUID NOT NULL REFERENCES food_source_records(id),
  verification_state TEXT NOT NULL,
  serving_basis TEXT NOT NULL,
  serving_text TEXT,
  serving_grams NUMERIC,
  calories NUMERIC NOT NULL, protein NUMERIC NOT NULL, carbs NUMERIC NOT NULL,
  fat NUMERIC NOT NULL, fiber NUMERIC NOT NULL DEFAULT 0,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (verification_state IN ('official_curated','internal_curated','exact_identifier','community_observed','estimate','unknown')),
  CHECK (serving_basis IN ('per_100g','per_serving','logged_snapshot')),
  CHECK (serving_grams IS NULL OR serving_grams > 0),
  CHECK (calories >= 0 AND protein >= 0 AND carbs >= 0 AND fat >= 0 AND fiber >= 0)
);
CREATE INDEX food_observations_verified_idx
  ON food_nutrition_observations(food_item_id, verification_state, observed_at DESC);
ALTER TABLE food_nutrition_observations ADD CONSTRAINT food_observations_item_id_unique
  UNIQUE(food_item_id,id);

ALTER TABLE nutrition_entries ADD COLUMN food_item_id UUID REFERENCES food_items(id);
ALTER TABLE nutrition_entries ADD COLUMN food_observation_id UUID REFERENCES food_nutrition_observations(id);
ALTER TABLE nutrition_entries ADD COLUMN component_metadata JSONB;
ALTER TABLE meal_presets ADD COLUMN food_item_id UUID REFERENCES food_items(id);
ALTER TABLE meal_presets ADD COLUMN food_observation_id UUID REFERENCES food_nutrition_observations(id);
ALTER TABLE nutrition_entries ADD CONSTRAINT nutrition_entries_catalog_pair_fkey
  FOREIGN KEY(food_item_id,food_observation_id)
  REFERENCES food_nutrition_observations(food_item_id,id) NOT VALID;
ALTER TABLE meal_presets ADD CONSTRAINT meal_presets_catalog_pair_fkey
  FOREIGN KEY(food_item_id,food_observation_id)
  REFERENCES food_nutrition_observations(food_item_id,id) NOT VALID;

ALTER TABLE meals ADD CONSTRAINT meals_user_id_id_unique UNIQUE(user_id,id);
ALTER TABLE fitness_tracker ADD CONSTRAINT fitness_tracker_user_id_id_unique UNIQUE(user_id,id);
ALTER TABLE nutrition_entries DROP CONSTRAINT IF EXISTS nutrition_entries_meal_id_fkey;
ALTER TABLE nutrition_entries ADD CONSTRAINT nutrition_entries_user_meal_fkey
  FOREIGN KEY(user_id,meal_id) REFERENCES meals(user_id,id) NOT VALID;
ALTER TABLE nutrition_entries ADD CONSTRAINT nutrition_entries_macros_nonnegative
  CHECK(calories >= 0 AND protein >= 0 AND carbs >= 0 AND fat >= 0 AND fiber >= 0) NOT VALID;
ALTER TABLE nutrition_entries ADD CONSTRAINT nutrition_entries_meal_domain
  CHECK(meal IN ('Breakfast','Lunch','Dinner','Snack')) NOT VALID;
ALTER TABLE meal_presets ADD CONSTRAINT meal_presets_macros_nonnegative
  CHECK(calories >= 0 AND protein >= 0 AND carbs >= 0 AND fat >= 0 AND fiber >= 0) NOT VALID;
ALTER TABLE days ADD CONSTRAINT days_macros_nonnegative
  CHECK(calories >= 0 AND protein >= 0 AND carbs >= 0 AND fat >= 0 AND fiber >= 0) NOT VALID;
ALTER TABLE meals ADD CONSTRAINT meals_macros_nonnegative
  CHECK(calories >= 0 AND protein >= 0 AND carbs >= 0 AND fat >= 0 AND fiber >= 0) NOT VALID;
ALTER TABLE exercise_max_reps DROP CONSTRAINT IF EXISTS exercise_max_reps_source_entry_fkey;
ALTER TABLE exercise_max_reps ADD CONSTRAINT exercise_max_reps_user_source_fkey
  FOREIGN KEY(user_id,source_entry) REFERENCES fitness_tracker(user_id,id) NOT VALID;

-- Validate only after explicit zero-violation guards. A bad restored database
-- fails before PostgreSQL attempts to validate and reports the exact invariant.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM nutrition_entries e JOIN meals m ON m.id=e.meal_id
             WHERE e.user_id IS DISTINCT FROM m.user_id) THEN
    RAISE EXCEPTION 'nutrition_entries contains cross-tenant meal links';
  END IF;
  IF EXISTS (SELECT 1 FROM nutrition_entries WHERE calories < 0 OR protein < 0 OR carbs < 0 OR fat < 0 OR fiber < 0) THEN
    RAISE EXCEPTION 'nutrition_entries contains negative macros';
  END IF;
  IF EXISTS (SELECT 1 FROM nutrition_entries WHERE meal NOT IN ('Breakfast','Lunch','Dinner','Snack')) THEN
    RAISE EXCEPTION 'nutrition_entries contains invalid meal domains';
  END IF;
  IF EXISTS (SELECT 1 FROM meal_presets WHERE calories < 0 OR protein < 0 OR carbs < 0 OR fat < 0 OR fiber < 0) THEN
    RAISE EXCEPTION 'meal_presets contains negative macros';
  END IF;
  IF EXISTS (SELECT 1 FROM days WHERE calories < 0 OR protein < 0 OR carbs < 0 OR fat < 0 OR fiber < 0) THEN
    RAISE EXCEPTION 'days contains negative macros';
  END IF;
  IF EXISTS (SELECT 1 FROM meals WHERE calories < 0 OR protein < 0 OR carbs < 0 OR fat < 0 OR fiber < 0) THEN
    RAISE EXCEPTION 'meals contains negative macros';
  END IF;
  IF EXISTS (SELECT 1 FROM exercise_max_reps p JOIN fitness_tracker f ON f.id=p.source_entry
             WHERE p.user_id IS DISTINCT FROM f.user_id) THEN
    RAISE EXCEPTION 'exercise_max_reps contains cross-tenant source links';
  END IF;
END $$;
ALTER TABLE nutrition_entries VALIDATE CONSTRAINT nutrition_entries_catalog_pair_fkey;
ALTER TABLE meal_presets VALIDATE CONSTRAINT meal_presets_catalog_pair_fkey;
ALTER TABLE nutrition_entries VALIDATE CONSTRAINT nutrition_entries_user_meal_fkey;
ALTER TABLE nutrition_entries VALIDATE CONSTRAINT nutrition_entries_macros_nonnegative;
ALTER TABLE nutrition_entries VALIDATE CONSTRAINT nutrition_entries_meal_domain;
ALTER TABLE meal_presets VALIDATE CONSTRAINT meal_presets_macros_nonnegative;
ALTER TABLE days VALIDATE CONSTRAINT days_macros_nonnegative;
ALTER TABLE meals VALIDATE CONSTRAINT meals_macros_nonnegative;
ALTER TABLE exercise_max_reps VALIDATE CONSTRAINT exercise_max_reps_user_source_fkey;

CREATE TABLE request_operations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id),
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'in_progress',
  stored_response JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  UNIQUE(user_id,idempotency_key),
  CHECK (idempotency_key <> ''),
  CHECK (status IN ('in_progress','completed','failed')),
  CHECK ((status = 'completed') = (stored_response IS NOT NULL))
);

CREATE OR REPLACE FUNCTION reject_immutable_food_evidence() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '% is immutable', TG_TABLE_NAME; END $$;
CREATE TRIGGER food_source_records_immutable BEFORE UPDATE OR DELETE ON food_source_records
FOR EACH ROW EXECUTE FUNCTION reject_immutable_food_evidence();
CREATE TRIGGER food_nutrition_observations_immutable BEFORE UPDATE OR DELETE ON food_nutrition_observations
FOR EACH ROW EXECUTE FUNCTION reject_immutable_food_evidence();
CREATE OR REPLACE FUNCTION reject_schema_migration_change() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'schema_migrations is immutable'; END $$;
CREATE TRIGGER schema_migrations_immutable BEFORE UPDATE OR DELETE ON schema_migrations
FOR EACH ROW EXECUTE FUNCTION reject_schema_migration_change();
