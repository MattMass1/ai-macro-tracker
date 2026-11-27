# Plan 001: Simplify Macro-Bit Onboarding & Fix Client Reliability Glitches

> **Executor instructions**: Follow this plan step by step on macOS with Xcode. Run every verification command and confirm the expected result before moving to the next step. If anything in the "STOP conditions" section occurs, stop and report — do not improvise. Do NOT add AI attribution anywhere in code, comments, commit messages, or PRs.
>
> **Drift check (run first)**:
> `git diff --stat f62416a..HEAD -- ios-macrobit/MacroBit/MacroBitApp.swift ios-macrobit/MacroBit/AppStore.swift ios-macrobit/MacroBit/Onboarding.swift ios-macrobit/MacroBit/FoodViews.swift ios-macrobit/MacroBit/CoachChatView.swift ios-macrobit/MacroBit/ChatJournal.swift`
> If any in-scope file changed since this plan was written, compare the "Current state" excerpts against live code before proceeding; on a mismatch, STOP and report.

## Status

- **Repository**: `MattMass1/ai-macro-tracker` (`ios-macrobit/`)
- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug / ux
- **Planned at**: commit `f62416a`, 2026-11-27

## Why this matters

New players currently hit four jarring transitions before playing: Sign-In (light mode) → an accidental flash of the main Studio screen (because `isSignedIn = true` fires before `await checkSetup()` resolves) → Dark Onboarding Step 0 (which repeats the Macro-Bit wordmark, Companion sprite, and `"About you"` heading) → Dark Onboarding Step 1 (which repeats `"Your plan"` and stacks 8 full-width buttons requiring multiple swipes to reach Save) → Light Studio. At the same time, four concrete client bugs degrade daily logging: `CoachChatView` silently swallows `store.errorMessage` and `store.isMutating` when tapping home saved-meal buttons, `PixelMacroSummary` renders `"0 OVER"` whenever over the calorie target, foreground resume skips `loadPresets()`, and `ChatJournal` drops rich `CoachSurface` cards on relaunch.

## Current state

- `ios-macrobit/MacroBit/AppStore.swift` — state store; sets `isSignedIn = true` before `await checkSetup()` in both `restore()` (lines 124–128) and `signIn(_ raw:)` (lines 142–144):
  ```swift
  // AppStore.swift:124-128
  func restore() async {
      guard vault.read() != nil else { return }
      await refreshToday()
      if day != nil { isSignedIn = true; restoreChat(); await checkSetup(); await loadPresets(); await loadSuggestions() }
  }
  ```
  ```swift
  // AppStore.swift:142-144
  authGeneration = UUID(); day = payload; isSignedIn = true
  journal.clear(); startFreshChat(day: chatDayKey)
  await checkSetup(); await loadPresets(); await loadSuggestions()
  ```
- `ios-macrobit/MacroBit/MacroBitApp.swift` — root routing (line 26), foreground refresh (lines 28–30), hardcoded `.male` in `TokenView` (line 41) and `WorkoutsView` (line 109):
  ```swift
  // MacroBitApp.swift:25-30
  if !store.isSignedIn { TokenView() } else if store.needsSetup { OnboardingView() } else { ContentView() }
  ...
  if phase == .active && store.isSignedIn { Task { await store.refreshToday(); store.rollChatIfNewDay(); await store.loadSuggestions() } }
  ```
- `ios-macrobit/MacroBit/Onboarding.swift` — 2-step dark setup wizard (lines 54–169). `answers` (lines 62–66) does not bound `inches` to `0..<12`, never passes per-field `validation: .invalid(...)` to `PixelValueField`, repeats `PixelSelectedArtwork()` (line 74) and `"About you"` / `"Your plan"` headers (lines 80–90, 115), stacks 5 Activity + 3 Goal choices vertically (lines 130–143), omits Companion character selection, and `prefill()` (line 108) only runs once on `.onAppear`.
- `ios-macrobit/MacroBit/FoodViews.swift` — `PixelMacroSummary` (lines 277–281) clamps over-target remaining calories to `0`:
  ```swift
  Text(whole(max(0, day.remaining.calories))).font(Theme.pixelFont(24))
  Text(day.totals.calories > day.targets.calories && day.targets.calories > 0 ? "OVER" : "LEFT")
  ```
  `TodayView` `logged` state (lines 12, 28, 67) never clears after logging.
