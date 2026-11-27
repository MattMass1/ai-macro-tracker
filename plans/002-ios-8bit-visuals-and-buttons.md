# Plan 002: Unify 8-Bit Visual System, Tactile Pixel Buttons, & Studio HUD

> **Executor instructions**: Follow this plan step by step on macOS with Xcode after Plan 001 merges. Run every verification command and confirm the expected result before moving to the next step. If anything in the "STOP conditions" section occurs, stop and report — do not improvise. Do NOT add AI attribution anywhere.
>
> **Drift check (run first)**:
> `git diff --stat f62416a..HEAD -- ios-macrobit/MacroBit/Theme.swift ios-macrobit/MacroBit/Components/PixelComponents.swift ios-macrobit/MacroBit/StudioLayout.swift ios-macrobit/MacroBit/CoachChatView.swift ios-macrobit/MacroBit/FoodViews.swift ios-macrobit/MacroBit/BarcodeScannerView.swift`

## Status

- **Repository**: `MattMass1/ai-macro-tracker` (`ios-macrobit/`)
- **Priority**: P1
- **Effort**: L
- **Risk**: LOW
- **Depends on**: `001-ios-onboarding-and-reliability.md`
- **Category**: direction / tech-debt / ui
- **Planned at**: commit `f62416a`, 2026-11-27

## Why this matters

Macro-Bit currently runs two incompatible visual languages side by side: PR #27 brought in rich 3D beveled pixel controls (`PixelBevel`, 3pt inset `PixelActionStyle`, `PixelActivity`, `PixelValueField`, custom pixel SVGs) in dark emerald exclusively for `OnboardingView`, while the rest of the app (where players spend 99% of their time) falls back to flat light buttons (`PixelPrimaryButtonStyle` with an unclipped bottom `Rectangle` that bleeds past the stepped border and only dims opacity on press), smooth iOS `RoundedRectangle` chat bubbles, smooth `Capsule()` quick replies, stock `ProgressView` spinners, stock UIKit `Form {}` sheets, SF Symbols in the tab bar, and stock Swift Charts. Unifying onto one parameterized 8-bit component library turns every screen, button, and meter into a cohesive retro game UI.

## Current state

- `ios-macrobit/MacroBit/Theme.swift` — defines two separate palettes (`profile*` dark emerald lines 8–17 vs. light sage lines 18–45), `PixelPanelShape` (lines 48–69), flat `PixelPrimaryButtonStyle` with unclipped bottom `Rectangle` (lines 81–93), and flat `PixelSecondaryButtonStyle` (lines 96–107).
- `ios-macrobit/MacroBit/Components/PixelComponents.swift` — defines `PixelBevel` (lines 79–107), `PixelActionStyle` (lines 293–302), `PixelActivity` (lines 336–359), `PixelValueField` (lines 250–291), `PixelChoice`, `PixelToggle`, `PixelDisclosure`, plus unused dead code `PixelNativeNavigation` (lines 173–213).
- `ios-macrobit/MacroBit/CoachChatView.swift` — uses smooth `RoundedRectangle` `CoachBubble` (lines 432–446), smooth `Capsule()` `QuickReplyRow` (lines 448–465), stock `ProgressView` (line 74), and mixes `"Today"` tab with a modal `TodayView` sheet also titled `"Today"`.
- `ios-macrobit/MacroBit/FoodViews.swift` — `PixelBar` uses flat single-color `Rectangle` blocks (lines 219–259); `ManualEntryView`, `SavedMealEditor`, and preset customization sheet use stock iOS `Form {}` (lines 93–163, 390–433); `ProgressViewScreen` uses `.largeTitle.bold()`, `.pickerStyle(.segmented)`, and stock Swift Charts `BarMark` (lines 165–203).
- `ios-macrobit/MacroBit/StudioLayout.swift` — `StudioTabBar` uses SF Symbols (`house`, `dumbbell`, `chart.line.uptrend.xyaxis`), flat highlight, and skipped tab index `1` (`0, 2, 3`) (lines 46–75).
- `ios-macrobit/MacroBit/BarcodeScannerView.swift` — uses smooth `RoundedRectangle(cornerRadius: 18)` viewfinder and `.ultraThinMaterial` circles (lines 71–124).

