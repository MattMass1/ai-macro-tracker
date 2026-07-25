# Build: Notion-backed macro tracker with a Poke MCP server and an iOS home-screen PWA

You are building a complete, deployable macro tracking system in one pass. Read this entire document before writing any code. Everything you need is here — do not ask clarifying questions, do not stub anything out, and do not leave TODOs. Ship working code.

---

## 1. Objective

A personal macro tracker with three properties:

1. **Notion is the database.** An existing Notion database already holds the meal log and must be used as-is.
2. **Poke (the AI assistant from Interaction Co.) logs food by text.** It connects via a custom MCP server you will build.
3. **An iPhone home-screen PWA shows today's numbers** and offers one-tap logging for repeat meals.

### Architecture: one service, two front doors

A single Python service hosts both interfaces. FastMCP's `@mcp.custom_route` lets one process serve the MCP endpoint and a REST API side by side:

```
  Poke (iMessage / SMS)  ──►  /mcp     ┐
                                       ├──►  FastMCP service (Render)  ──►  Notion API
  PWA (Vercel, iOS)      ──►  /api/*   ┘
```

**This structure is mandatory.** Do not have the Next.js app call Notion directly. All business logic — the day-boundary rule, macro totals, remaining calculations, validation — lives exactly once, in Python. The PWA is a pure presentation client and holds no Notion credentials. Duplicating this logic in TypeScript will cause the two clients to drift and is the single worst mistake you can make on this build.

---

## 2. The existing Notion workspace — READ THIS FIRST

These IDs are real and verified. Use them verbatim. **Do not create, recreate, or migrate the meal log database.**

Parent page **`Daily Food Log`**
```
3415aac7-7d31-8176-8d64-eb008a13e919
```

### `Nutrition Entries` — the meal log (USE AS-IS)

```
database    id:  f450b636-8123-409f-b1e5-960251f377dd
data source id:  a9165caa-f1ba-4a3d-9d9b-c850bd1b4c4c
```

| Property | Type | Notes |
|---|---|---|
| `Name` | title | Free text description of the food |
| `Meal` | select | `Breakfast`, `Lunch`, `Dinner` — you will add `Snack` |
| `Calories` | number | **No `(g)` suffix** |
| `Protein (g)` | number | |
| `Carbs (g)` | number | |
| `Fat (g)` | number | |
| `Date` | date | Date only, no time component |
| `C` | formula | **READ-ONLY. Never write to this property.** |

Property names are exact and case-sensitive, including the ` (g)` suffixes and the fact that `Calories` has none. Mismatching these is the most common way this build fails — Notion returns a validation error naming the property, so if you see one, fix the name rather than changing the schema.

Each row is **one meal**. There are no per-day rows. The Notion UI shows days because the default view groups by `Date` — that grouping is a display concern and requires nothing from you.

### Out of scope — do not touch

A database named `Daily Meals` (`c967e104-6b11-4ab6-8cd8-d0efcead79a3`) exists with a similar but different schema. It is abandoned. **Do not read it, write to it, migrate it, delete it, or deduplicate it against `Nutrition Entries`.** Ignore it entirely.

A database named `Daily Goals` also exists under a similar name — it is a generic todo list (Status/Priority/Tag), unrelated to macros. Ignore it.

---

## 3. Notion changes you must make

### 3a. One additive schema change to `Nutrition Entries`

Add a `Snack` option to the existing `Meal` select property. This is additive — it must not rename, remove, or reorder the existing `Breakfast`, `Lunch`, `Dinner` options, and must not modify any existing rows.

Use `PATCH /v1/data_sources/{data_source_id}` and send the **complete** options list (existing three plus `Snack`); Notion replaces the option set wholesale, so omitting the existing options would destroy them.

### 3b. Two new databases under `Daily Food Log`

Create both as children of page `3415aac7-7d31-8176-8d64-eb008a13e919`.

**`Macro Targets`**

