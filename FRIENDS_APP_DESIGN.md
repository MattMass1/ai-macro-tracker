# Macro Tracker → Friends App with AI Coach — Design Spec

Date: 2026-08-18
Status: Approved direction, pending detailed review
Author: Matt + Claude (brainstorming session)

## 1. Summary

Evolve the existing personal macro/workout tracker into a multi-user iPhone app
that Matt can give to friends via TestFlight. The centerpiece addition is an AI
coach: a chat assistant with tools that onboards each user, writes their workout
plan and macro targets, logs food and workouts from conversation, and answers
"what should I do today?" using rotation history — plus WHOOP recovery data for
users who have that integration.

Friends never touch an API key, token, or config file. Matt runs the single
deployment and supplies the AI provider key.

## 2. Decisions already made (do not re-litigate without Matt)

| Decision | Choice |
|---|---|
| Hosting model | Hosted by Matt; Matt holds all keys; friends just sign up |
| Distribution | iPhone app via Capacitor + TestFlight (Matt has Apple Developer) |
| Feature scope | Everything — macros AND workouts, plus the new AI coach |
| AI assistant depth | "Coach with hands": chat with tools that WRITE to the DB (plans, targets, logs), not advice-only |
| Auth | Invite code + device token. No passwords, no email, no Sign in with Apple (for now) |
| Backend | Stays on Render (upgrade to paid instance ~$7/mo to kill cold starts). AWS deferred to a future "big scale" conversation |
| Fork strategy | Evolve current codebase/deployment in place. Matt's existing data is adopted as user #1. No second repo |
| WHOOP | Per-user integrations table from day one; only Matt's WHOOP wired now. Friend-facing WHOOP OAuth is a later, purely-additive bolt-on |
| Scale intent | Small and trusted (friends). Matt supplies compute/API allotment and will tell friends to be reasonable. Big-scale (school) design is explicitly out of scope for this iteration |

## 3. Current state (what exists)