**Design constraints to honor:**
- Typography: `Jersey15-Regular` (`Theme.pixelFont(...)`) for headings, counters, badges, and buttons; native system font for readable paragraphs, food names, and editable text inputs (matching `Theme.swift:4-6` and `STYLE-GUIDE.md`).
- Accessibility: All controls retain ≥44×44pt hit targets, Dynamic Type scaling (`isAccessibilitySize` vertical fallbacks), VoiceOver labels/identifiers, and `accessibilityReduceMotion` checks.
- Pixel rendering: Always use `.interpolation(.none).antialiased(false)` on raster pixel assets.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Full build + unit + UI tests | `xcodebuild clean test -project ios-macrobit/MacroBit.xcodeproj -scheme MacroBit -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO` | `TEST SUCCEEDED`, 0 failures |
| Check dead nav removal | `grep -rn "PixelNativeNavigation" ios-macrobit/` | no matches |

## Scope

**In scope:**
- `ios-macrobit/MacroBit/Theme.swift`
- `ios-macrobit/MacroBit/Components/PixelComponents.swift`
- `ios-macrobit/MacroBit/StudioLayout.swift`
- `ios-macrobit/MacroBit/CoachChatView.swift`
- `ios-macrobit/MacroBit/FoodViews.swift`
- `ios-macrobit/MacroBit/BarcodeScannerView.swift`
- `ios-macrobit/MacroBit/MacroBitApp.swift` (`WorkoutsView` pixel typography only)
- `ios-macrobit/MacroBit/Resources/Assets.xcassets/` (add 4 pixel SVG tab/UI icons: `PixelHome`, `PixelDumbbell`, `PixelChart`, `PixelBarcode`, `PixelMenu`, `PixelTrash`)
- `ios-macrobit/MacroBitTests/StudioResourceTests.swift`

**Out of scope:**
- Food sprite pack (`design/whole-food-sprites-v1`) — handled in Plan 003.
- Sound engine & haptics — handled in Plan 004.
- `APIClient.swift`, `Models.swift`, `CoachChat.swift`, `AppStore.swift` business logic.

## Git workflow

- Branch: `app/macrobit-pixel-ui-unification` from `master` (after Plan 001 merges)
- Commit style: `Macro-Bit: unify 8-bit beveled buttons, dialogue boxes, and HUD`
- No AI attribution anywhere.

## Steps

### Step 1: Parameterize `PixelBevel` for both Studio (light sage) and Emerald (dark) surfaces & remove dead code

In `ios-macrobit/MacroBit/Components/PixelComponents.swift` and `Theme.swift`:
1. Delete unused `PixelNativeNavigation` (`PixelComponents.swift:173-213`).
2. Add a `PixelPalette` struct (or `enum PixelSurfaceStyle { case studioLight, studioAccent, studioDark, emeraldDark }`) supplying `{ face, highlightTop, shadeBottom, outline, shadow, ink }`:
   - `studioLight`: cream face (`Theme.surface`), bright cream top highlight (`#FFFFFF` 0.75), sage bottom shade (`Theme.edge`), forest outline (`Theme.accentEdge`), hard pixel drop shadow (`Theme.accentEdge.opacity(0.45)`).
   - `studioAccent`: forest green face (`Theme.accent`), moss top highlight (`#3A8B63`), deep forest bottom lip (`Theme.accentDark`), outline (`Theme.accentDark`), white ink.
   - `studioDark` / `emeraldDark`: existing `Theme.profile*` tokens so `OnboardingView` and `PixelComponentGallery` stay crisp (or opt into the unified palette so Onboarding matches Studio seamlessly — **recommended:** keep dark emerald optional via style token and use a shared deep-sage/cream game palette so Sign-In → Onboarding → Studio feels like one world).
