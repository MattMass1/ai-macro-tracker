# ElevenLabs voice agent for MMMacros — design spec

Date: 2026-10-08
Branch: `feat/elevenlabs-voice`
Status: design approved (Sections 1–3); external ElevenLabs API specifics under research.

## Goal

Add an ElevenLabs Agents live-voice mode that sits beside the existing GPT
Realtime voice, behind a feature flag, changing **only the voice layer**. The
coach's reasoning, tools, food lookup, database writes and verification stay
exactly as they are. Single user (Matthew).

## Non-goals

- No change to `CanvasService` business logic, food lookup, DB schema/migrations,
  existing tools, or the GPT Realtime path.
- No literal telephony (no Twilio/SIP phone number). This is an in-app live voice
  session over WebRTC that *feels* like a call.
- No MCP plumbing for the voice agent. Client tools are UI-only and never write.
- No redesign of the canvas UI.

## Experience

A live, in-app, full-duplex voice session: open mic, the coach replies in real
time, the user can barge in (interrupt) and the coach stops. No push-to-talk, no
record-and-send. Confirmation of writes is **spoken**, not tapped. A native
confirmation card is reserved for one case only: an unverified / reconciliation
outcome.

## Architecture

### The seam (iOS) — option B, controller-level

The existing `LiveCoachTransporting` protocol is frame-based (the app captures
mic PCM at 24 kHz via `LiveCoachAudioEngine` and sends it, and plays received
audio deltas). The ElevenLabs Swift SDK uses LiveKit WebRTC and owns the entire
audio pipeline itself, so that protocol is the wrong seam.

Introduce a higher-level protocol at the controller boundary:

- `CoachVoiceSession` (new protocol). `LiveCoachController` talks to this.
- `GPTVoiceSession` — wraps the existing `LiveCoachWebSocketTransport` +
  `LiveCoachAudioEngine`, behavior unchanged.
- `ElevenLabsVoiceSession` — drives the ElevenLabs SDK, owns its own audio (the
  SDK/LiveKit does capture + playback + VAD + barge-in). `LiveCoachAudioEngine`
  is **not used** on this path.

Both implementations map onto `LiveCoachController`'s existing published states:
`ready / connecting / listening / speaking / failed / ended`, `userCaption`,
`coachCaption`, `activityState`, `committedMealOperationID`,
`committedMealLabel`, `mealReconciliationOperationID`. The controller's public
surface and all `AgentCanvasView` UI are unchanged.

`AgentSurfaceStore.makeVoice(sessionId, provider:)` picks the implementation
based on the resolved voice provider (flag + local debug override).

### The seam (server)

New module `server/src/elevenlabs_voice.py`, two routes registered in
`create_app`:

- `POST /api/voice/elevenlabs/token` — device-bearer auth via existing
  `api_route` (owner-only: reject anyone but `CONFIG.matt_user_id`). Uses
  `ELEVENLABS_API_KEY` (server env only) to mint a short-lived WebRTC
  conversation token for the configured agent ID; returns token + agent ID.
  The API key never leaves the server; only the short-lived token is returned.
- `POST /api/voice/elevenlabs/agent-turn` — the webhook ElevenLabs calls.
  Server-to-server auth via `ELEVENLABS_TOOL_SECRET` header (route is
  `public=True` at the `api_route` layer, then gated by the secret; constant-time
  compare). Binds `bind_user(CONFIG.matt_user_id)`. Derives a trusted
  `session_id = uuid5(NAMESPACE, conversation_id)` from the ElevenLabs
  conversation id (a platform field, never model output), then calls the
  existing `canvas_service().voice_handler(session_id)(tool_call_id, {"message": ...})`.
  That reuse gives `turn_id = uuid5(session_id, tool_call_id)` and the
  `canvas:{session_id}:{turn_id}` business key with zero `CanvasService` change.

Never accept user identity from the model. The session id is server-derived from
the platform conversation id, not supplied by the model.

### End-to-end data flow

```
iPhone --token--> POST /api/voice/elevenlabs/token      (device bearer, owner-only)
iPhone <--conv token-- server (mints via ELEVENLABS_API_KEY)
iPhone --WebRTC audio--> ElevenLabs agent (small relay LLM)
                           every user utterance ->
         POST /api/voice/elevenlabs/agent-turn          (ELEVENLABS_TOOL_SECRET)
                           uuid5(conversation_id) -> voice_handler -> CanvasService.turn(adapter="voice")
                           existing reasoning / food lookup / idempotent write / readback
                           returns reply text ->
         ElevenLabs speaks the reply
```

## Writes, confirmation, idempotency (non-negotiable)

Reuse the existing invariant unchanged. The server derives the business key; the
model never supplies idempotency identities.

- Meal writes follow the existing canvas behavior: `CanvasService.turn`'s
  `log_meal` commits through `foods["log_meal"](f"canvas:{session_id}:{turn_id}", ...)`,
  which owns provenance, atomicity, duplicate-guard and committed-row readback.
  One write per turn (`last_food_result`). Turn-level replay guard: same
  `turn_id` + same message replays the cached result; same id + different message
  raises.
