CREATE TABLE IF NOT EXISTS days (
  date DATE PRIMARY KEY,
  calories NUMERIC NOT NULL DEFAULT 0, protein NUMERIC NOT NULL DEFAULT 0,
  carbs NUMERIC NOT NULL DEFAULT 0, fat NUMERIC NOT NULL DEFAULT 0,
  fiber NUMERIC NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meals (
  id TEXT PRIMARY KEY, day DATE NOT NULL REFERENCES days(date),
  meal_type TEXT NOT NULL,
  calories NUMERIC NOT NULL DEFAULT 0, protein NUMERIC NOT NULL DEFAULT 0,
  carbs NUMERIC NOT NULL DEFAULT 0, fat NUMERIC NOT NULL DEFAULT 0,
  fiber NUMERIC NOT NULL DEFAULT 0,
  UNIQUE (day, meal_type)
);
CREATE INDEX IF NOT EXISTS meals_day_idx ON meals(day);

CREATE TABLE IF NOT EXISTS nutrition_entries (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, meal TEXT NOT NULL DEFAULT 'Snack',
  calories NUMERIC NOT NULL DEFAULT 0, protein NUMERIC NOT NULL DEFAULT 0,
  carbs NUMERIC NOT NULL DEFAULT 0, fat NUMERIC NOT NULL DEFAULT 0,
  fiber NUMERIC NOT NULL DEFAULT 0, day DATE NOT NULL,
  meal_id TEXT REFERENCES meals(id),
  macro_source TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Existing deployments already have nutrition_entries; make this file a safe
-- schema upgrade as well as a complete definition for fresh databases.
ALTER TABLE nutrition_entries ADD COLUMN IF NOT EXISTS meal_id TEXT REFERENCES meals(id);
CREATE INDEX IF NOT EXISTS nutrition_entries_day_idx ON nutrition_entries(day);
CREATE INDEX IF NOT EXISTS nutrition_entries_meal_id_idx ON nutrition_entries(meal_id);

CREATE TABLE IF NOT EXISTS fitness_tracker (
  id TEXT PRIMARY KEY, exercise_name TEXT NOT NULL,
  workout_type TEXT[] NOT NULL DEFAULT '{}', muscle_group TEXT[] NOT NULL DEFAULT '{}',
  weight_1 NUMERIC, reps_1 NUMERIC, weight_2 NUMERIC, reps_2 NUMERIC,
  weight_3 NUMERIC, reps_3 NUMERIC, weight_4 NUMERIC, reps_4 NUMERIC,
  day DATE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fitness_tracker_day_idx ON fitness_tracker(day);

CREATE TABLE IF NOT EXISTS max_weight (
  id TEXT PRIMARY KEY, exercise_name TEXT NOT NULL,
  muscle_group TEXT[] NOT NULL DEFAULT '{}', all_time_max NUMERIC,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS exercise_max_reps (
  id TEXT PRIMARY KEY, exercise TEXT NOT NULL, max_weight NUMERIC NOT NULL DEFAULT 0,
  date_achieved DATE, workout_type TEXT[] NOT NULL DEFAULT '{}',
  source_entry TEXT REFERENCES fitness_tracker(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meal_presets (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '🍽️',
  calories NUMERIC NOT NULL DEFAULT 0, protein NUMERIC NOT NULL DEFAULT 0,
  carbs NUMERIC NOT NULL DEFAULT 0, fat NUMERIC NOT NULL DEFAULT 0,
  fiber NUMERIC NOT NULL DEFAULT 0, meal TEXT NOT NULL DEFAULT 'Dinner',
  sort_order NUMERIC NOT NULL DEFAULT 0, active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS macro_targets (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, calories NUMERIC NOT NULL DEFAULT 0,
  protein NUMERIC NOT NULL DEFAULT 0, carbs NUMERIC NOT NULL DEFAULT 0,
  fat NUMERIC NOT NULL DEFAULT 0, fiber NUMERIC NOT NULL DEFAULT 0,
  effective_date DATE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Runtime-only storage required to preserve the existing daily brief API.
CREATE TABLE IF NOT EXISTS briefs (
  day DATE PRIMARY KEY, text TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
