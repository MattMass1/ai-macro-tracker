# Classic Chat Restore and Voice Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Agent Canvas with the pre-canvas four-tab app whose Coach tab is a plain chat driven by the existing agent turn endpoint, and remove voice from client and server.

**Architecture:** iOS gets three new focused files (`Models/CoachChat.swift`, `Services/CoachChatStore.swift`, `Views/CoachChatView.swift`) plus the restored `MetricsFormCard`; eight canvas/voice files and five package products are deleted from the Xcode project by hand-editing `project.pbxproj`. The server loses only voice entry points; the canvas turn/action/snapshot routes and the food chain are untouched.

**Tech Stack:** SwiftUI (iOS 17), hand-edited pbxproj (no synchronized groups), FastAPI/Starlette server with pytest (`uv` venv at the scratchpad path).

**Spec:** `docs/superpowers/specs/2026-10-10-classic-chat-restore-design.md`

## Global Constraints

- Branch `ui/classic-chat`; nothing merges to `master` until the owner builds in Xcode and uses it.
- Swift cannot be compiled in this environment while Xcode is open: every Swift task ends with `xcrun swiftc -parse` on the touched files and a grep proving no dangling references; the owner's Xcode build is the real gate.
- No new migration file. No change to food lookup, one-question rule, tenancy, or nutrition data.
- Server suite must stay green: `cd server && <venv>/bin/python -m pytest -q -p no:cacheprovider`.
- Design B palette and `Theme` helpers already in the app are reused as-is; no visual redesign.

## Review Focus

1. A turn that returns HTTP 429 (daily coach limit): the chat shows the limit message and keeps the user's text (Task 3 test: `send()` on 429 appends the limit bubble, no retry).
2. A pending approval the user ignores and then sends another message: the server rejects a new plan edit while one is pending; the chat must still show the approval row until confirm/cancel (Task 3: approval row persists across turns until resolved).
3. Server session no longer exists (new deploy wiped in-process sessions): the turn fails with 400 "unknown session"/`wrongSession`; the store must mint a fresh session id and retry once (Task 2 test).
4. History restore when the account has >100 messages: only the latest 100 load and the composer still works (Task 3: limit 100 request).
5. Voice removal must leave no symbol behind: a grep for `ElevenLabs|LiveCoach|AgentCanvas|AgentSurface|A2UI|VoiceProvider` across `ios/` must return only git history (Task 6 step).

---

### Task 1: Chat models

**Files:**
- Create: `ios/MacroTracker/Models/CoachChat.swift`

**Interfaces:**
- Produces: `CoachTurn` (`reply: String?`, `approval: CoachApproval?`, `wantsMetricsForm: Bool`), `CoachApproval` (`id, title, detail, status`), `CoachIntent` (`action: String, reference: String?`), `ChatHistoryMessage` (`role, content`), `ChatHistoryPayload`, `MetricsFieldKind`, `MetricsField`, `MetricsFieldValues` (copied from the pre-canvas `Models.swift` at `0dd3940^`).

- [ ] **Step 1: Write the file** (full content in the implementation; decoding is lenient: unknown keys ignored, `surfaces[].components[].component == "ProfileMetrics"` sets `wantsMetricsForm`).
- [ ] **Step 2: Parse check** `xcrun swiftc -parse ios/MacroTracker/Models/CoachChat.swift`
- [ ] **Step 3: Commit** `git add ios/MacroTracker/Models/CoachChat.swift && git commit -m "feat(ios): coach chat models for the classic chat"`

### Task 2: Chat store

