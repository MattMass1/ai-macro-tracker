# Implement Design B in SwiftUI — Dashboard-First (Light, iOS-native)

Reference mockup: `ios/ui-concepts/design-b-dashboard-light.html` (read it first — colors, spacing, layout come from there).
Existing app: `ios/MacroTracker/` (SwiftUI, iOS 17+, TabView with Today/Log/Workouts).

## Goal
Restyle and restructure the existing MacroTracker app to match Design B's dashboard-first light theme. This is a UI restyle of the app that EXISTS — do not rebuild models, APIClient, AuthService, or AppStore. Keep all existing logic; change presentation.

## Design system (from the mockup)

- Background: `#f5f6f8` (screen), cards: `#ffffff` with `box-shadow: 0 2px 12px rgba(0,0,0,.05)` → use subtle shadow/rounded 22pt cards
- Ink: `#1a1d23` (headlines), `#374151` (section titles), `#8a93a6` (muted labels)
- Accent: deep green `#0f5132` (primary, active tab, macro bars), light green tint `#e6f4ec`
- Macro colors: protein `#0f5132` green, carbs `#b45309` amber, fat `#db2777` pink, fiber `#7c3aed` purple
- Radius: 22pt cards, 14pt inner elements, 99pt pills
- Typography: system font, bold 800 for numbers, SF-style

## Screen structure (4 tabs, in this order)

1. **Today** (default tab)
   - Date header: "TUESDAY, AUGUST 18" (small, muted, uppercase) + "Today" (22pt bold) + chevron day pickers ‹ ›
   - **Coach strip** (top, before rings): green gradient card `#1a3a2a → #0f5132`, rounded 22pt. Left: 🤖 avatar circle. Middle: "Coach" bold + one-line status e.g. "Ready for Pull day? Recovery 68%". Right: "Ask →" white pill. Tapping opens the Coach tab.
   - **Daily Macros card**: white card. Header "Daily Macros". Big center ring (calories) — circle with green progress arc showing consumed/target, center shows "1,428" + "of 2,100 cal". Below: 4-column macro row (Protein/Carbs/Fat/Fiber) each with value, label, and a thin progress bar.
   - **Meals card**: white card. Header "Meals". Rows: emoji tile (34pt, rounded 10pt, light bg), name + sub ("Breakfast · 8:02 AM"), calories right-aligned bold. Dashed "+ Add food" button at bottom.
2. **Coach** (chat tab)
   - Keep the existing ChatLogView chat flow, but restyle to light theme: white cards for bot messages on `#f5f6f8` background, green-tinted bubbles for user, green send button, mic button. Header with avatar + "Coach" + online dot.
3. **Workouts** (existing WorkoutsView, restyled to match: white cards on light bg, green accents)
4. **Progress** (NEW simple tab — placeholder for now: white card with "Coming soon" + a basic 7-day calorie bar chart if data is available, otherwise empty state)

## Implementation notes

- Tab bar: white background, top border `#e8ebf0`, active tab = green `#0f5132` with tinted pill, inactive = `#9aa3b5`.
- Use SwiftUI `Color` extensions in `Theme.swift` — replace the existing theme with this palette. Keep the existing accent usage where it maps (protein green stays green).
- The big calorie ring: `Circle().trim(from:0,to:fraction).stroke()` with `conic`-like gradient — use AngularGradient or a simple trim stroke. Center text via ZStack.
- Macro progress bars: `GeometryReader` or `ProgressView(value:)` styled thin (4pt) with the macro color.
- Keep `.task`/loading/empty states from the existing views. Do not break the APIClient or AppStore.
- Do NOT touch: Models, APIClient, AuthService, AppStore, Config, passcode gate, or the server.
- Tests: run `xcodebuild test` via Xcode MCP; the existing ModelDecodingTests must still pass. If no simulator is available, build only and report.

## Verify

Build succeeds (`xcodebuild build` via MCP or CLI). Report: files changed + any deviations from the mockup.
