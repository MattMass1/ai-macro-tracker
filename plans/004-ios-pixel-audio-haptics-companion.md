# Plan 004: Add 8-Bit Chiptune Audio, Haptics, Interactive Companion, & Daily Quests HUD

> **Executor instructions**: Follow this plan step by step on macOS with Xcode after Plan 002 merges (independent of Plan 003, though great alongside it). Run every verification command and confirm the expected result before moving to the next step. Do NOT add AI attribution anywhere.
>
> **Drift check (run first)**:
> `git diff --stat f62416a..HEAD -- ios-macrobit/MacroBit/CompanionView.swift ios-macrobit/MacroBit/StudioLayout.swift ios-macrobit/MacroBit/CoachChatView.swift ios-macrobit/MacroBit/FoodViews.swift ios-macrobit/MacroBit/Components/PixelComponents.swift`

## Status

- **Repository**: `MattMass1/ai-macro-tracker` (`ios-macrobit/`)
- **Priority**: P2
- **Effort**: L
- **Risk**: LOW
- **Depends on**: `002-ios-8bit-visuals-and-buttons.md`
- **Category**: direction / interactivity / audio
- **Planned at**: commit `f62416a`, 2026-11-27

## Why this matters

Right now Macro-Bit has zero sound effects, only one haptic in the entire app (barcode capture), a completely passive companion sprite (`.rest` and `.writing` only—no reaction to taps or logged meals), and no self-improvement game feedback loop (no streaks, no daily quest checklist, no goal-reached fanfare). Adding a crisp, non-intrusive 8-bit chiptune SFX engine (that respects the iPhone silent switch and mixes politely with music/podcasts), paired with iOS 17 `.sensoryFeedback` haptics, reactive companion animations, and a derived Daily Quests + Streak HUD makes every logged meal feel like leveling up in a real 8-bit RPG.

## Current state

- `ios-macrobit/MacroBit/CompanionView.swift` — `CompanionState` (lines 31–35) only has `.rest` and `.writing`. `CompanionView` has no tap gesture, celebration bounce, or idle blink.
- `ios-macrobit/MacroBit/StudioLayout.swift` — `CompanionStage` (lines 79–87) only observes `chat.isSending ? .writing : .rest`.
- `ios-macrobit/MacroBit/FoodViews.swift` — `PixelBar` (lines 219–259) animates 16 blocks at 35ms/block with no audio tick, haptic tick, or 100%-goal celebration.
- `ios-macrobit/MacroBit/CoachChatView.swift` — `Coach menu` (lines 146–154) has no Sound FX or Haptics toggle; logging a meal triggers no companion celebration or floating `+30g P` popup.
- `ios-macrobit/MacroBit/Models.swift` — `DayPayload` (`totals`, `targets`, `meals`) and `TrendsPayload` (`days`, `weekly`) already provide all data needed to compute Daily Quests and Day Streaks client-side with **zero backend API changes**.

**Audio & Accessibility invariants (MUST honor):**
- Configure `AVAudioSession` with `.ambient` and `.mixWithOthers` — SFX **must** obey the hardware silent switch and **must never** pause Spotify, Apple Music, or podcasts.
- Never synthesize or play audio inside `XCTest` unless muted/disabled or cleanly guarded so simulator CI runs never hang on audio hardware.
- Every motion animation (`celebrating` bounce, floating `+30g P` popup, quest check pop) **must** respect `@Environment(\.accessibilityReduceMotion)`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Full Xcode build + tests | `xcodebuild clean test -project ios-macrobit/MacroBit.xcodeproj -scheme MacroBit -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO` | `TEST SUCCEEDED`, 0 failures |

## Scope

**In scope:**
- `ios-macrobit/MacroBit/PixelAudio.swift` (new file: procedural 8-bit `AVAudioEngine` SFX synthesizer + `PixelHaptics`)
- `ios-macrobit/MacroBit/GameProgress.swift` (new file: pure unit-testable `DailyQuest` & `StreakCalculator` derived from `DayPayload` and `TrendsPayload`)
- `ios-macrobit/MacroBit/CompanionView.swift`
- `ios-macrobit/MacroBit/StudioLayout.swift`
- `ios-macrobit/MacroBit/Components/PixelComponents.swift`
- `ios-macrobit/MacroBit/Theme.swift`
- `ios-macrobit/MacroBit/CoachChatView.swift`
- `ios-macrobit/MacroBit/FoodViews.swift`
- `ios-macrobit/MacroBit.xcodeproj/project.pbxproj` (register `PixelAudio.swift` and `GameProgress.swift`)
- `ios-macrobit/MacroBitTests/StudioResourceTests.swift`

