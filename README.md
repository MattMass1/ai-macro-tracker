# Macro tracker — Notion + Poke MCP + iOS PWA

A personal macro tracker with three parts:

- **Notion is the database.** The existing `Nutrition Entries` database is the meal log, used as-is.
- **Poke logs food by text**, through a custom MCP server.
- **An iPhone home-screen PWA** shows today's numbers and one-taps repeat meals.

## Architecture: one service, two front doors

```
  Poke (iMessage / SMS)  ──►  /mcp     ┐
                                       ├──►  FastMCP service (Render)  ──►  Notion API
  PWA (Vercel, iOS)      ──►  /api/*   ┘
```

One Python process serves both. FastMCP's `@mcp.custom_route` mounts the REST API
alongside the MCP endpoint, so the day-boundary rule, macro totals, remaining
math, and validation live **once**, in [`server/src/domain.py`](server/src/domain.py).

The Next.js app is a presentation client. It holds no Notion credentials and does
no macro arithmetic — every number it renders came from Python.

### Layout

```
setup.py                    idempotent Notion setup (schema + 2 databases + seed)
server/src/domain.py        day boundary, validation, totals, Atwater check
server/src/notion.py        Notion HTTP client (httpx, API version 2025-09-03)
server/src/server.py        FastMCP tools + /api/* routes + /health
server/src/config.py        env loading, fails fast on anything missing
server/tests/               pytest — day boundary, totals, client request shapes
web/app/page.tsx            the entire app: today's view
web/app/lock/page.tsx       passcode screen
web/app/api/auth/route.ts   passcode -> httpOnly cookie
web/app/api/macro/[...path] server-side proxy that injects X-App-Token
web/lib/api.ts              typed wrappers against the Python service (server only)
web/middleware.ts           passcode gate
```

## The rules baked in

| | |
|---|---|
| Day boundary | Rolls at **4:00am America/New_York**. A 2am snack counts toward the previous day. Implemented once, in `effective_date()`. |
| Macro sourcing | Never estimated. Every write requires a `macro_source`; empty and placeholder values (`estimate`, `approx`, `guess`, …) are rejected. |
| Atwater check | If `\|4p + 4c + 9f − calories\|` exceeds `max(50, 20%)`, the entry still writes but the response carries a `warning`. Alcohol and fiber legitimately break the identity, so this never blocks. |
| Targets | Dated rows in `Macro Targets`; the latest effective date ≤ the day wins, so history survives a cut/bulk switch. |
| Overage | `remaining` goes negative and the UI turns the indicator red. Never clamped. |

`macro_source` is written to the page **body** as a paragraph, not to a property —
the `Nutrition Entries` schema has no field for it and none was added. The `C`
formula column is never written to.

### Notion API version

Pinned to `2025-09-03` on every request. Under that version, databases parent one
or more *data sources* and row operations moved:

| Operation | Call |
|---|---|
| Query rows | `POST /v1/data_sources/{data_source_id}/query` |
| Create a row | `POST /v1/pages`, parent `{"type":"data_source_id", …}` |
| Update / archive a row | `PATCH /v1/pages/{page_id}` |
| Create a database | `POST /v1/databases`, properties under `initial_data_source` |
| Resolve data source id | `GET /v1/databases/{database_id}` → `data_sources[0].id` |
| Update schema | `PATCH /v1/data_sources/{data_source_id}` |