| Property | Type |
|---|---|
| `Name` | title |
| `Effective Date` | date |
| `Calories` | number |
| `Protein` | number |
| `Carbs` | number |
| `Fat` | number |

Targets are read as: the row with the **latest `Effective Date` that is ≤ the requested day**. This preserves history when targets change between cut and bulk phases. Seed one row dated `2026-01-01` named `Initial` with `Calories 2400`, `Protein 215`, `Carbs 200`, `Fat 70` — these are starting values the user will edit in Notion.

**`Meal Presets`**

| Property | Type |
|---|---|
| `Name` | title |
| `Emoji` | rich_text |
| `Calories` | number |
| `Protein` | number |
| `Carbs` | number |
| `Fat` | number |
| `Meal` | select — `Breakfast`, `Lunch`, `Dinner`, `Snack` |
| `Sort Order` | number |
| `Active` | checkbox |

Backs the PWA's quick-add tiles. Create it empty — the user populates it, or Poke does via `save_preset`.

### 3c. Setup script

Provide `setup.py` at the repo root that performs 3a and 3b idempotently — safe to run twice, creating nothing that already exists — and prints the resulting data source IDs in `.env` format for the user to paste. It must locate existing databases by title under the parent page before creating anything.

---

## 4. Macro sourcing rule — non-negotiable

The user has a standing rule:

> Never estimate macros. Use the FDA database for whole foods. Research specific brand labels for all other products. Do not guess portion macros or estimate weights.

This shapes the tool design:

- `log_meal` takes a required `macro_source` string describing where the numbers came from (e.g. `"FDA FoodData Central: chicken breast, roasted"`, `"Fairlife Core Power label"`, `"user provided from package"`).
- The tool description must explicitly instruct the calling model to look up real values and never estimate.
- The server **rejects** a call with an empty, whitespace-only, or obviously-placeholder `macro_source` (e.g. `"estimate"`, `"approx"`, `"guess"`, `"unknown"`) with a clear error telling the caller to look up the label or FDA entry first.
- The server performs an **Atwater cross-check**: if `|(4·protein + 4·carbs + 9·fat) − calories|` exceeds `max(50, 0.20 × calories)`, the entry is still written but the response includes a `warning` field naming the discrepancy. This catches transcription errors cheaply without blocking legitimate edge cases like alcohol or fiber.
- `macro_source` is appended to the page body as a paragraph block, not stored in a property (the schema has no field for it and you must not add one).

---

## 5. The day-boundary rule

The user's day rolls over at **4:00am Eastern**, not midnight. A meal logged at 2am belongs to the day they were awake for.

Implement this once, in one function, and use it everywhere:

```python
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")
DAY_ROLLOVER_HOUR = 4

def effective_date(now: datetime | None = None) -> date:
    """The logging day for a given instant. The day rolls at 4am local."""
    now = now or datetime.now(LOCAL_TZ)
    return (now.astimezone(LOCAL_TZ) - timedelta(hours=DAY_ROLLOVER_HOUR)).date()
```

Verify against these cases and include them as tests:

| Local time | Resolves to |
|---|---|
| Sat 11:59pm | Saturday |
| Sun 12:01am | Saturday |
| Sun 3:59am | Saturday |
| Sun 4:01am | Sunday |
| Sun 2:00pm | Sunday |

**Never use `datetime.now()` without a timezone, and never use UTC for this.** Under UTC, everything logged after 8pm Eastern would silently land on the next day — the exact bug this rule exists to prevent.

Because "today" and the calendar date disagree between midnight and 4am, the PWA must **label the day it is displaying** (e.g. "Saturday, July 25") rather than saying "Today" unconditionally.

---

## 6. Repository layout