- `ios-macrobit/MacroBit/CoachChatView.swift` — `logFromHome` (lines 234–240) never shows `store.errorMessage` or `store.isMutating`; `homeLogged` (line 14) never clears; `coachHeader` has asymmetric `.padding(.leading, 64)` (line 154).
- `ios-macrobit/MacroBit/ChatJournal.swift` — `SavedChat.Line` only persists `fromUser: Bool` and `text: String`, discarding `surfaces` across relaunch.

**Repo conventions & invariants (MUST preserve):**
- Keep `checkSetup()` non-blocking on network error (`needsSetup = false` on catch — tested in `ContractTests`).
- Keep `APIClient` routes, headers (`X-Nutrient-Contract: nullable-v1`), and `Models.swift` / `CoachChat.swift` decoding rules intact.
- Keep existing accessibility identifiers used by `StudioUITests`: `sign-in-with-apple`, `setup-feet`, `setup-inches`, `setup-pounds`, `setup-age`, `setup-next`, `setup-save`, `setup-back`, `setup-cancel`, `setup-error`, `targets-current`, `setup-activity-*`, `setup-goal-*`, `home-preset-*`, `home-preset-logged`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Build + Unit + UI tests | `xcodebuild clean test -project ios-macrobit/MacroBit.xcodeproj -scheme MacroBit -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO` | `TEST SUCCEEDED`, 0 failures |
| Unit tests only (fast) | `xcodebuild test -project ios-macrobit/MacroBit.xcodeproj -scheme MacroBit -only-testing:MacroBitTests -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO` | `TEST SUCCEEDED`, 0 failures |

*(Replace `iPhone 17 Pro` with an installed simulator from `xcrun simctl list devices available`.)*

## Scope

**In scope:**
- `ios-macrobit/MacroBit/AppStore.swift`
- `ios-macrobit/MacroBit/MacroBitApp.swift`
- `ios-macrobit/MacroBit/Onboarding.swift`
- `ios-macrobit/MacroBit/FoodViews.swift`
- `ios-macrobit/MacroBit/CoachChatView.swift`
- `ios-macrobit/MacroBit/ChatJournal.swift`
- `ios-macrobit/MacroBitTests/ContractTests.swift`
- `ios-macrobit/MacroBitUITests/StudioUITests.swift`

**Out of scope:**
- `ios/` (legacy MacroTracker app) — never touch.
- `ios-macrobit/MacroBit/APIClient.swift` — no API contract changes.
- `ios-macrobit/MacroBit/Components/PixelComponents.swift` and `Theme.swift` — handled in Plan 002.

## Git workflow

- Branch: `app/macrobit-onboarding-reliability` from `master`
- Commit style: match repo (`Macro-Bit: simplify onboarding and fix client feedback bugs`)
- No AI attribution in code, comments, or commit messages.

## Steps

### Step 1: Eliminate the onboarding flash race on sign-in and app restore

In `ios-macrobit/MacroBit/AppStore.swift`:
1. Add `@Published var isRestoring = false`.
2. In `restore()`:
   - Set `isRestoring = true; defer { isRestoring = false }`.
   - Fetch `await refreshToday()`. If `day != nil`, run `await checkSetup()` **before** setting `isSignedIn = true`, then call `restoreChat()`, `await loadPresets()`, `await loadSuggestions()`.
3. In `signIn(_ raw:)`:
   - After `vault.save(token)` succeeds and `api` + `authGeneration` + `day` are set, call `await checkSetup()` **before** setting `isSignedIn = true`, then call `journal.clear()`, `startFreshChat(day: chatDayKey)`, `await loadPresets()`, `await loadSuggestions()`.
4. In `MacroBitApp.swift`:
   - In `RootView`, while `store.isRestoring && !store.isSignedIn`, show a minimal Studio splash with `PixelActivity(state: .busy("Loading…"))` so returning users don't see `TokenView` flash either.
   - In `RootView`'s `.onChange(of: scenePhase)` when `phase == .active && store.isSignedIn`, add `await store.loadPresets()` alongside `refreshToday()` and `loadSuggestions()`.
   - In `TokenView` and `WorkoutsView`, replace hardcoded `.male` with `@AppStorage(CompanionPreferences.characterKey) private var characterRaw = CompanionCharacter.male.rawValue` resolved via `CompanionCharacter(rawValue: characterRaw) ?? .male`.