3. Update `PixelBevel` to take `style: PixelBevelStyle` (preserving existing default parameters so existing call sites don't break) and ensure every inner highlight/shade overlay is clipped inside `PixelPanelShape()`.
4. Upgrade `PixelPrimaryButtonStyle`, `PixelSecondaryButtonStyle`, and `PixelTileButtonStyle` in `Theme.swift` / `FoodViews.swift`:
   - Replace the unclipped `Rectangle().fill(Theme.accentDark)` in `PixelPrimaryButtonStyle` with `PixelBevel(..., inset: configuration.isPressed)` + `.offset(y: configuration.isPressed ? 2 : 0)` + `.animation(.linear(duration: 0.05), value: configuration.isPressed)`.
   - Give `PixelSecondaryButtonStyle` the exact same 3D pixel bevel press feel (using moss-tinted face + 2pt downward press shift).
   - Add a `PixelChipButtonStyle` for compact quick-reply and action chips (beveled 8-bit capsule replacement, 44pt min touch height).

**Verify**: `xcodebuild test ... -only-testing:MacroBitTests` → `TEST SUCCEEDED`.

### Step 2: Upgrade `CoachChatView` to authentic 8-bit RPG dialogue & HUD

In `ios-macrobit/MacroBit/CoachChatView.swift`:
1. Replace `CoachBubble`'s `RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)` with `PixelBevel` (`PixelPanelShape()`):
   - Coach bubbles: cream RPG dialogue box with a 2px forest pixel frame and subtle left pixel accent bar.
   - User bubbles: moss-tinted (`Theme.accentTint`) stepped pixel box aligned right.
2. Replace `QuickReplyRow`'s `Capsule()` pills with `PixelChipButtonStyle` (primary beveled green for index `0`, secondary beveled moss for remaining choices) using `Theme.pixelFont(19)`.
3. Replace stock `ProgressView("Writing. First connection can take about a minute.")` (line 74) with an inline `HStack` containing `PixelActivity(state: .busy("Writing…"))` + readable status copy inside a pixel strip.
4. Show composed coach prose above/beside `ChatSurfaceCard` if `message.text` contains extra non-duplicate guidance (keep card as primary surface without duplicating raw receipt strings).

**Verify**: Run `StudioUITests` chat preview/receipt/totals tests → all pass.

### Step 3: Detail `PixelBar` & pixel-style `ProgressViewScreen`, `WorkoutsView`, `BarcodeScannerView`, and forms

1. **`PixelBar` (`FoodViews.swift`)**:
   - Upgrade each lit segment from a flat `Rectangle` to a 3-layer pixel block: base color + 2px top bright specular strip (`Color.white.opacity(0.32)`) + 2px bottom shade (`Color.black.opacity(0.22)`), enclosed in a crisp 2px stepped outer frame (`Theme.accentEdge`) on a recessed track (`Theme.dock`).
   - When `filled == PixelMeter.blocks && !over`, apply a subtle golden/lime pixel border highlight (respecting `reduceMotion`) to celebrate hitting 100% of a daily goal.
2. **`ProgressViewScreen` (`FoodViews.swift`)**:
   - Replace `.largeTitle.bold()` with `Theme.pixelFont(34, relativeTo: .largeTitle)` (and same in `WorkoutsView`).
   - Replace `.pickerStyle(.segmented)` with two side-by-side `PixelChoice` / beveled pixel toggle buttons (`7 DAYS`, `30 DAYS`).
   - Replace stock Swift Charts `BarMark` with a custom `PixelBarChart` view built from stacked pixel blocks (matching `PixelBar`'s 8-bit aesthetic) with a dashed pixel target line, day labels in `Theme.pixelFont(13)`, and full VoiceOver summary labels.
   - Replace stock `ProgressView` loaders across `TodayView`, `ManualEntryView`, and `ProgressViewScreen` with `PixelActivity`.
3. **Pixel-styled Sheets (`ManualEntryView`, `SavedMealEditor`, preset servings sheet in `FoodViews.swift`)**:
   - Replace stock UIKit `Form {}` containers with `ScrollView` + `PixelPanel` + `PixelValueField` + `PixelChoice` (meal slot selector) + `PixelPrimaryButtonStyle` save button on `Theme.canvas`, keeping keyboard Done toolbar and all existing accessibility identifiers (`edit-preset-name`, `edit-preset-emoji`, `save-preset-edit`).
4. **`BarcodeScannerView.swift`**:
   - Replace the smooth `RoundedRectangle(cornerRadius: 18)` scan frame with 8-bit stepped pixel corner brackets (L-shaped pixel reticles in `Theme.profileAccent`) and replace `.ultraThinMaterial` circular top-bar buttons with stepped `PixelBevel` square icon buttons (44×44pt).

**Verify**: Run `xcodebuild test ... -only-testing:MacroBitTests` → `TEST SUCCEEDED`.

### Step 4: Custom Pixel Tab Icons & Clean Tab IA (`StudioLayout.swift`)

1. Add 24×24 orthogonal stepped SVG assets (same 2px pixel grid style as `PixelLeaf.imageset/leaf.svg`) to `Assets.xcassets`: `PixelStudio`, `PixelToday`, `PixelDumbbell`, `PixelChart`, `PixelMenu`.
2. In `StudioLayout.swift`:
   - Clean up tab indices from `(0, 2, 3)` to contiguous enum `enum StudioTab: Int { case studio = 0, today = 1, workouts = 2, progress = 3 }` (or keep 3 tabs with clear naming: **Studio** (coach + quick saved-meal strip), **Log / Today** (macros HUD + full saved-meal grid + meals list), **Progress** — with Workouts reachable or 4th tab). *Wait: verify `StudioUITests.swift` tab identifiers `studio.tab.0`, `studio.tab.2`, `studio.tab.3` and update tests in lockstep if adding/renaming tabs!*
   - Style active tab as a raised 3D beveled pixel cartridge tab (`PixelBevel`) with a 2px accent indicator bar on top, using the new pixel SVG glyphs instead of SF Symbols.
   - Add tests in `StudioResourceTests.swift` verifying all new pixel SVG asset names resolve via `UIImage(named:)`.

**Verify**: Full `xcodebuild clean test` → `TEST SUCCEEDED`, 0 failures.

## Test plan

- `StudioResourceTests.swift`:
  - Assert every new pixel SVG icon (`PixelStudio`, `PixelDumbbell`, `PixelChart`, `PixelMenu`, etc.) loads non-nil.
  - Assert `PixelBarChart` geometry helper handles empty days, zero targets, and over-target days without NaN/negative heights.
- `StudioUITests.swift`:
  - Re-run all existing UI tests and inspect generated screenshots across Studio, Today, Progress, Workouts, Manual Entry, Saved Meal Editor, and Scanner to verify zero clipping, zero text truncation, and clean alignment.

## Done criteria

- [ ] `xcodebuild clean test` passes all unit and UI tests with 0 failures.
- [ ] `grep -rn "PixelNativeNavigation" ios-macrobit/` returns 0 matches.
- [ ] `grep -rn "Capsule()" ios-macrobit/MacroBit/CoachChatView.swift` returns 0 matches.
- [ ] `grep -rn "Form {" ios-macrobit/MacroBit/FoodViews.swift` returns 0 matches.
- [ ] `PixelPrimaryButtonStyle` bottom shadow is clipped cleanly inside `PixelPanelShape()`.
- [ ] No files outside Scope are modified.

## STOP conditions

- Stop if changing `StudioTabBar` tab count breaks external deep links or `StudioUITests` assertions in ways that require product input — keep 3 tabs (`Studio`, `Workouts`, `Progress`) and label the modal sheet `"Food Log"` instead of `"Today"` if unsure.
- Stop if any `PixelValueField` inside `ManualEntryView` fails Dynamic Type accessibility size layout without wrapping.

## Maintenance notes

- All future screens (e.g., Workouts when built) should compose exclusively from `PixelPanel`, `PixelBevel`, `PixelValueField`, `PixelChoice`, `PixelAction`, and `PixelActivity` rather than stock SwiftUI `Form` or `ProgressView`.