```
ai-macro-tracker/
├── PROMPT.md
├── README.md
├── setup.py
├── server/
│   ├── src/
│   │   ├── server.py          # FastMCP instance, tools, custom routes, entrypoint
│   │   ├── notion.py          # Notion HTTP client (httpx)
│   │   ├── domain.py          # effective_date, totals, Atwater check, validation
│   │   └── config.py          # env var loading
│   ├── tests/
│   │   ├── test_domain.py     # day boundary + totals + Atwater
│   │   └── test_notion.py     # client shape, mocked
│   ├── requirements.txt
│   └── render.yaml
└── web/
    ├── app/
    │   ├── layout.tsx
    │   ├── page.tsx           # today view
    │   ├── globals.css
    │   └── api/auth/route.ts  # passcode exchange -> httpOnly cookie
    ├── components/
    │   ├── MacroRings.tsx
    │   ├── PresetGrid.tsx
    │   ├── MealList.tsx
    │   └── DayHeader.tsx
    ├── lib/
    │   └── api.ts             # typed fetch wrappers against the Python service
    ├── public/
    │   ├── manifest.json
    │   ├── icon-192.png
    │   ├── icon-512.png
    │   └── apple-touch-icon.png
    ├── middleware.ts          # passcode gate
    ├── next.config.ts
    ├── package.json
    └── tsconfig.json
```

---

## 7. The Notion client (`server/src/notion.py`)

**Use raw `httpx`. Do not use the `notion-client` package** — it lags behind the API version this build requires.

Every request sends:
```
Authorization: Bearer {NOTION_TOKEN}
Notion-Version: 2025-09-03
Content-Type: application/json
```

### API version 2025-09-03 is a breaking change

Notion databases now parent one or more *data sources*, and most operations moved. Get this wrong and nothing works.

| Operation | Correct call |
|---|---|
| Query rows | `POST /v1/data_sources/{data_source_id}/query` |
| Create a row | `POST /v1/pages` with parent `{"type":"data_source_id","data_source_id":"..."}` |
| Update a row | `PATCH /v1/pages/{page_id}` |
| Delete a row | `PATCH /v1/pages/{page_id}` with `{"archived": true}` |
| Create a database | `POST /v1/databases`, properties under `initial_data_source.properties` |
| Get data source id | `GET /v1/databases/{database_id}` → `data_sources[0].id` |
| Update schema | `PATCH /v1/data_sources/{data_source_id}` |

**Forbidden:** `POST /v1/databases/{id}/query`. That endpoint is the pre-2025-09-03 form and will fail. If you find yourself writing it, stop.

If a search call is needed, the object filter value is `"data_source"`, not `"database"`.

Before writing this module, fetch `https://developers.notion.com/docs/upgrade-guide-2025-09-03` and confirm the request shapes above still match. If the live docs disagree with this table, follow the live docs and note the deviation in `README.md`.

### Implementation requirements

- Query all meals for a day with a `Date` equals filter on the ISO date string.
- Paginate: follow `next_cursor` until `has_more` is false. Never assume one page.
- Retry `429` and `5xx` with exponential backoff, honoring `Retry-After` when present. Cap at 4 attempts.
- Notion number properties can be `null`. Coerce to `0.0` when totaling — never let a `None` propagate into arithmetic.
- Surface Notion's error `message` field in raised exceptions. A bare `400` with no context wastes debugging time.

---

## 8. MCP server (`server/src/server.py`)

```python
from fastmcp import FastMCP
mcp = FastMCP("macro-tracker")
```

Run with streamable HTTP:
```python
mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
```

**Poke connects to the path `/mcp` and no other path.** FastMCP serves this by default under the HTTP transport. Do not remount it, do not add a prefix, do not rename it.

### Tools

Each tool needs a clear docstring — Poke reads these to decide when to call. Write them for a model that only sees the name, description, and schema. All arithmetic happens server-side; the caller never computes totals.

