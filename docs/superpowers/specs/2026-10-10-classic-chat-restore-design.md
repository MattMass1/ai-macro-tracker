# Classic chat restore and voice removal

Date: 2026-10-10. Status: approved by owner in chat ("yes, go. Complete and
don't stop until you're finished").

## Owner intent (verbatim where it matters)

- "This is a really broken app. I don't like this Gen UI attempt. I want to
  go back to my old interface, and rip the voice mode out entirely."
- "I like the ability to ask the agent to switch my workout for the day etc."
- "Maybe add some features that you added to the new version to that older
  version to make it usable or more intuitive. If it seems overdoing it, then
  just leave it." A visual upgrade is explicitly deferred ("I'll do that later").
- Food logging: "use the FatSecret API ... If the FatSecret API did not have
  the food, then just use an Exa search to find the food. That's it." (This is
  the server's current two-layer chain; no change.)

## What "old interface" means

Commit `0dd3940` replaced a four-tab SwiftUI app -- **Workouts, Coach, Today,
Progress** (Coach default) -- with the Agent Canvas. The Coach tab was
`ChatLogView`: a message feed with bubbles, a composer, barcode/manual entry,
chat history restored from `GET /api/chat/history`, plus inline cards for a
metrics form and an exercise video, and a mic button that opened the voice
coach. It talked to the legacy `POST /api/chat`.

## Design

### iOS (branch `ui/classic-chat`; owner builds in Xcode, nothing merges to
`master` until it builds and is used)

1. **Root**: the four tabs return, same order and default, with the existing
   `ProgressDashboardView`, `WorkoutsView` (including the tomorrow-workout
   feature added 2026-10-09) and `TodayView` (its "Ask coach" action switches
   to the Coach tab).
2. **Coach tab = `CoachChatView`**, a plain chat backed by the agent the app
   already has: it POSTs to `/api/agent-canvas/{session}/turn` and shows the
   `reply` as a bubble. The only surviving "card" is an inline **Confirm /
   Cancel** row when the turn returns a pending `approval` (the server requires
   a confirm tap for workout-plan edits; that is what makes "switch my workout
   today" safe). It POSTs `{action: confirm|cancel, reference: id}` to
   `/api/agent-canvas/{session}/action` and shows that reply. Food questions
   are plain text; the one-question-per-meal rule is server-side and applies
   unchanged. If a turn's surfaces include a `ProfileMetrics` component (the
   server asked for measurements), the restored `MetricsFormCard` appears; on
   save it sends the values as a text message ("My metrics: ...") so the agent
   stages them through its normal set_metrics + Confirm path.
3. **Ported conveniences** (small, no redesign): quick-action chips above the
   composer ("What's left today?", "What's my workout?", "Log a meal",
   "Swap today's workout"); the typing indicator and cold-start retry the old
   view had; chat history restored on open. Not ported: surfaces, slots, the
   component catalog, the 2 s poll, receipt cards (the reply already states
   what was logged).
4. **Session identity**: one canvas session id per credential, stored in
   UserDefaults under `mmacros.chat.session.v1`, regenerated when the server
   reports `wrongSession`/an unknown session (HTTP 400 on the turn).
5. **Voice removed**: `ElevenLabsVoice.swift`, `LiveCoachController.swift`,
   `LiveCoachTransport.swift`, `LiveCoachAudioEngine.swift`, the voice button,
   `VoiceProviderPreference`, and the `ElevenLabs`, `LiveKitWebRTC`,
   `LiveKitUniFFI`, `A2UISwiftCore`, `A2UISwiftUI` package products and
   references are deleted from the project, with their tests
   (`ElevenLabsVoiceTests`, `LiveCoachControllerTests`) and the canvas tests
   and fixtures (`AgentCanvasContractTests`, `AgentCanvasIntegrationTests`,
   the `docs/agent-canvas/fixtures/*.json` resources).
6. **Canvas UI removed**: `AgentCanvasView.swift`, `AgentSurfaceRenderer.swift`,
   `AgentSurfaceStore.swift`, `AgentCanvasContract.swift`. `AppStore` loses the
   canvas store, the `accountRoute`/`AgentRootRoute` gate and the voice
   preference cache; `DayPayload` keeps its optional `canvasProtocol`/
   `voiceProvider` fields (decoding stays lenient).

### Server (same branch, separate commit; production keeps running
throughout)

7. **Voice entry points removed**: `/api/voice/elevenlabs/token`,
   `/api/voice/elevenlabs/agent-turn`, the `/api/agent-canvas/{session}/live`
   websocket route and its per-connection `LiveCoachService`, `elevenlabs_voice.py`,
   `scripts/elevenlabs_agent_setup.py`, the ElevenLabs config fields, and
   their tests. `live_coach.py` stays for now: `agent_canvas` imports the shared
   tool schemas from it and `server.py` uses `LiveSessionGate`; the class is
   dormant once its route is gone and can be deleted in a later cleanup.
8. **Unchanged**: the canvas turn/action/snapshot endpoints (the chat's
   backend), the two-layer food lookup (FatSecret then Exa), the one-question
   rule, the efficiency work, the daily purge.

## Invariants

- Every server query stays tenant-scoped; nothing in this change touches
  nutrition data or migrations.
- No new migration file (production applies migrations by an operator-run
  prestart step; a pending file takes the store down -- twice today).
- The ElevenLabs agent on the owner's account is left untouched (it shares the
  account with the BACP project).

## Out of scope

Visual redesign of the restored screens (owner: later). Deleting
`live_coach.py`. Removing the legacy `/api/chat` route (the web client uses it).