**Verify**: Run unit tests (`-only-testing:MacroBitTests`) → `TEST SUCCEEDED`.

### Step 2: Simplify `OnboardingView` into a tight Character Select + Quest flow

In `ios-macrobit/MacroBit/Onboarding.swift`:
1. Add `@AppStorage(CompanionPreferences.characterKey) private var characterRaw = CompanionCharacter.male.rawValue` so players choose their 8-bit coach (`Male coach` / `Female coach`) right inside Step 0 with live `CompanionView` preview on the pedestal.
2. Remove the duplicate headers:
   - Do **not** repeat `"About you"` in both `PixelStepProgress` and `PixelPanel(title:)`; pass `title: nil` on the outer panel when `PixelStepProgress` already labels the step.
   - Do **not** repeat `PixelSelectedArtwork()`'s giant wordmark when the player just came from `TokenView`; use a compact character-select header showing the live `CompanionView` (96×96) + compact toggle buttons for the two characters.
3. Tighten Step 1 (`plan`) so **both Activity and Goal fit comfortably without multi-swipe scrolling**:
   - Keep `ProfileAnswers.Activity` accessibility IDs (`setup-activity-*`) and `ProfileAnswers.Goal` IDs (`setup-goal-*`).
   - Use concise 1-line button titles for Activity (e.g., `"Sedentary · Mostly sitting"`, `"Light · 1–3x/wk"`, `"Moderate · 3–5x/wk"`, `"Active · 6–7x/wk"`, `"Very active · Hard training"`) with compact vertical padding (`padding(.vertical, 8)`), and lay out the 3 Goal choices (`Lose`, `Maintain`, `Gain`) in an `HStack` (falling back to `VStack` at `dynamicTypeSize.isAccessibilitySize`).
   - Show a live **Estimated Daily Targets** preview strip (computed client-side only as preview or keeping the exact server save) — or keep targets strictly server-calculated on save and clearly label `"Set my targets"`. *(Do not invent a divergent formula; let the server remain authoritative on save.)*
4. Fix validation and `editing` mode:
   - In `answers`, require `(0..<12).contains(i)` for inches.
   - Add per-field `PixelFieldValidation` (`feetError`, `inchesError`, `poundsError`, `ageError`) and pass `validation:` into each `PixelValueField` (already supported by `PixelComponents.swift:258`).
   - When `editing == true` (`"My targets"`), render a **single scrollable screen** with Current Targets + About You + Activity + Goal + `"Update my targets"` / `"Cancel"` (no forced 2-step Next/Back wizard to edit one number).
   - Add `.onChange(of: store.profile) { _, _ in prefill() }` so prefill succeeds even if `store.profile` finishes loading after `.onAppear`.

**Verify**: Run `xcodebuild test ... -only-testing:MacroBitUITests/StudioUITests/testSetupWalksBothStepsAndEntersStudio` and `testMyTargetsOpensPrefilledFromTheMenuAndCancelReturns` → both pass.

### Step 3: Fix `"0 OVER"` calorie bug, silent home errors, and auto-clearing logged badges

1. In `ios-macrobit/MacroBit/FoodViews.swift` (`PixelMacroSummary`, lines 277–281):
   - Replace `Text(whole(max(0, day.remaining.calories)))` with:
     ```swift
     let isOver = day.totals.calories > day.targets.calories && day.targets.calories > 0
     let delta = isOver ? (day.totals.calories - day.targets.calories) : max(0, day.remaining.calories)
     Text(whole(delta)).font(Theme.pixelFont(24)).foregroundStyle(isOver ? Theme.danger : Theme.ink)
     Text(isOver ? "OVER" : "LEFT").font(Theme.pixelFont(14)).foregroundStyle(isOver ? Theme.danger : Theme.muted)
     ```