| Tool | Arguments | Behavior |
|---|---|---|
| `log_meal` | `name: str`, `calories: float`, `protein: float`, `carbs: float`, `fat: float`, `macro_source: str`, `meal: str = "Snack"`, `date: str \| None = None` | Validate, run Atwater check, create row in `Nutrition Entries`, append `macro_source` as a body paragraph. Returns the day's updated totals and remaining. `date` defaults to `effective_date()`. |
| `log_preset` | `preset_name: str`, `servings: float = 1.0`, `meal: str \| None = None`, `date: str \| None = None` | Look up preset by case-insensitive name among `Active` presets, multiply macros by `servings`, log it. `macro_source` is recorded as `"Meal Preset: {name}"`. Errors listing available preset names if not found. |
| `list_presets` | — | All `Active` presets ordered by `Sort Order`, with per-serving macros. |
| `save_preset` | `name: str`, `calories: float`, `protein: float`, `carbs: float`, `fat: float`, `meal: str = "Dinner"`, `emoji: str = "🍽️"` | Create or update a preset by name. Lets the user say "save that as my usual dinner". |
| `get_today` | — | Totals, targets, remaining, and the day's meals. Includes the resolved date so the caller can state which day it means. |
| `get_day` | `date: str` | Same as `get_today` for an explicit `YYYY-MM-DD`. |
| `get_range_summary` | `start: str`, `end: str` | Per-day totals plus averages across the range. Inclusive of both ends. |
| `undo_last_meal` | — | Archive the most recently created entry for the current effective day. Returns what was removed and the corrected totals. Errors clearly if the day has no entries. |
| `get_targets` | `date: str \| None = None` | Effective targets for that day. |
| `set_targets` | `calories: float`, `protein: float`, `carbs: float`, `fat: float`, `effective_date: str \| None = None` | New row in `Macro Targets`, defaulting to today. |

### Validation applied to every write

- Macros must be non-negative and finite. Reject `NaN` and `inf`.
- Calories ≤ 10000, each macro ≤ 1000 per entry — a typo guard, not a nutrition opinion.
- `meal` must be one of `Breakfast`, `Lunch`, `Dinner`, `Snack`. Match case-insensitively and normalize before writing; reject anything else with the valid list in the message.
- `date` strings must parse as `YYYY-MM-DD`.
- `macro_source` must be non-empty and not a placeholder (see §4).

Errors must be actionable sentences, since Poke relays them to the user over text. `"meal must be one of Breakfast, Lunch, Dinner, Snack — got 'brunch'"` is useful; `"ValidationError"` is not.

---

## 9. REST API for the PWA

Same process, via `@mcp.custom_route`:

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

@mcp.custom_route("/api/today", methods=["GET"])
async def api_today(request: Request) -> JSONResponse:
    ...
```

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/today` | Current day: date, totals, targets, remaining, meals, presets |
| `GET` | `/api/day/{date}` | A specific day |
| `GET` | `/api/presets` | Active presets |
| `POST` | `/api/log-preset` | `{preset_name, servings, meal?}` — quick-add |
| `POST` | `/api/log` | `{name, calories, protein, carbs, fat, macro_source, meal}` — manual entry |
| `DELETE` | `/api/meal/{page_id}` | Archive one entry |
| `GET` | `/health` | Returns `200 OK`, no auth — for uptime checks |

`/api/today` response shape:

```json
{
  "date": "2026-07-25",
  "day_label": "Saturday, July 25",
  "totals":    { "calories": 1840, "protein": 168.5, "carbs": 142, "fat": 61 },
  "targets":   { "calories": 2400, "protein": 215,   "carbs": 200, "fat": 70 },
  "remaining": { "calories": 560,  "protein": 46.5,  "carbs": 58,  "fat": 9  },
  "meals": [
    {
      "id": "35a5aac7-7d31-81bb-84e9-cc9338611008",
      "name": "Fairlife Core Power 30g shake",
      "meal": "Snack",
      "calories": 150, "protein": 30, "carbs": 3, "fat": 2.5,
      "created_time": "2026-07-25T14:22:00.000Z"
    }
  ],
  "presets": [
    { "name": "Usual dinner", "emoji": "🍗", "calories": 620,
      "protein": 68, "carbs": 45, "fat": 18, "meal": "Dinner", "sort_order": 1 }
  ]
}
```

`remaining` may be negative — the UI must handle overage rather than clamping to zero.

