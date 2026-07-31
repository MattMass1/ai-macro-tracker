# INSTRUCTIONS.md — working guide for coding agents

This file is the handoff document for any coding agent (Hermes, Codex CLI,
Claude Code, or a fresh session) picking up work in this repository. Read it
before touching code. `PROMPT.md` is the original one-shot build brief —
historic context. This file is the live state of the repo.

---

## 1. What this project is

A personal macro tracker with a two-front-door architecture:

```
  Poke (iMessage / SMS)  ──►  /mcp     ┐
                                       ├──►  FastMCP service (Python)  ──►  Notion API
  PWA (iOS home screen)  ──►  /api/*   ┘
```

- **Notion is the database.** All state lives there.
- **Python service (`server/`)** holds every business rule — day boundary,
  totals, validation, Atwater check — exactly once.
- **Next.js PWA (`web/`)** is a pure presentation client. It never calls Notion
  directly and never sees `NOTION_TOKEN`.
- **A nightly cron sweep** (`server/src/sweep_max_weights.py`) maintains the
  workout PR log from the Fitness Tracker. See §6.

**Golden rule: never duplicate business logic in TypeScript.** If a rule exists
in Python, the PWA must render what Python sends, not recompute it.

## 2. Environment & credentials

- Notion integration token lives in `server/.env` (`NOTION_TOKEN`) and the
  Hermes home env (`/opt/data/.env`). **Never commit real secrets.**
- On the hosted Hermes box, the repo lives at `/opt/data/ai-macro-tracker` and
  the Python venv is `server/.venv` (created with `uv`).
- Local run: `cd server && .venv/bin/python src/server.py` (port 8000), and
  `cd web && npm run dev` (port 3000). Both are currently run on-demand by
  Hermes; nothing is a persistent service on the box.

## 3. Handing coding tasks to Codex

Coding tasks (new features, refactors, bug fixes) can be delegated to the
**Codex CLI**. From a Hermes session:

```bash
# One-shot feature task (in this repo)
terminal(command='codex exec --sandbox danger-full-access "Implement X in server/src/..."', workdir="/opt/data/ai-macro-tracker", pty=true, timeout=600)

# Longer task, monitored in background
terminal(command='codex exec --sandbox danger-full-access "Refactor Y and add tests"', workdir="/opt/data/ai-macro-tracker", background=true, pty=true)
```

Rules that hold here:

- **Always `pty=true`.** Codex is an interactive terminal app.
- **Must run inside a git repo** — this repo is one. Do not run Codex in a
  scratch dir for repo work.
- **Use `--sandbox danger-full-access` in gateway/service contexts.** The
  `workspace-write` sandbox fails under Hermes gateway sessions with
  bubblewrap/uid-map errors. Use process boundaries as the safety layer:
  clean git status before launch, narrow prompts, review `git diff` after,
  run the test suite, confirm with the user before committing broad changes.
- **Prompt hygiene:** include the file paths involved, the acceptance criteria,
  and point Codex at §4 and §5 of this file for the invariants.
- Verify Codex output yourself afterward: `git diff`, run pytest, run
  `npm run build` if the web app changed, and check the sweep dry-run if it
  touched workout code. Codex self-reports are not proof.

## 4. Hard invariants — do not break these

1. **Notion API version `2025-09-03`** on every request. Row queries hit
   `POST /v1/data_sources/{data_source_id}/query`. **Forbidden:** the
   pre-2025-09-03 `POST /v1/databases/{id}/query` form. Databases parent data
   sources; resolve `data_source_id` via `GET /v1/databases/{id}` →
   `data_sources[0].id`.
2. **Property names are exact and case-sensitive** — `Calories` (no suffix),
   `Protein (g)`, `Carbs (g)`, `Fat (g)`, `Name`, `Meal`, `Date`. Never write
   to the `C` formula column.
3. **`macro_source` rule:** every meal write requires a real macro source.
   Empty or placeholder values (`estimate`, `guess`, `approx`…) are rejected.
   Never estimate macros — look up FDA FoodData Central or the brand label.
4. **Day boundary:** the logging day rolls at **4:00am America/New_York**.
   Implemented once in `domain.effective_date()`. Never use naive
   `datetime.now()` or UTC for this.
