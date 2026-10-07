-- Calorie-only entries preserve unknown nutrients as SQL NULL, never zero.
-- No existing values change. Application and native clients must be null-aware
-- before writes are enabled. Separate owner approval required for live apply.
ALTER TABLE nutrition_entries
    ALTER COLUMN protein DROP NOT NULL,
    ALTER COLUMN carbs DROP NOT NULL,
    ALTER COLUMN fat DROP NOT NULL,
    ALTER COLUMN fiber DROP NOT NULL;