**Files:**
- Create: `ios/MacroTracker/Services/CoachChatStore.swift`
- Modify: `ios/MacroTracker/Services/APIClient.swift` (replace the three canvas calls' return type with `CoachTurn`; add `chatHistory(limit:)`; delete `canvasSnapshotIfChanged`)

**Interfaces:**
- Consumes: `APIClient.canvasTurn(sessionId:turnId:message:) -> CoachTurn`, `canvasAction(sessionId:intent:) -> CoachTurn`, `chatHistory(limit:) -> [ChatHistoryMessage]`.
- Produces: `@MainActor final class CoachChatStore: ObservableObject` with `send(_ text: String) async throws -> CoachTurn`, `respond(approval id: String, confirm: Bool) async throws -> CoachTurn`, `history(limit:) async throws -> [ChatHistoryMessage]`, `reset()`; session id kept per credential scope in UserDefaults `mmacros.chat.session.v1`; a 400 whose message mentions "session" mints a new id and retries the turn once.

- [ ] **Step 1: Write the store and API changes.**
- [ ] **Step 2: Parse check** both files.
- [ ] **Step 3: Commit** `git commit -m "feat(ios): coach chat store on the agent turn endpoint"`

### Task 3: Chat view

**Files:**
- Create: `ios/MacroTracker/Views/CoachChatView.swift` (adapted from `ChatLogView` at `0dd3940^`: header without mic, bubbles, typing bubble, quick-action chips, approval row, `MetricsFormCard` when `wantsMetricsForm`, barcode/manual sheets, history on appear, cold-start retry, 429 handling)
- Create: `ios/MacroTracker/Views/Components/MetricsFormCard.swift` (verbatim from `0dd3940^`)

**Interfaces:**
- Consumes: `CoachChatStore`, `MetricsFormCard(fields:isSaving:onSave:onDismiss:)`, `ScanFoodSheet(onLogged:)`, `ManualFoodView()`, `FatSecretAttribution()`, `Theme.*`, `AppStore.loadDay()/loadWorkoutData()`.

- [ ] **Step 1: Write both files.**
- [ ] **Step 2: Parse check.**
- [ ] **Step 3: Commit** `git commit -m "feat(ios): classic coach chat view"`

### Task 4: Root and AppStore

**Files:**
- Modify: `ios/MacroTracker/ContentView.swift` (four-tab root from `0dd3940^`, keeping the current `ProgressDashboardView`; `TodayView(onAskCoach:)` switches to tab 1)
- Modify: `ios/MacroTracker/AppStore.swift` (drop `canvas`, `accountRoute`, `AgentRootRoute`, `VoiceProviderPreference`; keep `accountHasTargets` reads)

- [ ] **Step 1: Edit both files.**
- [ ] **Step 2: Parse check; grep `AgentRootRoute|store.canvas|AgentToastModifier` returns nothing outside deleted files.**
- [ ] **Step 3: Commit** `git commit -m "feat(ios): four-tab root returns; canvas store removed from AppStore"`

### Task 5: Delete canvas and voice files and tests

**Files:**
- Delete: `Views/AgentCanvasView.swift`, `Views/AgentSurfaceRenderer.swift`, `Services/AgentSurfaceStore.swift`, `Models/AgentCanvasContract.swift`, `Services/ElevenLabsVoice.swift`, `Services/LiveCoachController.swift`, `Services/LiveCoachTransport.swift`, `Services/LiveCoachAudioEngine.swift`, `MacroTrackerTests/ElevenLabsVoiceTests.swift`, `MacroTrackerTests/LiveCoachControllerTests.swift`, `MacroTrackerTests/AgentCanvasContractTests.swift`, `MacroTrackerTests/AgentCanvasIntegrationTests.swift`, `MacroTracker.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`

- [ ] **Step 1: `git rm` the files.**
- [ ] **Step 2: Commit** `git commit -m "chore(ios): remove the agent canvas UI and all voice code"`

### Task 6: Xcode project surgery

**Files:**
- Modify: `ios/MacroTracker.xcodeproj/project.pbxproj`

- [ ] **Step 1:** Remove every `PBXBuildFile`, `PBXFileReference`, group child, Sources/Resources/Frameworks phase entry for the deleted files, the fixture JSON resources, the five package products (`A2UISwiftCore`, `A2UISwiftUI` x2, `ElevenLabs`, `LiveKitWebRTC`, `LiveKitUniFFI`), both `packageProductDependencies` lists, the `packageReferences` list, and the `XCRemoteSwiftPackageReference` / `XCSwiftPackageProductDependency` sections.
- [ ] **Step 2:** Add build files, file references, group entries and Sources entries for `CoachChat.swift`, `CoachChatStore.swift`, `CoachChatView.swift`, `MetricsFormCard.swift` (ids `B004…/F004…`).
- [ ] **Step 3:** Validate: `plutil -lint` passes; grep for `ElevenLabs|LiveCoach|AgentCanvas|AgentSurface|A2UI|a2ui|livekit|agent-canvas/fixtures` in the pbxproj returns nothing; every `fileRef =` id resolves to a `PBXFileReference`.
- [ ] **Step 4: Commit** `git commit -m "chore(ios): project file without canvas/voice sources and packages"`

### Task 7: Server voice removal

**Files:**
- Modify: `server/src/server.py` (delete the two ElevenLabs routes, the `/live` websocket route and its `LiveCoachService` construction in `create_app`; drop `import elevenlabs_voice`)
- Modify: `server/src/config.py` (drop `voice_provider`, `elevenlabs_*`)
- Modify: `server/src/agent_canvas.py` (delete `canvas_live_start`, `canvas_tool_event`, `voice_handler` if unused after the route removal)
- Delete: `server/src/elevenlabs_voice.py`, `server/scripts/elevenlabs_agent_setup.py`, `server/tests/test_elevenlabs_voice.py`, `server/tests/test_agent_canvas_live.py`, `server/tests/test_live_release_integration.py`, `server/tests/live_route_smoke.py`, `server/tests/live_native_transport_fixture.py`
- Test: `server/tests/test_voice_removed.py` (routes return 404; `elevenlabs_voice` not importable; `config` has no voice fields)

- [ ] **Step 1: Write the failing test.**
- [ ] **Step 2: Run it: FAIL (routes exist).**
- [ ] **Step 3: Remove the code; fix any test still importing the deleted pieces (`day` payload `voiceProvider` stays).**
- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Commit** `git commit -m "feat(server): remove voice entry points (ElevenLabs routes, realtime socket, agent setup)"`

### Task 8: Docs, push, handoff

- [ ] Update `FLEET_CREED.md` design line (4 tabs; no voice), `docs/voice-canvas-handoff.md` header (superseded), memory.
- [ ] Push `ui/classic-chat`; tell the owner to build in Xcode and report errors; fix until it builds; then merge to `master` (server deploy follows).