5. **No touching abandoned DBs:** `Daily Meals` and `Daily Goals` are out of
   scope. Never read, write, migrate, or deduplicate against them.
6. **Secrets:** `NOTION_TOKEN` / `APP_SHARED_TOKEN` must never reach the
   browser bundle. Web app calls go through Next.js route handlers that inject
   `X-App-Token` server-side.
7. **The web app never does macro arithmetic** — every number it renders came
   from the Python service.

## 5. Repo layout

```
PROMPT.md                       original build brief (historic)
INSTRUCTIONS.md                 this file
setup.py                        idempotent Notion setup (schema + 2 databases + seed)
server/
  src/domain.py                 day boundary, validation, totals, Atwater check
  src/notion.py                 Notion HTTP client (httpx, API 2025-09-03)
  src/server.py                 FastMCP tools + /api/* routes + /health
  src/config.py                 env loading, fails fast on missing vars
  src/sweep_max_weights.py      nightly PR sweep (Fitness Tracker -> Exercise Max Reps)
  tests/                        pytest — domain, client shapes
web/
  app/page.tsx                  today view (rings, presets, meals)
  app/api/auth/route.ts         passcode -> httpOnly cookie
  app/api/macro/[...path]       server-side proxy injecting X-App-Token
  lib/api.ts, lib/types.ts      typed wrappers + payload types
  middleware.ts                 passcode gate (Next 16: rename to proxy.ts optional)
```

## 6. The nightly max-weight sweep

`server/src/sweep_max_weights.py` — the only piece not part of the original
build. It keeps the **Exercise Max Reps** database in sync with the
**Fitness Tracker** workout log:

- Reads every Fitness Tracker row (318+ entries; schema: `Exercise Name`
  title, `Weight 1-4` / `Reps 1-4` numbers, `Muscle Group` multi-select,
  `Workout type` multi-select).
- Computes the **absolute PR** per exercise: the heaviest set ever logged
  across all rows (max over Weight 1-4).
- Updates Exercise Max Reps: creates missing rows, raises the max when beaten,
  records `Date achieved` + `Source entry` relation, and backfills the
  `Workout type` tag — mapped from `Muscle Group` (`Quads`/`Hams` → `Legs`,
  `Push` → `Push`, `Pull` → `Pull`, `Abs` → `Abs`), because the tracker rows
  only populate Muscle Group.
- **Never lowers a stored max.** Idempotent. Prints nothing when nothing
  changed (cron watchdog pattern: silent nights deliver no message).

Usage:

```bash
cd server
.venv/bin/python src/sweep_max_weights.py --dry-run   # preview
.venv/bin/python src/sweep_max_weights.py             # apply
```

Cron: `sweep_nightly.sh` in the Hermes scripts dir
(`/opt/data/scripts/sweep_nightly.sh`) runs it daily at 08:05 UTC (4:05am ET,
after the day rollover). Job id `38380b55a106` ("Nightly max-weight sweep").

**Known data-quality note:** the Fitness Tracker occasionally contains rows
with Weight/Reps swapped or typo'd weights. The sweep reads what's there.
If suspicious numbers appear in a dry run, inspect the source rows before
applying — a bad PR is sticky (the sweep never lowers a max). Heuristic used
during initial setup: a set with `weight < 30` while `reps > 30` is a swapped
column pair; weights > 400 only make sense for Leg Press (sled plate weight).

## 7. Planned / in-flight work

- **"Next workout" tracker (approved, not yet built):** the PWA should show
  the next Push/Pull/Legs day. Decision: *auto from logs* — derive from the
  last workout date in the Exercise Max Reps DB, using the `Workout type`
  tags the sweep now maintains. Push → Pull → Legs rotation. Not started;
  free to implement in `web/` with a small addition to the Python service if
  needed (remember rule: logic lives in Python).
- **PWA presets:** `Meal Presets` DB is empty; the Quick Add grid shows the
  empty state until presets are saved (via Poke or Notion).

## 8. Testing & verification

```bash
cd server && .venv/bin/python -m pytest      # domain + notion client tests
cd web && npm run build                       # type-check + production build
```

Definition of done for any change: pytest green, build green, no
`/v1/databases/{id}/query` form, `Notion-Version: 2025-09-03` everywhere,
no secrets in client bundles, no new business logic in TypeScript.