- server/ — Python FastMCP service on Render. One process serves /mcp
  (Poke/iMessage client) and /api/* (REST for the web/iOS app). Domain logic
  centralized in server/src/domain.py (day boundary 4am America/New_York,
  macro validation, Atwater check, totals). Postgres via server/src/store.py,
  schema in server/src/schema.sql.
- web/ — Next.js app; dual-mode: Vercel-served PWA and Capacitor static
  export for iOS (web/ios/ exists on the ios-capacitor branch).
- Auth today: single shared APP_SHARED_TOKEN header + APP_PASSCODE cookie
  gate. Single-user: NO user_id on any table.
- Existing OpenAI-key-backed chat food parser in server/src/server.py
  (~line 542, _openai_access_token / _post_openai_chat).
- Workout side: fitness_tracker, max_weight, exercise_max_reps tables;
  workout day defaults (Push/Legs rotation) are HARD-CODED to Matt's routine.
- WHOOP: connected via personal MCP/skill outside the app.

## 4. Data model changes

### New tables

users            (id, display_name, created_at, is_admin BOOLEAN DEFAULT FALSE)
invite_codes     (code PK, user_id FK, created_at, claimed_at NULLABLE)
devices          (token_hash PK, user_id FK, label, created_at, last_seen)
integrations     (user_id FK, provider TEXT, access_token, refresh_token,
                  scopes, expires_at, UNIQUE(user_id, provider))
workout_plans    (user_id FK, plan JSONB, updated_at)   -- structured routine:
                  -- days, exercises, sets/reps, rotation order
chat_messages    (id, user_id FK, role, content, tool_calls JSONB NULLABLE,
                  created_at)
Notes:
- invite_codes.user_id: a code is minted FOR a user row (created at mint
  time). Claiming a code binds a device to that user. Lost phone → mint a new
  code for the SAME user id; data intact. Codes are single-use (claimed_at).
- devices.token_hash: device tokens are long random strings; only the hash is
  stored server-side. Client stores the raw token in the iOS Keychain.
- integrations: tokens encrypted at rest is nice-to-have; at minimum never
  returned by any API response.

### Existing tables — add user_id

Every one of: days, meals, nutrition_entries, fitness_tracker,
max_weight, exercise_max_reps, meal_presets, macro_targets, briefs.

Key widenings:
- days: PK (date) → (user_id, date)
- meals: UNIQUE(day, meal_type) → UNIQUE(user_id, day, meal_type)
- All indexes gain a leading user_id where they serve per-user queries.

## 5. Auth

### Flow
1. Matt mints an invite code (admin CLI command or admin-only endpoint —
   implementer's choice, CLI is fine at this scale).
2. Friend installs the app from TestFlight, enters the code once.
3. POST /api/claim-invite {code} → server validates unclaimed code, creates a
   device token, stores its hash, marks code claimed, returns the raw token.
4. App stores the token in the Keychain. All subsequent requests send
   Authorization: Bearer <token>.
5. Middleware resolves token hash → user_id; EVERY query in store.py is
   scoped by that user id. No route may accept a user id from the client.

### What this replaces
- APP_PASSCODE + lock screen (web/app/lock/, web/middleware.ts passcode
  gate) — replaced by the invite-code screen.
- APP_SHARED_TOKEN for the iOS/web app path — replaced by device tokens.
  (The web PWA served from Vercel can keep working the same way with the same
  device-token auth; it is not the primary target but should not be broken.)

### What stays
- /mcp (Poke) keeps its existing auth and is PINNED to Matt's user id.
- /health stays unauthenticated.

## 6. The AI coach

### Shape
Server-side agent loop behind POST /api/chat (and the tools it needs), with
per-user history in chat_messages. The client is a thin chat UI.

### Provider
Recommendation: Claude (claude-sonnet-5) via the Anthropic API for the coach —
tool-use quality is the whole feature. The existing OpenAI food parser may stay
as-is in v1 (it works today), with a follow-up task to fold parsing into the
coach so there is ONE provider and ONE key. Reviewer may collapse to a single
provider immediately if that simplifies the build.

### Tools (all auto-scoped to the authenticated user)
Thin wrappers over existing domain functions:
- get_today, get_day, get_range_summary
- log_meal, log_preset, save_preset, undo_last_meal
- get_targets, set_targets
- log_workout, get_recent_workouts (wrap existing fitness_tracker paths)
- NEW set_workout_plan / get_workout_plan — read/write workout_plans.plan
- NEW get_readiness — if the user has a whoop row in integrations,
  fetch recovery/sleep; otherwise return rotation context only (last N
  workouts, plan order). The coach must degrade gracefully — a WHOOP-less
  user gets rotation-based suggestions, never an error.

### Behaviors
1. Onboarding interview. First launch after claiming an invite lands in
   chat. Coach asks goals, experience, equipment, schedule — then writes macro
   targets (set_targets) and a workout plan (set_workout_plan). No forms.
2. "What should I do today?" One-tap prompt. Coach reads plan + recent
   workouts + readiness and answers with today's session.
3. Ongoing logging/adjusting. "log 2 eggs and toast", "swap leg day",
   "cut my calories a bit" — all through the same tool set.

### Guardrails (Matt's key, trusted friends, but circuit breakers anyway)
- Per-user daily message cap (default 50) → friendly "ask Matt to raise it".
- Max agent-loop iterations per message (e.g. 8 tool rounds).
- macro_source validation stays: the coach must supply real sources, the
  domain layer's rejection of estimate/guess placeholders is unchanged.

## 7. Workout generalization
The hard-coded Push/Legs defaults (see recent commits b953685, a3ae970) move
into DATA: Matt's current routine is seeded as HIS workout_plans row by the
migration. UI and coach read the plan from the DB. Streak/weekly-reset logic
becomes per-user. No friend ever sees Matt's routine as a default — their plan
comes from their onboarding interview.

## 8. iOS app changes

- Capacitor branch (ios-capacitor) becomes mainline.
- Replace passcode lock screen with invite-code entry screen.
- Store device token in Keychain (secure storage plugin), not localStorage.
- Add a Chat tab (the coach) alongside existing Today/Workout views.
- Remove NEXT_PUBLIC_APP_SHARED_TOKEN / NEXT_PUBLIC_APP_PASSCODE from the
  mobile bundle entirely — embedding shared secrets in the app binary is
  exactly what the device-token model eliminates.
- Ship via TestFlight (Matt has the developer account; external-tester invites
  are ample for friends-scale).

## 9. Migration

One idempotent script in the style of scripts/backfill_meals.py:
1. Create all new tables (schema.sql grows; keep the additive/IF NOT EXISTS
   convention already used there).
2. Add user_id columns; create Matt's users row; stamp ALL existing rows
   with Matt's id; widen PKs/uniques as §4.
3. Seed Matt's hard-coded routine into his workout_plans row.
4. Insert Matt's WHOOP credentials into integrations (values supplied via
   env/CLI at run time, not committed).
5. Safe to run twice (assert-and-skip, matching existing backfill style).

Run against the live Render DB after a pg_dump backup. The app must behave
identically for Matt before and after (his Poke flow included).

## 10. Testing

Additions to the existing pytest suite:
- Tenancy isolation — the most important tests in the app. User A's token
  can never read or write user B's rows, on every route and every coach tool.
- Auth: claim flow (valid, already-claimed, bogus code), bearer resolution,
  missing/invalid token → 401.
- Migration idempotency: run twice on a seeded DB, identical result.
- Coach loop against a FAKED provider (no API spend in CI): tool dispatch,
  user scoping of tools, iteration cap, message cap.
- Existing domain tests (day boundary, totals, Atwater) must keep passing.

## 11. Rollout order (suggested phases)

1. Multi-tenant core: schema migration + auth + scoped store. Matt dogfoods.
2. Coach backend: /api/chat, tools, onboarding interview, guardrails.
3. iOS: invite screen, Keychain, Chat tab. TestFlight build to Matt's phone.
4. WHOOP-aware get_readiness for Matt's account.
5. First friend invite. Iterate.

## 12. Out of scope (this iteration)

- AWS migration (revisit at "big scale" conversation; Render paid tier now).
- Friend-facing WHOOP OAuth (architecture supports it; build when a friend
  with a band asks).
- Sign in with Apple / passwords / account recovery beyond re-minting codes.
- App Store public release (TestFlight only).
- Billing, quotas beyond the message cap, or any monetization.
- Android.

## 13. Open questions for the reviewing agent

- Collapse to a single AI provider now (coach + parser) or keep the existing
  OpenAI parser through v1? (Matt's call was "recommend Claude, reviewer may
  simplify".)
- workout_plans.plan JSONB shape — reviewer should propose the concrete
  structure the coach writes and the UI reads.
- Whether the Vercel-served PWA keeps the old passcode path during transition
  or cuts over to device tokens immediately.
