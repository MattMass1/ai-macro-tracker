# Macro Tracker: Notion → PostgreSQL Migration Plan

## Goal
Replace Notion as the data store with Render PostgreSQL. Keep the HTTP API and
PWA unchanged. Preserve all 1,444 existing records.

## Data inventory (exported to /opt/data/migration-export/*.json)

| Table | Rows | Key columns (Notion property → type) |
|---|---|---|
| nutrition_entries | 1027 | Name(title), Meal(select), Calories, Protein (g), Carbs (g), Fat (g), Fiber, Date(date), C(formula, never written) |
| fitness_tracker | 361 | Exercise Name(title), Workout type(multi), Muscle Group(multi), Weight 1..4, Reps 1..4, Date (user input)(date), Date(formula), Created time |
| max_weight | 25 | Exercise Name(title), All-Time Max(rollup, derived), Muscle Group(multi), Primary Fitness Tracker(relation) |
| exercise_max_reps | 22 | Exercise(title), Max weight(number), Date achieved(date), Workout type(multi), Source entry(relation) |
| meal_presets | 7 | Name(title), Emoji(rich_text), Calories, Protein, Carbs, Fat, Fiber, Meal(select), Sort Order(number), Active(checkbox) |
| macro_targets | 2 | Name(title), Calories, Protein, Carbs, Fat, Fiber, Effective Date(date) |

## 1. PostgreSQL schema (snake_case)

