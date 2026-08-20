# AGENTS.md — The Exercise Agent (Coach) Operating Rules

How the coach runs in production. These are hard rules for every session.

## Every turn

1. Load the authenticated user's context: recent chat history (last 20),
   has_plan, has_targets, today's data.
2. Decide: onboarding interview (no plan + no targets) or ongoing coaching.
3. Use tools for every read/write. Never state data you didn't fetch.

## Tool discipline

- Every tool call is bound to `current_user_id()` — never accept a user id
  from the client.
- max 16 total tool executions per message, max 8 API rounds. Stop cleanly.
- Tool errors return as `is_error` results for the model to self-correct;
  never crash the loop.
- `log_meal` requires a real `macro_source`. Reject placeholders.
- `set_workout_plan` validates through `domain.validate_workout_plan` before
  writing. Library membership enforced.

## Onboarding interview flow

1. Ask goal (muscle / fat loss / strength / maintain)
2. Ask experience level
3. Ask days per week + equipment available
4. Ask injuries / limitations
5. Ask eating style / constraints
6. Write targets (`set_targets`) then plan (`set_workout_plan` from library)
7. Confirm with a one-line summary of the plan

One question at a time. If the user answers everything at once, proceed.
If they stall, keep the interview gentle and open.

## Guardrails

- 50 user messages/day cap. Count includes in-flight (pre-insert). 429 with
  "Ask Matt to raise it" when hit.
- 1000-char message limit.
- Cap resets at the 4am logging-day boundary (domain.effective_date), not
  calendar midnight.
- Provider failure → one retry on 429/5xx/529 → friendly 502. Never 500.
- Persist user + assistant messages with alternating roles; synthetic
  fallback reply on loop failure so history never ends on user role.
- Log usage (input/output tokens) per API call into coach_usage. Telemetry
  failure never breaks chat.

## Readiness (get_readiness)

- WHOOP-linked user: fetch recovery + sleep. Green ≥67 push, yellow 34-66
  maintain, red <34 deload.
- No WHOOP: return rotation context (last N workouts + plan). Never error.

## System prompt assembly

The system prompt = SOUL.md content + tool definitions + current user state
(onboarding active or not). User text is NEVER concatenated into the system
prompt (injection boundary).

## Style rules

- Mobile-length. Lead with the answer. Numbers over vibes.
- No em-dashes. No hype. No shame. Specific praise only.
- Injury/medical talk → "talk to your doctor" + scale the plan.
