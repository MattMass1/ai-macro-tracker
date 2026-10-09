# Voice → Canvas Fix and Operator Handoff

Audience: an agent (or engineer) maintaining the MMMacros / Macro Coach app. This
documents how the ElevenLabs voice agent was made to render components in the app,
the debugging process used to get there, and the operational facts needed to keep
it running and extend it.

Fixed on branch `master`, commit `2d7ea36` (2026-10-09).

---

## 1. What the system is (voice path)

MMMacros is a SwiftUI iOS app plus a FastAPI server on Render (Postgres). The
coach has a text path and a voice path that share one brain:

- **Coach brain:** `CanvasService.turn(session_id, turn_id, message, *, adapter)` in
  `server/src/agent_canvas.py`. It runs the LLM (`run_agent` in `coach.py`, which
  calls OpenAI chat completions), executes tools (food lookup, workout edits, DB
  writes), and returns a canvas snapshot (surfaces, components, approval, receipt)
  plus a `reply`. `adapter` is `"text"` or `"voice"` and is only validated; it does
  NOT change what the turn produces. Text and voice generate identical components.
- **Voice layer (ElevenLabs):** replaced only the realtime voice I/O (microphone,
  speaker, turn-taking). The ElevenLabs SDK runs its own WebRTC link to ElevenLabs.
  A server webhook runs the shared turn and returns the spoken `reply`.

Key voice-path files:
- `server/src/elevenlabs_voice.py` — token minting, the `agent_turn` webhook logic,
  server-trusted id derivation, and the in-process conversation→user/session map.
- `server/src/server.py` — the two routes: `POST /api/voice/elevenlabs/token` and
  `POST /api/voice/elevenlabs/agent-turn`.
- `ios/MacroTracker/Services/ElevenLabsVoice.swift` — the SDK adapter.
- `ios/MacroTracker/Services/AgentSurfaceStore.swift` — owns the canvas state and
  the voice controller; applies snapshots via `receive(...)`.
- `ios/MacroTracker/Services/APIClient.swift` — HTTP client.
- `ios/MacroTracker/Models/AgentCanvasContract.swift` — the client-side snapshot
  model and `AgentSurfaceState.apply(...)`.

---

## 2. The bug

Symptoms reported:
1. The voice agent said it performed an action (logged a meal, set up a workout
   preview) but **no components rendered** in the app.
2. Asking voice to add or switch a workout produced a "pending preview" the user
   could never resolve, because no Confirm card appeared to tap.
3. Text chat worked fine for all of the above.

## 3. Root cause

Canvas state is keyed by `(user, session_id)` in `CanvasService.session(...)`.
There are therefore many independent canvas sessions per user.

The voice webhook derived its OWN session id from the ElevenLabs conversation id
(`derive_session_id(conversation_id)`), which is a DIFFERENT session from the one
the app is viewing and polling. So the voice turn ran correctly, produced all its
components, and wrote them into a session the app never looked at. ElevenLabs spoke
the `reply`, but the app saw nothing. The app's hard gate
`AgentSurfaceState.apply` drops any snapshot whose `sessionId` does not match the
session it holds (`AgentCanvasContract.swift`, the `guard value.sessionId ==
sessionId else { throw .wrongSession }` line), so even if a stray snapshot had
arrived it would have been rejected.

The GPT voice path never had this problem because it streamed snapshots back to the
app over its own websocket, tied to the app's session id. ElevenLabs has no such
push channel.

## 4. The fix (three parts, all required)

The fix binds the ElevenLabs conversation to the app's own canvas session, then has
the app pull that session while voice is live.

1. **iOS sends its session id at token mint.**
   `APIClient.elevenLabsToken(sessionId:)` posts `{"session_id": <app canvas uuid>}`.
   `LiveElevenLabsConversation(sessionId:)` captures the app's session id and passes
   it through when it requests the token.