2. In `ios-macrobit/MacroBit/CoachChatView.swift`:
   - In `coachHeader` (line 154), change `.padding(.leading, 64).padding(.trailing, 24)` to `.padding(.horizontal, 20)` so the title aligns cleanly with the `20pt` Studio card below.
   - Surface `store.errorMessage` inside the Studio panel (top of the `ScrollView` or right above `composer`) as a dismissible error banner with `"Dismiss"` so failed `logFromHome` or `refreshToday` calls are never silent.
   - In `savedMealStrip`, show `"Logging…"` when `store.isMutating` is true, and auto-clear `homeLogged` after 2.5 seconds (using a cancellable `Task` stored in `@State private var clearLoggedTask: Task<Void, Never>?`) so logging the same preset twice gives fresh visual confirmation every time. Apply the same 2.5s auto-clear to `TodayView.logged` in `FoodViews.swift`.

**Verify**: Run unit tests (`-only-testing:MacroBitTests`) → `TEST SUCCEEDED`.

### Step 4: Persist `surfaces` JSON in `ChatJournal` across same-day relaunch

In `ios-macrobit/MacroBit/ChatJournal.swift` and `ios-macrobit/MacroBit/AppStore.swift`:
1. Add optional `surfacesJSON: Data? = nil` to `SavedChat.Line` (default `nil` keeps backward compatibility with existing on-disk JSON files).
2. In `AppStore.perform`, store raw surface JSON bytes (or make `CoachSurface` `Codable` by adding `Encodable` conformance in `CoachChat.swift` — if touching `CoachChat.swift`, keep all existing `Decodable` rules byte-for-byte identical).
3. In `AppStore.restoreChat()`, decode `surfaces` back into `ChatMessage.surfaces` (never restoring stale `quickReplies` so old `"Log it"` buttons cannot be re-tapped after relaunch).

**Verify**: Full test suite (`xcodebuild clean test ...`) → `TEST SUCCEEDED`.

## Test plan

Add unit tests in `ios-macrobit/MacroBitTests/ContractTests.swift`:
- `testSignInChecksSetupBeforeMarkingSignedIn` — assert `isSignedIn` is still `false` while `/api/profile` is in flight, and when `/api/profile` returns `needs_setup: true`, both `isSignedIn` and `needsSetup` become `true` together (no Studio flash).
- `testProfileAnswersRejectsOutOfRangeInches` — `inches = 12` and `inches = -1` return invalid.
- `testPixelMacroSummaryOverageDelta` — extract a pure helper `PixelMeter.calorieDelta(totals:targets:remaining:)` and test under-target (`200 LEFT`), exact (`0 LEFT`), and over-target (`350 OVER`, never `0 OVER`).
- `testChatJournalPreservesSurfacesWithoutRestoringStaleQuickReplies` — save and reload a `meal_receipt` and `day_totals` turn.

Update `StudioUITests.swift`:
- Verify `testSetupWalksBothStepsAndEntersStudio` works cleanly without needing extra `swipeUp()` calls on Step 2, and test companion character selection on Step 0.

## Done criteria

- [ ] `xcodebuild clean test -project ios-macrobit/MacroBit.xcodeproj -scheme MacroBit ...` passes with 0 failures.
- [ ] New user sign-in with `needs_setup: true` transitions directly from `TokenView` to `OnboardingView` with zero flash of `ContentView`.
- [ ] Over-target calories display exact positive overage with `"OVER"` in `Theme.danger`, never `"0 OVER"`.
- [ ] `CoachChatView` displays `store.errorMessage` on failed home preset tap and auto-clears `homeLogged` after 2.5s.
- [ ] No files outside the In-Scope list are modified (`git status`).

## STOP conditions

- Stop if `ContractTests` expects `isSignedIn == true` synchronously before `checkSetup()` completes — inspect the test and report before changing test assertions.
- Stop if adding `Encodable` to `CoachSurface` alters any `Decodable` contract test in `ContractTests.swift`.
- Stop if any existing `StudioUITests` accessibility identifier breaks and cannot be preserved alongside the compact layout.

## Maintenance notes

- Plan 002 builds directly on the cleaned-up `OnboardingView`, `CoachChatView`, and `FoodViews` structure from this plan — land Plan 001 first.
- Keep `checkSetup()`'s error fallback (`needsSetup = false`) intact unless backend availability guarantees change.