### API auth

All `/api/*` routes require header `X-App-Token: {APP_SHARED_TOKEN}`. Return `401` with a JSON body when it is missing or wrong. `/mcp` and `/health` are exempt — `/mcp` uses Poke's own bearer credential.

Enable CORS for the Vercel origin on `/api/*`: allow `GET, POST, DELETE, OPTIONS`, allow `Content-Type` and `X-App-Token`, and handle preflight `OPTIONS`.

---

## 10. The PWA (`web/`)

Next.js (App Router) + TypeScript + Tailwind, deployed to Vercel.

### Behavior

- **Today view** is the entire app. No navigation, no tabs, no routing beyond the root page.
- Four macro readouts — calories, protein, carbs, fat — as progress rings or bars showing consumed against target, with the remaining number prominent. Protein is the user's primary metric; give it visual weight.
- **Overage state**: when a macro exceeds target, the indicator visibly changes (color shift, full ring) and remaining shows as negative. Do not clamp or hide it.
- **Preset grid** of tap-to-log tiles below the rings — emoji, name, calorie count. One tap logs one serving and optimistically updates the rings; on failure, roll back and surface the error inline.
- **Meal list** for the day, newest first, each row swipe- or long-press-deletable with a confirmation.
- **Day header** naming the day being shown (§5).
- **Live refresh**: poll `/api/today` every 15 seconds, plus immediately on window focus and on regaining network. This is how numbers logged via Poke appear without a manual reload. Use SWR or TanStack Query with `refreshInterval` and `revalidateOnFocus` rather than a hand-rolled `setInterval`.
- **Passcode gate**: `middleware.ts` checks an httpOnly cookie; unauthenticated visitors get a passcode screen that posts to `/api/auth`, which compares against `APP_PASSCODE` and sets the cookie with a 1-year expiry. Compare with `crypto.timingSafeEqual`. This is a lock on a personal app, not an auth system — do not add user accounts.
- The browser must never see `NOTION_TOKEN` or `APP_SHARED_TOKEN`. All calls to the Python service go through Next.js route handlers or server components that inject `X-App-Token` server-side.

### iOS home-screen requirements

Getting installed properly on iOS needs all of these:

- `public/manifest.json` with `name`, `short_name`, `start_url: "/"`, `display: "standalone"`, `background_color`, `theme_color`, and 192px + 512px icons.
- `<link rel="apple-touch-icon" href="/apple-touch-icon.png">` — 180×180. **iOS ignores the manifest icons for the home-screen icon and uses this.**
- `<meta name="apple-mobile-web-app-capable" content="yes">` — without it, the app opens in Safari with browser chrome instead of fullscreen.
- `<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">`
- Viewport: `width=device-width, initial-scale=1, viewport-fit=cover`
- Respect the notch and home indicator with `env(safe-area-inset-top)` / `env(safe-area-inset-bottom)` padding — content under the status bar looks broken.
- Generate the icon PNGs as part of the build. A flat background with a bold glyph is fine; do not reference image files that don't exist.

### Visual direction

Dark, high-contrast, legible at a glance one-handed. Large numerals for the figures that matter. Generous tap targets — minimum 44×44pt. Restrained motion: ring fills animate, nothing else. This is a utility checked twenty times a day, not a landing page.

---

## 11. Configuration

`server/.env`:

| Variable | Purpose |
|---|---|
| `NOTION_TOKEN` | Notion internal integration secret |
| `NUTRITION_DS_ID` | `a9165caa-f1ba-4a3d-9d9b-c850bd1b4c4c` |
| `TARGETS_DS_ID` | From `setup.py` output |
| `PRESETS_DS_ID` | From `setup.py` output |
| `PARENT_PAGE_ID` | `3415aac7-7d31-8176-8d64-eb008a13e919` |
| `APP_SHARED_TOKEN` | Shared secret for `/api/*` |
| `LOCAL_TZ` | `America/New_York` |
| `DAY_ROLLOVER_HOUR` | `4` |
| `PORT` | Injected by Render |