2. **Server routes the turn to that session.**
   - The token endpoint records `conversation_id -> (user_id, app_session_id)` via
     `elevenlabs_voice.record_voice_session(..., app_session_id=...)`.
   - The webhook resolves `app_session_id` with
     `elevenlabs_voice.resolve_voice_app_session(conversation_id)` and passes it to
     `run_agent_turn(..., session_id_override=app_session_id)`.
   - `run_agent_turn` validates the override is a canonical UUID
     (`_valid_session_override`) and runs the turn on it instead of the derived id.

3. **iOS pulls the canvas while voice is live.**
   `AgentSurfaceStore` runs a 2-second poll (`startCanvasPull` / `pullCanvasDuringVoice`)
   that calls `api.canvasSnapshot(sessionId:)` and applies via `receive(...)` while
   `voice.state` is `.listening` or `.speaking`. This is the ElevenLabs equivalent
   of the push the GPT path got for free. The poll only runs for the ElevenLabs
   provider and no-ops when idle, busy, or signed out.

**Tenancy is preserved.** The session id from the client is only ever used as one of
that user's own `(user, session_id)` keys. The turn is always bound to the
token-minting user (`bind_user(user_id)`), so a client-supplied id cannot read or
write another user's data. Invalid or absent ids fall back to the derived id, so
the change is backward compatible: an old app build gets the old behavior.

---

## 5. Debugging process (how the root cause was found)

This followed a find-root-cause-before-fixing discipline. The useful moves, in
order, for anyone debugging a similar "it ran but nothing showed" issue:

1. **Disprove the stated hypothesis with code, not opinion.** The report was "the
   backend is not writing components." Reading `turn()` showed `adapter` never gates
   surface generation, so the backend writes identical components for voice and
   text. That reframed it from a backend-write bug to a delivery/render bug.

2. **Find the one gate that could silently drop good data.** Reading
   `AgentSurfaceState.apply` surfaced the `sessionId` match requirement. That made
   "the voice turn is writing to a different session than the app polls" the leading
   hypothesis.

3. **Confirm the tenancy model before routing a client value into it.**
   `CanvasService.session` keys by `(user, session_id)` and never trusts a
   client-supplied tenant, so passing the app's session id through was safe.

4. **Instrument the boundaries instead of guessing.** Added `mmacros.voice` INFO
   logs at the token endpoint and webhook (ids and counts only, never user content)
   so one voice turn would show: did the token record the app session, did the
   webhook resolve it, which session did the turn run on, how many surfaces.

5. **Read the absence, not just the presence.** When the logs came back with the
   full request flow but ZERO `mmacros.voice` lines, that proved the deployed server
   was not running the new code.

6. **Check git, not the working tree.** `git grep <symbol> HEAD` showed the server
   changes were uncommitted. Render builds from committed git, not local files, so
   nothing had shipped. Committing and pushing to `master` deployed it.

Lesson for the operator agent: Render deploys committed `master`. Xcode builds the
local working tree. A fix can be "done" locally and still not be live. Always verify
what is actually running (git `HEAD` for the server, the installed build for the app)
before chasing a code bug.

---

## 6. How to verify / debug the voice path

After any deploy, start a FRESH voice session (see the restart caveat below), then
read the Render logs. A healthy turn shows three lines:

```
mmacros.voice  token minted conv=<id> app_session=<app canvas uuid>
mmacros.voice  webhook conv=<id> user_bound=True app_session=<same uuid>
mmacros.voice  voice turn ran session=<same uuid> surfaces=<N>=1+ approval=<bool>
```

Interpretation:
- `app_session=None` on "token minted" → the app is not sending its session id
  (stale app build, or the `APIClient` change missing). Fix on the app side.
- token has a uuid but `app_session=None` on "webhook" → server not matching it
  (stale server, or the conversation id differs between mint and webhook). Server side.
- `voice turn ran session=<uuid not equal to the id the app is GET-polling>` →
  routing is wrong; the override is not taking effect.