- Confirmation is **spoken** ("logged six ounces of chicken and a sweet potato,
  you are at 1,840"). No pre-commit tap. Write-safety does not depend on a tap:
  it rests on spoken readback, the food resolver asking when unsure, the
  idempotency + readback chain, the reconciliation path, and undo.
- Unknown outcome: the webhook is one HTTP request. If it times out or the
  connection drops after a possible commit, the outcome is UNKNOWN. The agent
  must not speak "logged" and must not retry blindly. A retry re-sends the same
  `tool_call_id` -> same `turn_id` -> same business key -> the DB returns
  `replayed`, never a second row. If no verified `committed`/`replayed` returns,
  the agent says it is unsure and the existing reconciliation path
  (`mealReconciliationOperationID`) resolves it on next sync.
- Barge-in never undoes a commit. Never trust a model-supplied `confirmed=true`.

## Feature flag

- Server: `VOICE_PROVIDER=openai|elevenlabs` env var, default `openai`. GPT stays
  default until the owner flips it. Added to `.env.example` and the Render env
  docs (never a real value in git).
- App learns it from `/api/today`: add a `voiceProvider` field alongside the
  existing `canvasProtocol` on `DayPayload`. No new endpoint, no new fetch.
- Local debug override on iOS: a `UserDefaults` / launch-arg flag to force
  `elevenlabs` on a debug build without flipping the server flag. Production
  behavior untouched.

## Spike environment (Phase 1, read-only)

- Data: local Postgres with a seeded synthetic Matthew (run `migrations.py`,
  insert fake targets/meals). Never the production DB.
- Reachability: run the server locally and expose it via one cloudflared tunnel
  URL (free quick tunnel). ElevenLabs' webhook and the app's API base both point
  at that URL. Nothing deployed to Render, no Render env touched. Quick-tunnel
  URLs rotate per session; the idempotent setup script re-points the webhook, and
  the app base is a debug override.
- Agent config via an idempotent setup script
  `server/scripts/elevenlabs_agent_setup.py`: system prompt (reused from
  `canvas_live_start`), smallest capable ElevenLabs-hosted relay LLM, voice id
  (configurable, default swappable), the `agent_turn` server tool with URL +
  secret header, turn/interruption settings, retention low, audio saving off.
  Prints the agent id. Makes no paid calls; agent create/update is config.
- Measurements (real, not claimed): end-of-speech -> first audio (p50/p95),
  barge-in, reconnect, tool correctness, cost/minute, vs the GPT path. Read-only
  intents only ("what are my remaining macros", "show today's log").

## Testing

- Server (pytest): token endpoint rejects missing/other-user auth and never leaks
  the API key (only the short-lived token); webhook rejects bad/missing
  `ELEVENLABS_TOOL_SECRET`; `agent_turn` parity with the GPT adapter on identical
  inputs; idempotency + unknown-outcome cases (double-submit same `tool_call_id`
  -> `replayed`, no second row; post-commit timeout -> UNKNOWN then reconciled).
  Full existing suite stays green using `DATABASE_URL=postgresql://fixture.invalid`
  + fixture `APP_SHARED_TOKEN`.
- iOS: unit tests for `CoachVoiceSession` -> `LiveCoachController` state mapping
  using a fake ElevenLabs session (no network, no SDK calls). Build the app. No
  signing/provisioning/account changes.

## Order of work (stop and report after each phase)

1. Spike (read-only): token endpoint, agent_turn webhook, setup script, iOS
   ElevenLabs session behind the flag; read-only intents on synthetic data;
   measure and compare to GPT.
2. One write: `log_meal` through `agent_turn` with spoken confirmation; tests for
   double-submit, post-commit timeout (UNKNOWN then reconciled, no second write),
   reconnect mid-turn.
3. Remaining write tools: workout set logging, undo, complete.

## Risks and assumptions (to verify during Phase 1 / by research)

- The ElevenLabs server-tool webhook includes `conversation_id` and a stable
  per-call `tool_call_id` in its payload (required for the session/turn id
  derivation). If not, fall back to injecting a server-issued session id as a
  dynamic variable at conversation start.
- The ElevenLabs Swift SDK (~3.4.x) supports iOS 17.0 (the app's deployment
  target). If it requires higher, that is a blocker to surface immediately.
- A short-lived WebRTC conversation token can be minted server-side from the API
  key for an authenticated agent.
- A small ElevenLabs-hosted relay LLM is sufficient (reasoning lives behind
  `agent_turn`).

## Owner inputs required (the only planned interruptions)

- `ELEVENLABS_API_KEY` set in `server/.env` (gitignored) when it is time to run
  the setup script / token endpoint live.
- Starting cloudflared (or approving `brew install cloudflared`) for the live
  spike.
- A voice id choice (a default is wired in meanwhile).

## Delivery rules

Branch `feat/elevenlabs-voice`, small commits per phase. Nothing pushed,
deployed, or charged without explicit owner approval. No edits to signing,
`Config.xcconfig` secrets, or provisioning.