`web/.env.local`:

| Variable | Purpose |
|---|---|
| `MACRO_API_URL` | `https://<service>.onrender.com` |
| `APP_SHARED_TOKEN` | Must match the server |
| `APP_PASSCODE` | PWA unlock code |

Commit a `.env.example` for both. Commit no real secrets. Fail fast at startup with a clear message naming any missing required variable — do not start a server that will 500 on first request.

`server/render.yaml`: web service, Python env, build `pip install -r requirements.txt`, start `python src/server.py`, health check path `/health`.

---

## 12. Definition of done

- [ ] `setup.py` runs twice with no duplicates and no errors, printing the two data source IDs.
- [ ] `Snack` exists on the `Meal` select; `Breakfast`, `Lunch`, `Dinner` still exist; no existing rows changed.
- [ ] `Macro Targets` and `Meal Presets` exist under `Daily Food Log`, targets seeded.
- [ ] Server starts locally; `/health` returns 200; `/mcp` responds to an MCP initialize.
- [ ] All ten tools are registered with schemas and docstrings.
- [ ] `log_meal` writes a row to `Nutrition Entries` that appears in the Notion UI under the correct day group with correct property values.
- [ ] `macro_source` lands in the page body; placeholder values are rejected.
- [ ] Atwater mismatch produces a `warning` without blocking the write.
- [ ] `undo_last_meal` archives the right row and returns corrected totals.
- [ ] Day-boundary tests pass for all five cases in §5.
- [ ] `/api/*` returns `401` without `X-App-Token`, `200` with it.
- [ ] PWA renders rings, presets, and meals against the live service.
- [ ] Preset tap logs and updates optimistically; failure rolls back.
- [ ] Overage renders as negative remaining with a changed indicator.
- [ ] Polling picks up a meal logged elsewhere within 15 seconds.
- [ ] Passcode gate blocks unauthenticated access; cookie is httpOnly.
- [ ] `next build` and `pytest` both pass clean.
- [ ] No `NOTION_TOKEN` or `APP_SHARED_TOKEN` reachable from client bundles.

## 13. Self-verification before you finish

Grep your own output and confirm:

1. **No `/v1/databases/{...}/query` anywhere.** Every row query hits `/v1/data_sources/{...}/query`.
2. **`Notion-Version: 2025-09-03`** on every Notion request.
3. **Property names exact**: `Calories`, `Protein (g)`, `Carbs (g)`, `Fat (g)`, `Name`, `Meal`, `Date`. No writes to `C`.
4. **No reference to `Daily Meals`** (`c967e104-…`) anywhere in the code.
5. **No naive `datetime.now()`** without a timezone, and no UTC in day-boundary logic.
6. **MCP served at `/mcp`**, unprefixed.
7. **No TypeScript file imports or references `NOTION_TOKEN`.**
8. **Every env var referenced in code appears in `.env.example`.**
9. **Every endpoint `web/lib/api.ts` calls exists in §9.**
10. **No placeholder comments, no `TODO`, no `pass  # implement later`.**

Then write `README.md` covering: what this is, the two-service architecture, local development for both, deployment to Render and Vercel, and a **Manual steps** section listing what only the user can do:

1. Create a Notion internal integration at `notion.so/my-integrations` and copy the secret.
2. Open the `Daily Food Log` page → `⋯` → **Connections** → add the integration. *(Without this, every Notion call returns 404 — it is the most common setup failure.)*
3. Run `setup.py`, paste the printed IDs into the environment.
4. Deploy the server to Render, deploy the web app to Vercel.
5. Add the MCP integration in Poke at `poke.com/settings/connections/integrations/new` using `https://<service>.onrender.com/mcp`.
6. Open the Vercel URL on iPhone → Share → **Add to Home Screen**.

Note in the README that Render's free tier sleeps after inactivity, so the first Poke message or app open after idle may take ~30 seconds. This affects no data, only latency.
