-- Additive, date-scoped workout edits. The permanent routine is unchanged.
CREATE TABLE IF NOT EXISTS daily_workout_sessions (
    user_id UUID NOT NULL REFERENCES users(id),
    day DATE NOT NULL,
    workout_type TEXT NOT NULL,
    exercises JSONB NOT NULL CHECK (jsonb_typeof(exercises) = 'array'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, day)
);