- `voice turn ran session=<the app's id> surfaces>=1` but nothing renders →
  app-side poll/render issue.

The app withholds canvas bodies from its own console logs, so the server
`mmacros.voice` triad is the primary diagnostic.

These diagnostic logs are intentionally still in `server.py`. They are cheap and
safe (ids and counts only). Remove them once the feature is considered stable.

---

## 7. Operational facts the maintainer must know

- **Deploy flow:** push to `master` → Render auto-deploys. There is no separate
  deploy step. Xcode builds the local tree, so iOS changes reach the phone only when
  you rebuild and run in Xcode (they do NOT need a server deploy, and vice versa).
  Many bugs come from only one half being current.

- **In-process session map, lost on restart:** the conversation→user/session map in
  `elevenlabs_voice.py` is in memory with a single worker (`WEB_CONCURRENCY=1`). A
  deploy restarts the process and clears it. Always start a NEW voice session after a
  deploy; a session minted before the restart will be refused ("This voice session
  has expired").

- **The coach brain is OpenAI, by design.** Calls to `api.openai.com/v1/chat/completions`
  in the logs are the reasoning brain (`run_agent`), not a leftover GPT voice path.
  ElevenLabs replaced voice I/O only. Moving the brain off OpenAI is a separate,
  unscoped project.

- **Plan edits require a native Confirm tap, by design.** Adding or switching a
  workout produces a preview (the `request_plan_edit` / `propose_today_workout`
  tools set `status: awaiting_confirmation`) that does not write until the user taps
  Confirm. This is intentional and matches the text path. Meals auto-commit.

- **Xcode / SPM gotcha:** never run CLI `xcodebuild -resolvePackageDependencies`
  while Xcode is open; it causes an ICU checkout race ("unable to create file").
  Package artifacts are cached in `~/Library/Caches/org.swift.swiftpm`, so re-resolves
  need no re-download. Quit Xcode before any CLI package operation.

- **ElevenLabs agent:** `agent_4801m4fdjqqmez1b91e2trv5g3d6` ("MMMacros Coach (voice)")
  lives on the owner's ElevenLabs account, which is SHARED with the BACP project. Do
  not touch the BACP agent. Agent config is managed by
  `server/scripts/elevenlabs_agent_setup.py` (idempotent create/update; agent CRUD is
  not billed). The app↔session mapping is entirely server-side; the ElevenLabs agent
  only sends `conversation_id`, so no agent reconfiguration is needed for the fix.

- **Tenancy is the highest-priority correctness property.** Canvas state is keyed by
  `(user, session_id)`; the voice turn is always bound to the token-minting user.
  Never bind the owner for another user's turn, and never trust a client-supplied
  tenant.

- **Local tests:** this machine does not have the server test deps installed
  (`pytest`, `httpx`, `asyncpg`, `fastmcp`). Pure logic was verified by stubbing
  `httpx` and running the functions directly, plus `python3 -m py_compile`. Full
  pytest and the iOS build run in Xcode / a provisioned environment.
  `server/tests/test_agent_canvas_review.py` is order-dependent (fails alone, passes
  in the full suite).

---

## 8. Security

An ElevenLabs API key (`sk_259...`) and the production Postgres password were pasted
into an earlier chat transcript. The owner should revoke/rotate both. Secrets live in
`server/.env` (gitignored) and Render environment variables, never in the app bundle
or committed files.

---

## 9. Next phase

A full redesign of the chat/canvas interface (the message feed, how components render
inline, the voice UI, the composer). The owner will supply visual examples
(screenshots preferred). Run it as a design pass: examples → two or three concrete
mockup directions to react to → agree on one → then build. This is the "generative
UI" goal where the coach surface composes its own components. Relevant files:
`agent_canvas.py` (server contract), `AgentSurfaceRenderer.swift` (rendering),
`AgentCanvasContract.swift` (client model).