Verified against [the 2025-09-03 upgrade guide](https://developers.notion.com/docs/upgrade-guide-2025-09-03)
and the query-a-data-source reference during the build — **no deviations found**.
A test in `server/tests/test_notion.py` greps the source to make sure the
pre-2025-09-03 database-level query form never comes back.

## MCP tools

| Tool | What it does |
|---|---|
| `log_meal` | Log one food. Requires `macro_source`; returns updated totals, plus a `warning` on an Atwater mismatch. |
| `log_preset` | Log a saved preset by name × servings. |
| `list_presets` | Active presets with per-serving macros. |
| `save_preset` | Create or update a preset ("save that as my usual dinner"). |
| `get_today` | Totals, targets, remaining, meals for the current logging day. |
| `get_day` | Same, for an explicit `YYYY-MM-DD`. |
| `get_range_summary` | Per-day totals and averages across an inclusive range. |
| `undo_last_meal` | Archive the newest entry for the current day; returns corrected totals. |
| `get_targets` | Targets in effect for a day. |
| `set_targets` | New dated targets row. |

## REST API

All `/api/*` routes require `X-App-Token: {APP_SHARED_TOKEN}` and return `401`
JSON without it. `/mcp` (Poke's own bearer credential) and `/health` are exempt.
CORS allows `GET, POST, DELETE, OPTIONS` with `Content-Type` and `X-App-Token`.

| Method | Path |
|---|---|
| `GET` | `/api/today` |
| `GET` | `/api/day/{date}` |
| `GET` | `/api/presets` |
| `POST` | `/api/log-preset` |
| `POST` | `/api/log` |
| `DELETE` | `/api/meal/{page_id}` |
| `GET` | `/health` (no auth) |

## Local development

### Server

```bash
cd server && pip install -r requirements.txt && cp .env.example .env
```

Fill in `.env`, then:

```bash
python src/server.py
```

`/health` should return 200 and `/mcp` should answer an MCP initialize. Tests:

```bash
cd server && python -m pytest
```

### Web

```bash
cd web && npm install && cp .env.example .env.local
```

Point `MACRO_API_URL` at `http://localhost:8000`, match `APP_SHARED_TOKEN` to the
server, pick an `APP_PASSCODE`, then:

```bash
cd web && npm run dev
```

Regenerate the icons (needs Pillow) any time the mark changes:

```bash
cd web && npm run icons
```

Next 16 warns that `middleware.ts` is now called `proxy.ts`. The file works as-is;
renaming it is a one-line change whenever you want the warning gone.

## Deployment

**Render (server).** `server/render.yaml` defines the web service: Python,
`pip install -r requirements.txt`, start `python src/server.py`, health check
`/health`. Set `NOTION_TOKEN`, `TARGETS_DS_ID`, `PRESETS_DS_ID` and
`ALLOWED_ORIGINS` (your Vercel origin) in the Render dashboard; `APP_SHARED_TOKEN`
is generated on first deploy — copy it into Vercel.

**Vercel (web).** Root directory `web`. Environment variables: `MACRO_API_URL`
(the Render URL), `APP_SHARED_TOKEN` (matching Render), `APP_PASSCODE`.

Render's free tier sleeps after inactivity, so the first Poke message or app open
after an idle stretch takes about 30 seconds. That is latency only — no data is
affected. The app shows a "waking up" hint when a request times out.

## Manual steps — only you can do these

1. Create an internal integration at [notion.so/my-integrations](https://www.notion.so/my-integrations) and copy the secret.
2. Open the **Daily Food Log** page → `⋯` → **Connections** → add the integration.
   *Skip this and every Notion call returns 404. It is the most common setup failure.*
3. Run setup and paste the printed IDs into `server/.env` (and Render):

   ```bash
   NOTION_TOKEN=secret_... python setup.py
   ```

   It is idempotent — running it twice creates nothing new. It adds `Snack` to the
   existing `Meal` select (preserving `Breakfast`, `Lunch`, `Dinner`), creates
   `Macro Targets` and `Meal Presets` under `Daily Food Log`, and seeds one targets
   row dated 2026-01-01 at 2400 kcal / 215p / 200c / 70f for you to edit in Notion.
4. Deploy `server/` to Render and `web/` to Vercel.
5. Add the MCP integration in Poke at
   [poke.com/settings/connections/integrations/new](https://poke.com/settings/connections/integrations/new)
   using `https://<service>.onrender.com/mcp`.
6. Open the Vercel URL on your iPhone → Share → **Add to Home Screen**.

`Meal Presets` starts empty. Populate it in Notion, or tell Poke "save that as my
usual dinner" — the tiles in the app come straight from it.

## Out of scope

A `Daily Meals` database with a similar schema exists in the workspace and is
abandoned. Nothing here reads it, writes it, migrates it, or deduplicates against
it. A `Daily Goals` todo list is likewise unrelated and untouched.
