-- Durable conversational meal drafts, structured saved "usuals" and private
-- per-user food defaults. Additive only: no existing row or value changes.
-- Separate owner approval required for live apply (APPROVAL_REQUIRED).
-- Every new tenant-owned row has a NOT NULL owner, and every private
-- parent-child link is a composite (user_id, id) foreign key so a child can
-- never point at another user's parent, whatever the application does.

-- Composite keys the private children reference. Both tables already have
-- `id` as primary key, so these unique indexes cannot fail on existing data.
CREATE UNIQUE INDEX meal_presets_user_id_id_key ON meal_presets(user_id, id);
CREATE UNIQUE INDEX nutrition_entries_user_id_id_key ON nutrition_entries(user_id, id);

-- One editable interpretation of one consumed (or planned) meal.
CREATE TABLE meal_drafts (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  intent TEXT NOT NULL,
  state TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  log_date DATE NOT NULL,
  meal_type TEXT,
  meal_type_origin TEXT,
  usual_label TEXT,
  session_ref TEXT,
  pending_question TEXT,
  parent_draft_id UUID,
  amends_entry_id TEXT,
  amends_entry_revision INTEGER,
  committed_entry_id TEXT,
  committed_entry_revision INTEGER,
  committed_operation_key TEXT,
  committed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, id),
  FOREIGN KEY (user_id, parent_draft_id) REFERENCES meal_drafts(user_id, id),
  CHECK (intent IN ('consume', 'plan')),
  CHECK (state IN ('collecting', 'needs_detail', 'ready', 'committed', 'canceled')),
  CHECK (revision >= 1),
  CHECK (meal_type IS NULL OR meal_type IN ('Breakfast', 'Lunch', 'Dinner', 'Snack')),
  CHECK (usual_label IS NULL OR char_length(usual_label) BETWEEN 1 AND 80),
  CHECK (pending_question IS NULL OR char_length(pending_question) <= 1000),
  CHECK ((amends_entry_id IS NULL) = (amends_entry_revision IS NULL)),
  CHECK (amends_entry_revision IS NULL OR amends_entry_revision >= 1),
  -- A committed draft names exactly the entry revision it produced; nothing
  -- else may carry commit facts.
  CHECK ((state = 'committed') = (committed_entry_id IS NOT NULL)),
  CHECK ((committed_entry_id IS NULL) = (committed_entry_revision IS NULL)),
  CHECK ((committed_entry_id IS NULL) = (committed_at IS NULL)),
  CHECK (state <> 'committed' OR intent = 'consume')
);
-- One frozen draft per (owner, entry, entry revision).
CREATE UNIQUE INDEX meal_drafts_committed_entry_revision_key
  ON meal_drafts(user_id, committed_entry_id, committed_entry_revision)
  WHERE committed_entry_id IS NOT NULL;
CREATE INDEX meal_drafts_user_recent_idx ON meal_drafts(user_id, updated_at DESC);

-- Separately addressable components of one draft.
CREATE TABLE meal_draft_items (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  draft_id UUID NOT NULL,
  position INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'food',
  status TEXT NOT NULL DEFAULT 'included',
  description TEXT NOT NULL,
  quantity NUMERIC,
  unit TEXT,
  preparation TEXT,
  size TEXT,
  field_origins JSONB NOT NULL DEFAULT '{}'::jsonb,
  needs TEXT,
  question TEXT,
  resolution JSONB,
  estimate_consent TEXT,
  template_item_id UUID,
  FOREIGN KEY (user_id, draft_id) REFERENCES meal_drafts(user_id, id) ON DELETE CASCADE,
  CHECK (kind IN ('food', 'usual')),
  CHECK (status IN ('included', 'removed')),
  CHECK (char_length(description) BETWEEN 1 AND 200),
  CHECK (quantity IS NULL OR (quantity > 0 AND quantity <= 5000)),
  CHECK (unit IS NULL OR char_length(unit) BETWEEN 1 AND 20),
  CHECK (preparation IS NULL OR preparation IN ('raw', 'cooked')),
  CHECK (size IS NULL OR size IN ('small', 'medium', 'large')),
  CHECK (needs IS NULL OR needs IN ('identity', 'portion', 'preparation', 'contents', 'estimate_consent')),
  CHECK (jsonb_typeof(field_origins) = 'object'),
  CHECK (resolution IS NULL OR jsonb_typeof(resolution) = 'object')
);
CREATE INDEX meal_draft_items_draft_idx ON meal_draft_items(user_id, draft_id, position);

-- Saved-usual header reuses meal_presets; these are its structured contents.
ALTER TABLE meal_presets ADD COLUMN template_revision INTEGER;
ALTER TABLE meal_presets ADD CONSTRAINT meal_presets_template_revision_check
  CHECK (template_revision IS NULL OR template_revision >= 1);

CREATE TABLE meal_preset_items (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  preset_id TEXT NOT NULL,
  template_revision INTEGER NOT NULL,
  position INTEGER NOT NULL,
  description TEXT NOT NULL,
  quantity NUMERIC,
  unit TEXT,
  preparation TEXT,
  size TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (user_id, preset_id) REFERENCES meal_presets(user_id, id),
  -- Older revisions are kept so editing a usual never rewrites its history.
  UNIQUE (user_id, preset_id, template_revision, position),
  CHECK (template_revision >= 1),
  CHECK (char_length(description) BETWEEN 1 AND 200),
  CHECK (quantity IS NULL OR (quantity > 0 AND quantity <= 5000)),
  CHECK (unit IS NULL OR char_length(unit) BETWEEN 1 AND 20),
  CHECK (preparation IS NULL OR preparation IN ('raw', 'cooked')),
  CHECK (size IS NULL OR size IN ('small', 'medium', 'large'))
);

-- Private names ("my usual chick fil a") and confirmed per-food defaults
-- ("I always weigh 93/7 beef cooked"). Never copied into shared catalogs.
CREATE TABLE user_food_aliases (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  phrase TEXT NOT NULL,
  target_preset_id TEXT,
  target_identity TEXT,
  default_preparation TEXT,
  active BOOLEAN NOT NULL DEFAULT true,
  revision INTEGER NOT NULL DEFAULT 1,
  confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (user_id, target_preset_id) REFERENCES meal_presets(user_id, id),
  CHECK (char_length(phrase) BETWEEN 1 AND 120),
  -- Exactly one target kind.
  CHECK ((target_preset_id IS NULL) <> (target_identity IS NULL)),
  CHECK (target_identity IS NULL OR char_length(target_identity) BETWEEN 1 AND 200),
  CHECK (default_preparation IS NULL OR default_preparation IN ('raw', 'cooked')),
  CHECK (default_preparation IS NULL OR target_identity IS NOT NULL),
  CHECK (revision >= 1)
);
CREATE UNIQUE INDEX user_food_aliases_active_phrase_key
  ON user_food_aliases(user_id, phrase, (target_preset_id IS NULL)) WHERE active;

-- Consumed-record revision for exact-entry amendments.
ALTER TABLE nutrition_entries ADD COLUMN entry_revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE nutrition_entries ADD COLUMN meal_draft_id UUID;
ALTER TABLE nutrition_entries ADD CONSTRAINT nutrition_entries_entry_revision_check
  CHECK (entry_revision >= 1);
