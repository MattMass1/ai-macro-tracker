-- Date-scoped (one calendar logging day) workout plans.
-- Additive only: no existing table or row is modified. A row is a PLAN for
-- one user's day; it never records completed sets (fitness_tracker does).
-- Rollback (reviewed): see docs/rollback/002_daily_workout_plans.md.
CREATE TABLE daily_workout_plans (
  user_id UUID NOT NULL REFERENCES users(id),
  day DATE NOT NULL,
  workout_type TEXT NOT NULL CHECK (char_length(workout_type) BETWEEN 1 AND 80),
  exercises JSONB NOT NULL CHECK (jsonb_typeof(exercises) = 'array'),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  operation_id TEXT NOT NULL CHECK (char_length(operation_id) BETWEEN 1 AND 160),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, day)
);