```sql
CREATE TABLE nutrition_entries (
  id            TEXT PRIMARY KEY,          -- Notion page id
  name          TEXT NOT NULL,
  meal          TEXT NOT NULL DEFAULT 'Snack',
  calories      NUMERIC NOT NULL DEFAULT 0,
  protein       NUMERIC NOT NULL DEFAULT 0,
  carbs         NUMERIC NOT NULL DEFAULT 0,
  fat           NUMERIC NOT NULL DEFAULT 0,
  fiber         NUMERIC NOT NULL DEFAULT 0,
  day           DATE NOT NULL,
  macro_source  TEXT NOT NULL DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE fitness_tracker (
  id            TEXT PRIMARY KEY,
  exercise_name TEXT NOT NULL,
  workout_type  TEXT[] NOT NULL DEFAULT '{}',   -- multi_select
  muscle_group  TEXT[] NOT NULL DEFAULT '{}',
  weight_1 NUMERIC, reps_1 NUMERIC,
  weight_2 NUMERIC, reps_2 NUMERIC,
  weight_3 NUMERIC, reps_3 NUMERIC,
  weight_4 NUMERIC, reps_4 NUMERIC,
  day           DATE,                          -- "Date (user input)"
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE max_weight (
  id            TEXT PRIMARY KEY,
  exercise_name TEXT NOT NULL,
  muscle_group  TEXT[] NOT NULL DEFAULT '{}',
  all_time_max  NUMERIC,                       -- rollup: compute at load
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE exercise_max_reps (
  id            TEXT PRIMARY KEY,
  exercise      TEXT NOT NULL,
  max_weight    NUMERIC NOT NULL DEFAULT 0,
  date_achieved DATE,
  workout_type  TEXT[] NOT NULL DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE meal_presets (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  emoji         TEXT NOT NULL DEFAULT '🍽️',
  calories      NUMERIC NOT NULL DEFAULT 0,
  protein       NUMERIC NOT NULL DEFAULT 0,
  carbs         NUMERIC NOT NULL DEFAULT 0,
  fat           NUMERIC NOT NULL DEFAULT 0,
  fiber         NUMERIC NOT NULL DEFAULT 0,
  meal          TEXT NOT NULL DEFAULT 'Dinner',
  sort_order    NUMERIC NOT NULL DEFAULT 0,
  active        BOOLEAN NOT NULL DEFAULT true,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE macro_targets (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  calories       NUMERIC NOT NULL DEFAULT 0,
  protein        NUMERIC NOT NULL DEFAULT 0,
  carbs          NUMERIC NOT NULL DEFAULT 0,
  fat            NUMERIC NOT NULL DEFAULT 0,
  fiber          NUMERIC NOT NULL DEFAULT 0,
  effective_date DATE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 2. Store layer (server/src/store.py)

Mirror `notion.py`'s client interface so `server.py` changes are minimal.
Use `asyncpg` (matches the existing async FastMCP code).

| notion.py method | store.py equivalent |
|---|---|
| query_data_source(ds_id) | SELECT * FROM <table> — but ds_id → table name via a small map |
| create_page(ds_id, props) | INSERT |
| update_page(page_id, props) | UPDATE ... WHERE id |
| archive_page(page_id) | DELETE (or soft-delete via active flag) |
| get_page(id) | SELECT ... WHERE id |
| block_children / append_children | (brief feature) — new `briefs` table with TEXT content |
| read_title / read_number / read_select etc | (drop — columns are typed already) |

**Simplest path:** keep the notion.py *property-name* reading helpers, but back
them with SQL. Actually cleanest: write a `store.py` that exposes domain-level
functions (fetch_meals, insert_meal, fetch_presets, save_preset, etc.) matching
the existing `fetch_*` / `write_*` helpers in server.py, and swap those call
sites. The `notion_api.X` property readers get replaced by direct column access.

## 3. Data load script (scripts/migrate_load.py)

- Read each JSON file, map Notion property shape → SQL columns.
- Titles: `properties["Name"]["title"][0]["plain_text"]`
- Numbers: `properties["Calories"]["number"]`
- Select: `properties["Meal"]["select"]["name"]`
- Multi-select: `[x["name"] for x in properties["Workout type"]["multi_select"]]`
- Date: `properties["Date"]["date"]["start"]` (or "Date (user input)")
- Rich text: `properties["Emoji"]["rich_text"][0]["plain_text"]`
- Checkbox: `properties["Active"]["checkbox"]`
- Formula (C, Date, All-Time Max): skip — derived/never written
- fitness_tracker: 4 pairs of Weight N / Reps N → weight_N, reps_N
- max_weight: compute `all_time_max` = max of related fitness weights at load
- Skip archived rows.

## 4. Config changes (server/src/config.py + render.yaml)

- ADD: `DATABASE_URL` (required)
- REMOVE: `NOTION_TOKEN`, `NUTRITION_DS_ID`, `TARGETS_DS_ID`, `PRESETS_DS_ID`,
  `FITNESS_DS_ID`, `PARENT_PAGE_ID`, and any other *_DS_ID
- Keep: `APP_SHARED_TOKEN`, `OPENAI_ACCESS_TOKEN`, `LOCAL_TZ`, `DAY_ROLLOVER_HOUR`,
  `ALLOWED_ORIGINS`

## 5. server.py call-site changes

- `notion_client()` → `store_client()` (asyncpg pool)
- `notion_api.read_*` → direct column access on returned rows
- `fetch_meals` / `fetch_presets` / `fetch_targets` / `fetch_workouts` etc: swap
  query_data_source → SQL SELECT
- `write_meal` / `save_preset` / `set_targets` / `log_workout`: swap create_page →
  INSERT
- Brief storage (write_brief_to_notion / read_brief_from_notion): new `briefs`
  table (day DATE PRIMARY KEY, text TEXT), replace block-based logic entirely.
- daily_summary.py + sweep_max_weights.py: swap their Notion queries too.

## 6. render.yaml

- Postgres is provisioned separately on Render; set `DATABASE_URL` as an env var
  (sync: false, generateValue: false) referencing the internal connection string.

## 7. Rollout order + rollback

1. Provision Render Postgres (blocked on user: connection string or API key).
2. Run migrate_load.py against Postgres; verify row counts == 1444.
3. Deploy code with DATABASE_URL set.
4. Smoke test /api/today, /api/plan, /api/presets, log a meal.
5. Keep Notion read-only (do NOT delete) for 1 week as rollback.
6. After verified: archive Notion DBs.

## Notes / gotchas

- Fiber column already added to Notion DBs (Aug 14) — preserve it in Postgres.
- fitness_tracker `workout_type` / `muscle_group` are arrays; keep TEXT[].
- "Date (user input)" is the sort key for workout history, not "Date" (formula).
- Existing code has 4-set cap; schema mirrors weight_1..4 / reps_1..4.