**Out of scope:**
- Background looping music (keep sound strictly crisp, optional SFX for v1 so users don't mute the app).
- Backend XP/level storage or any API contract changes.

## Git workflow

- Branch: `app/macrobit-audio-haptics-game-loop` from `master` (after Plan 002 merges)
- Commit style: `Macro-Bit: add 8-bit chiptune SFX, haptics, interactive companion, and daily quests`
- No AI attribution anywhere.

## Steps

### Step 1: Build procedural 8-bit `PixelAudio` & `PixelHaptics` (`PixelAudio.swift`)

Create `ios-macrobit/MacroBit/PixelAudio.swift`:
1. `enum PixelSound: CaseIterable`:
   - `.tap` — 25ms soft square-wave blip (660 Hz, gain 0.12)
   - `.choice` — 30ms dual-step blip (523 → 659 Hz, gain 0.14)
   - `.tileLog` — 2-note retro coin pickup (988 Hz 55ms → 1319 Hz 110ms, gain 0.18)
   - `.confirmMeal` — 3-note RPG confirm chime (C5 → E5 → G5, 65ms each, gain 0.20)
   - `.goalHit` — 4-note 8-bit fanfare arpeggio (C5 → E5 → G5 → C6, gain 0.22)
   - `.barTick` — ultra-short 14ms quiet triangle pip (pitch scalable by block index `0..<16`, gain 0.06)
   - `.companionChirp` — playful 2-note chirp when tapping the coach (784 → 1046 Hz, gain 0.14)
   - `.error` — 2-step low square buzz (220 → 165 Hz, 70ms each, gain 0.16)
2. `enum PixelWaveform` (pure, unit-testable PCM buffer generator):
   - Generates finite `Float` sample arrays at 44.1 kHz with 4ms linear attack/release envelopes (prevents audio clicks/pops) for square and triangle waveforms.
3. `@MainActor final class PixelAudio`:
   - `static let shared = PixelAudio()`
   - Respects `@AppStorage("audio.sfxEnabled")` (default `true`) and skips engine start when running under `XCTestConfigurationFilePath` or `-fixture-ui` so tests remain deterministic and instant.
   - Configures `try? AVAudioSession.sharedInstance().setCategory(.ambient, options: .mixWithOthers)` lazily on first sound play, attaches a small pool of 3 `AVAudioPlayerNode`s to `AVAudioEngine` so rapid button taps never cut each other off, and pre-caches all `AVAudioPCMBuffer`s in memory (<100 KB total).
   - Handles `AVAudioEngineConfigurationChange` safely (restarts lazily on next play instead of crashing).
4. `@MainActor enum PixelHaptics`:
   - Respects `@AppStorage("audio.hapticsEnabled")` (default `true`).
   - Methods: `tap()` (`.light` impact), `select()` (`UISelectionFeedbackGenerator`), `success()` (`.success` notification), `goalHit()` (`.heavy` + `.success`), `error()` (`.error` notification).

**Verify**: Build & run `MacroBitTests` → `TEST SUCCEEDED`.

### Step 2: Wire audio & haptics into buttons, tabs, bars, and meal saves

1. Add `"Sound FX"` and `"Haptics"` `Toggle` items inside `CoachChatView`'s `Coach menu` (lines 146–154).
2. Trigger `PixelAudio.shared.play(.tap)` + `PixelHaptics.tap()` on:
   - `PixelPrimaryButtonStyle`, `PixelSecondaryButtonStyle`, `PixelActionStyle` press-down (using `.onChange(of: configuration.isPressed)` when transitioning `false -> true`).
   - `PixelChoice`, `PixelToggle`, `StudioTabBar` tab switch (`play(.choice)` + `PixelHaptics.select()`).
3. Trigger `PixelAudio.shared.play(.tileLog)` + `PixelHaptics.success()` when `logFromHome` or `PixelPresetGrid.quickLog` succeeds.
4. Trigger `PixelAudio.shared.play(.confirmMeal)` + `PixelHaptics.success()` when a `mealReceipt` surface arrives in `AppStore.perform`, upgrading to `.goalHit` + `PixelHaptics.goalHit()` if `day.totals.protein >= day.targets.protein` just crossed 100% for the first time today!
5. Trigger `PixelAudio.shared.play(.error)` + `PixelHaptics.error()` on `OnboardingView` validation errors and `saveFailed` surfaces.
6. In `PixelBar`: optionally play quiet `.barTick` every 3rd climbing block (throttled so it sounds like a crisp retro counter without buzzing).

**Verify**: Run `MacroBitTests` → `TEST SUCCEEDED`.

### Step 3: Make `CompanionView` & `CompanionStage` interactive and reactive

In `ios-macrobit/MacroBit/CompanionView.swift` and `StudioLayout.swift`:
1. Expand `CompanionState`:
   ```swift
   enum CompanionState: Equatable {
       case rest
       case writing
       case celebrating
   }
   ```
2. When `state == .celebrating` (and `!holdStill`):
   - Play a crisp 2-step pixel jump (`offset(y: -6)` → `0` on a discrete 120ms stepped timeline, never mushy spring blur) plus pixel sparkle accent overlay for 1.2 seconds, then automatically settle back to `.rest`.
3. Add a floating pixel delta popup (`"+30g P · +150 kcal"`) above `CompanionStage` whenever a meal is logged (via an `@Published var lastReward: MealRewardBanner?` on `AppStore` that auto-clears after 2s; respects `reduceMotion` by showing static fade instead of rise).
4. Add `.onTapGesture` on `CompanionStage` in `CoachChatView`:
   - Plays `PixelAudio.shared.play(.companionChirp)` + `PixelHaptics.tap()`, triggers a quick bounce, and briefly shows a tiny 8-bit speech bubble above the coach's head (e.g., `"42g protein left today!"` when targets exist, or `"Tell me what you ate!"` when empty).

**Verify**: Run `MacroBitTests` and `StudioUITests` → all pass.

### Step 4: Add Derived Daily Quests & Day Streak HUD (`GameProgress.swift`)

Create `ios-macrobit/MacroBit/GameProgress.swift` (pure, zero backend changes):
1. `struct DailyQuest: Identifiable, Equatable { let id: String; let title: String; let complete: Bool; let progressText: String }`
2. `enum GameProgress`:
   - `static func dailyQuests(day: DayPayload) -> [DailyQuest]`:
     - **First Fuel**: `day.meals.count >= 1` (`"Log your first meal"`)
     - **Protein Target**: `day.targets.protein > 0 && day.totals.protein >= day.targets.protein` (`"\(Int(day.totals.protein))/\(Int(day.targets.protein))g"`)
     - **Calorie Zone**: `day.targets.calories > 0 && day.totals.calories >= day.targets.calories * 0.85 && day.totals.calories <= day.targets.calories * 1.10` (`"Within 10% of goal"`)
     - **Fiber Boost**: `day.targets.fiber > 0 && day.totals.fiber >= day.targets.fiber` (`"\(Int(day.totals.fiber))/\(Int(day.targets.fiber))g"`)
   - `static func currentStreak(days: [TrendDay], todayKey: String) -> Int`: computes consecutive logged days ending on `todayKey` or yesterday.
3. Render a compact **Daily Quests** `PixelPanel` inside `TodayView` (right below `PixelMacroSummary`, with `PixelGlyphView(glyph: .check)` for completed quests) and a **Streak pixel pill** (`"🔥 5D"` or pixel flame + `"5 DAY STREAK"`) in `ProgressViewScreen` header when `streak >= 1`.

**Verify**: Full `xcodebuild clean test` → `TEST SUCCEEDED`, 0 failures.

## Test plan

Add unit tests in `ios-macrobit/MacroBitTests/StudioResourceTests.swift`:
- `testPixelWaveformProducesFiniteBoundedSamples` — verify every `PixelSound` case generates non-empty finite PCM samples in `[-1.0, 1.0]` with zero leading/trailing clicks.
- `testDailyQuestsEvaluateCorrectlyFromDayPayload` — empty day (0/4 complete), partial day (1/4), target-hit day (4/4), zero-target fallback day (never false-completes protein/calorie/fiber quests).
- `testStreakCalculatorHandlesConsecutiveAndBrokenDays` — 0 days, today only (1), yesterday + prior 2 days (3), gap breaks streak.
- `testCompanionCelebratingRespectsReduceMotion` — `holdStill: true` renders grounded rest pose.

## Done criteria

- [ ] `xcodebuild clean test` passes all unit and UI tests with 0 failures (and never hangs on simulator audio).
- [ ] Sound FX uses `.ambient` + `.mixWithOthers` (verified via code inspection).
- [ ] Sound FX and Haptics toggles exist in `Coach menu` and persist via `@AppStorage`.
- [ ] Tapping `CompanionStage` plays chirp + shows brief status bubble; logging a meal triggers celebration + floating macro reward.
- [ ] Daily Quests and Day Streak unit tests pass.

## STOP conditions

- Stop if `AVAudioEngine` initialization causes simulator test timeouts — ensure `PixelAudio` short-circuits when `ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil`.
- Stop if adding `.onTapGesture` on `CompanionStage` interferes with `ScrollView` touch handling or `StudioUITests` element queries.

## Maintenance notes

- Procedural chiptune synthesis avoids binary asset bloat and licensing ambiguity; if custom `.caf` sound files are recorded later, swap only `PixelAudio.buffer(for:)` while keeping all call sites identical.
- If persistent XP/levels are later added to `mmmacros-backend`, `GameProgress` is the single extension point to bind server XP alongside client-derived quests.
