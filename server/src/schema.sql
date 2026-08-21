CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_admin BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS invite_codes (
  code TEXT PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS devices (
  token_hash TEXT PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  label TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS devices_user_id_idx ON devices(user_id);

CREATE TABLE IF NOT EXISTS integrations (
  user_id UUID NOT NULL REFERENCES users(id),
  provider TEXT NOT NULL,
  access_token TEXT,
  refresh_token TEXT,
  scopes TEXT,
  expires_at TIMESTAMPTZ,
  UNIQUE(user_id, provider)
);

CREATE TABLE IF NOT EXISTS workout_plans (
  user_id UUID PRIMARY KEY REFERENCES users(id),
  plan JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_metrics (
  user_id UUID PRIMARY KEY REFERENCES users(id),
  height_cm NUMERIC NOT NULL,
  weight_kg NUMERIC NOT NULL,
  goal_weight_kg NUMERIC NOT NULL,
  age INTEGER,
  activity_level TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workout_library (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  muscle_group TEXT[] NOT NULL DEFAULT '{}',
  workout_type TEXT NOT NULL,
  equipment TEXT NOT NULL,
  difficulty TEXT NOT NULL,
  swaps TEXT[] NOT NULL DEFAULT '{}',
  video_url TEXT,
  instructions TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE workout_library ADD COLUMN IF NOT EXISTS video_url TEXT;
ALTER TABLE workout_library ADD COLUMN IF NOT EXISTS instructions TEXT;
CREATE INDEX IF NOT EXISTS workout_library_type_name_idx
  ON workout_library(workout_type, name);

CREATE TABLE IF NOT EXISTS chat_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id),
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  tool_calls JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_user_created_idx
  ON chat_messages(user_id, created_at);

CREATE TABLE IF NOT EXISTS coach_usage (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id),
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS coach_usage_user_created_idx
  ON coach_usage(user_id, created_at);

CREATE TABLE IF NOT EXISTS days (
  user_id UUID REFERENCES users(id),
  date DATE NOT NULL,
  calories NUMERIC NOT NULL DEFAULT 0, protein NUMERIC NOT NULL DEFAULT 0,
  carbs NUMERIC NOT NULL DEFAULT 0, fat NUMERIC NOT NULL DEFAULT 0,
  fiber NUMERIC NOT NULL DEFAULT 0
);
ALTER TABLE days ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);

CREATE TABLE IF NOT EXISTS meals (
  id TEXT PRIMARY KEY, user_id UUID REFERENCES users(id), day DATE NOT NULL,
  meal_type TEXT NOT NULL,
  calories NUMERIC NOT NULL DEFAULT 0, protein NUMERIC NOT NULL DEFAULT 0,
  carbs NUMERIC NOT NULL DEFAULT 0, fat NUMERIC NOT NULL DEFAULT 0,
  fiber NUMERIC NOT NULL DEFAULT 0
);
ALTER TABLE meals ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);

CREATE TABLE IF NOT EXISTS nutrition_entries (
  id TEXT PRIMARY KEY, user_id UUID REFERENCES users(id), name TEXT NOT NULL,
  meal TEXT NOT NULL DEFAULT 'Snack', calories NUMERIC NOT NULL DEFAULT 0,
  protein NUMERIC NOT NULL DEFAULT 0, carbs NUMERIC NOT NULL DEFAULT 0,
  fat NUMERIC NOT NULL DEFAULT 0, fiber NUMERIC NOT NULL DEFAULT 0,
  day DATE NOT NULL, meal_id TEXT REFERENCES meals(id),
  macro_source TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE nutrition_entries ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);
ALTER TABLE nutrition_entries ADD COLUMN IF NOT EXISTS meal_id TEXT REFERENCES meals(id);

CREATE TABLE IF NOT EXISTS fitness_tracker (
  id TEXT PRIMARY KEY, user_id UUID REFERENCES users(id), exercise_name TEXT NOT NULL,
  workout_type TEXT[] NOT NULL DEFAULT '{}', muscle_group TEXT[] NOT NULL DEFAULT '{}',
  weight_1 NUMERIC, reps_1 NUMERIC, weight_2 NUMERIC, reps_2 NUMERIC,
  weight_3 NUMERIC, reps_3 NUMERIC, weight_4 NUMERIC, reps_4 NUMERIC,
  day DATE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE fitness_tracker ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);

CREATE TABLE IF NOT EXISTS max_weight (
  id TEXT PRIMARY KEY, user_id UUID REFERENCES users(id), exercise_name TEXT NOT NULL,
  muscle_group TEXT[] NOT NULL DEFAULT '{}', all_time_max NUMERIC,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE max_weight ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);

CREATE TABLE IF NOT EXISTS exercise_max_reps (
  id TEXT PRIMARY KEY, user_id UUID REFERENCES users(id), exercise TEXT NOT NULL,
  max_weight NUMERIC NOT NULL DEFAULT 0, date_achieved DATE,
  workout_type TEXT[] NOT NULL DEFAULT '{}',
  source_entry TEXT REFERENCES fitness_tracker(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE exercise_max_reps ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);

CREATE TABLE IF NOT EXISTS meal_presets (
  id TEXT PRIMARY KEY, user_id UUID REFERENCES users(id), name TEXT NOT NULL,
  emoji TEXT NOT NULL DEFAULT '🍽️', calories NUMERIC NOT NULL DEFAULT 0,
  protein NUMERIC NOT NULL DEFAULT 0, carbs NUMERIC NOT NULL DEFAULT 0,
  fat NUMERIC NOT NULL DEFAULT 0, fiber NUMERIC NOT NULL DEFAULT 0,
  meal TEXT NOT NULL DEFAULT 'Dinner', sort_order NUMERIC NOT NULL DEFAULT 0,
  active BOOLEAN NOT NULL DEFAULT true, macro_source TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE meal_presets ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);
-- The '' default exists only to backfill legacy rows when the column is first
-- added; drop it immediately so every new preset must state its provenance.
ALTER TABLE meal_presets ADD COLUMN IF NOT EXISTS macro_source TEXT NOT NULL DEFAULT '';
ALTER TABLE meal_presets ALTER COLUMN macro_source DROP DEFAULT;

CREATE TABLE IF NOT EXISTS macro_targets (
  id TEXT PRIMARY KEY, user_id UUID REFERENCES users(id), name TEXT NOT NULL,
  calories NUMERIC NOT NULL DEFAULT 0, protein NUMERIC NOT NULL DEFAULT 0,
  carbs NUMERIC NOT NULL DEFAULT 0, fat NUMERIC NOT NULL DEFAULT 0,
  fiber NUMERIC NOT NULL DEFAULT 0, effective_date DATE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE macro_targets ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);

CREATE TABLE IF NOT EXISTS briefs (
  user_id UUID REFERENCES users(id), day DATE NOT NULL, text TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE briefs ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);

-- Complete tenant constraints only after a migration has stamped legacy rows.
DO $$
DECLARE
  table_name TEXT;
  has_null BOOLEAN;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['days','meals','nutrition_entries','fitness_tracker',
    'max_weight','exercise_max_reps','meal_presets','macro_targets','briefs']
  LOOP
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I WHERE user_id IS NULL)', table_name)
      INTO has_null;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute
      WHERE attrelid=table_name::regclass AND attname='user_id' AND attnotnull)
      AND NOT has_null THEN
      EXECUTE format('ALTER TABLE %I ALTER COLUMN user_id SET NOT NULL', table_name);
    END IF;
  END LOOP;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM days WHERE user_id IS NULL)
    AND NOT EXISTS (SELECT 1 FROM meals WHERE user_id IS NULL)
    AND NOT EXISTS (SELECT 1 FROM briefs WHERE user_id IS NULL) THEN
    ALTER TABLE meals DROP CONSTRAINT IF EXISTS meals_day_fkey;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='days'::regclass
      AND conname='days_pkey' AND pg_get_constraintdef(oid) = 'PRIMARY KEY (date)') THEN
      ALTER TABLE days DROP CONSTRAINT days_pkey;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='days'::regclass
      AND contype='p' AND pg_get_constraintdef(oid) LIKE '%(user_id, date)%') THEN
      ALTER TABLE days ADD PRIMARY KEY (user_id, date);
    END IF;
    ALTER TABLE meals DROP CONSTRAINT IF EXISTS meals_day_meal_type_key;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='meals'::regclass
      AND contype='u' AND pg_get_constraintdef(oid) LIKE '%(user_id, day, meal_type)%') THEN
      ALTER TABLE meals ADD UNIQUE (user_id, day, meal_type);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='meals'::regclass
      AND contype='f' AND pg_get_constraintdef(oid) LIKE '%(user_id, day)%') THEN
      ALTER TABLE meals ADD FOREIGN KEY (user_id, day) REFERENCES days(user_id, date);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='briefs'::regclass
      AND conname='briefs_pkey' AND pg_get_constraintdef(oid) = 'PRIMARY KEY (day)') THEN
      ALTER TABLE briefs DROP CONSTRAINT briefs_pkey;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='briefs'::regclass
      AND contype='p' AND pg_get_constraintdef(oid) LIKE '%(user_id, day)%') THEN
      ALTER TABLE briefs ADD PRIMARY KEY (user_id, day);
    END IF;
  END IF;
END $$;

DROP INDEX IF EXISTS meals_day_idx;
DROP INDEX IF EXISTS nutrition_entries_day_idx;
DROP INDEX IF EXISTS nutrition_entries_meal_id_idx;
DROP INDEX IF EXISTS fitness_tracker_day_idx;
CREATE INDEX IF NOT EXISTS meals_user_day_idx ON meals(user_id, day);
CREATE INDEX IF NOT EXISTS nutrition_entries_user_day_idx ON nutrition_entries(user_id, day);
CREATE INDEX IF NOT EXISTS nutrition_entries_user_meal_idx ON nutrition_entries(user_id, meal_id);
CREATE INDEX IF NOT EXISTS fitness_tracker_user_day_idx ON fitness_tracker(user_id, day);
CREATE INDEX IF NOT EXISTS max_weight_user_exercise_idx ON max_weight(user_id, exercise_name);
CREATE INDEX IF NOT EXISTS exercise_max_reps_user_date_idx ON exercise_max_reps(user_id, date_achieved);
CREATE INDEX IF NOT EXISTS meal_presets_user_active_idx ON meal_presets(user_id, active, sort_order);
CREATE INDEX IF NOT EXISTS macro_targets_user_effective_idx ON macro_targets(user_id, effective_date);
