# Moving Off Notion — Evaluation Plan

> Status: **thinking doc** — no execution yet. Matthew's hypothesis: Notion is
> friction for agentic work. This plan scopes what a move would take, the
> options, and a safe migration path. It deliberately does NOT recommend
> ripping out Notion today — it prepares the decision.

**Goal:** Decide whether/how to move the macro-tracker data layer off Notion
into something friendlier for agents, without breaking the PWA, the WHOOP
coach, the nightly sweep, or Matthew's ability to see/edit his data.

**Architecture today:**
```
PWA (Vercel) → FastMCP server (Render :8000) → Notion API (source of truth)
WHOOP cron → workout_rec.py → same server
Nightly sweep → Notion (Fitness Tracker + Max Reps)
Gmail ingest → (planned) → Notion
```

Notion holds 5 data sources: Nutrition Entries, Macro Targets, Meal Presets,
Fitness Tracker, Exercise Max Reps. The server's `notion.py` (552 lines) is
the ONLY data-access layer — every fetch/write funnels through ~15 functions
in server.py. That single seam is what makes this migration tractable.

## Why Notion is hard for agentic work (Matthew's thesis — agreed)

- **No real query language.** Filtering is property-matching JSON; aggregates
  (weekly sums, streaks, volume) must be computed client-side after fetching
  everything. Every stats call re-reads rows.
- **Latency + rate limits.** 429s under bursts; each widget call = several
  round trips. Free-tier integration tokens have no retry budget.
- **Schema friction.** Multi-selects, title-first pages, formula props.
  Property names drift (Calories vs Protein (g)).
- **Not a database.** No joins, no SQL, no indexes, no transactions. The
  sweep has to reconstruct state by reading everything.
- **Poke/Reginald coupling.** He writes to the same DB — a move must keep him
  working or cut him over deliberately.

## Options

### Option A — Postgres (Supabase or self-hosted) — front-runner
- **Pros:** real SQL (agents love it), JSONB for flexible rows, PostgREST
  gives instant REST, Supabase has auth/edge functions, Matthew already uses
  Supabase for BACP. Fast queries, no rate limits.
- **Cons:** needs a schema migration, needs a way for Matthew to see/edit
  data (Supabase Studio is fine but less pretty than Notion), Poke cutover.
- **Effort:** moderate (2-4 Codex sessions).

### Option B — SQLite (local file, single-user)
- **Pros:** zero infra, file-based, trivially agent-friendly (sqlite3),
  perfect for a single user. Instant, no network.
- **Cons:** not multi-device by itself; the server is on Render (ephemeral
  disk) so the DB would need to live somewhere persistent or sync.
- **Effort:** small, but the "where does the file live" question is real.

### Option C — Hybrid (recommended destination): Postgres for agents + Notion as human UI
- **Pros:** best of both. Agents query Postgres at speed; Notion stays as the
  "view/edit anywhere" surface; Poke keeps working; email ingest keeps a
  friendly target.
- **Cons:** two sources of truth = sync job (one-way Notion → Postgres is
  enough for reads; writes go through the server which writes both).
- **Effort:** medium, but no risky cutover. This is the safe landing spot.

## Migration path (whenever we pull the trigger)

1. **Add storage abstraction** — introduce a `Store` interface in the server
   (get/set meals, workouts, targets, presets, PRs). `NotionStore` implements
   it with today's code. Zero behavior change; proves the seam.
2. **Stand up Postgres** (Supabase project or local) with 5 tables mirroring
   the Notion data sources.
3. **Backfill** — one-time script: Notion → Postgres for all rows (~500).
   Verify row counts + spot-check values.
4. **Dual-write** — server writes both (Notion + Postgres) for a soak period;
   reads prefer Postgres. Poke unaffected (writes to Notion; a sync job or
   read-through keeps Postgres fresh).
5. **Cut reads** fully to Postgres; keep Notion writes as the human-visible
   mirror. Sweep + WHOOP coach + widgets now query SQL.
6. **Decide on Notion's future** — keep as read-only mirror (recommended) or
   sunset (only if Matthew stops needing the mobile view / Poke is gone).

## Files likely to change (when executed)

- `server/src/notion.py` → becomes one implementation behind a Store
- New: `server/src/store.py` (interface), `server/src/store_postgres.py`
- `server/src/server.py` — swap fetch_* / write_* calls to the Store
- `server/src/sweep_max_weights.py` — via Store
- `scripts/migrate_notion_to_pg.py` — backfill + verify
- `render.yaml` + env: DATABASE_URL

## Verification

- Existing test suite (116 tests) still passes against NotionStore.
- New PostgresStore tests: same fixtures, same expected outputs.
- `GET /api/today`, `/api/workout-stats`, `/api/plan`, `/api/brief` identical
  from both stores (golden-response comparison).
- Sweep dry-run idempotent on Postgres.
- PWA screenshots before/after — no visual change.

## Risks / tradeoffs

- **Two sources of truth** (hybrid) needs a sync discipline; drift possible
  but low-stakes (food logs aren't bank ledgers).
- **Poke cutover** — if he must read Postgres, that's a separate integration
  we don't control; safer to keep Notion live for him.
- **Matthew's edit flow** — Supabase Studio is functional, not pleasant.
  Hybrid keeps Notion as his editor, so this risk disappears.
- **Effort is real** (4-6 Codex sessions) — do it when agentic speed matters,
  not because Notion is *bad*; it's merely slower.

## Open questions (for Matthew)

1. Do we even need Poke to keep writing after the WHOOP/email-ingest moves to
   Hermes? If Poke is retired, Notion can be sunset entirely and the UI
   question changes.
2. Is Supabase Studio acceptable as the only human UI if we go full Postgres,
   or is the Notion mirror worth keeping forever?
3. Budget: is this a "next month" project or a "someday" doc? (Both fine —
   the plan survives either.)

## Recommendation

**Park this plan; act on it when (a) agents start hitting Notion limits in
practice (rate limits, slow widget calls, sweep complexity) or (b) Poke is
retired and the coupling question disappears. When we move, go Hybrid
(Option C): Postgres for agents, Notion as the human mirror.** The single
`notion.py` seam means the cost of waiting is low and the plan stays valid.
