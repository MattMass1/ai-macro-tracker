# Macro Tracker — SwiftUI Native iOS App

## Overview

Full native SwiftUI rewrite of the Macro Tracker web app. Same backend
(Render + Postgres), same features, native iOS feel. Built for Xcode 26.3+,
iOS 17.0+, Swift 5.

## Project structure (under `ios/`)

```
ios/
  project.yml              # XcodeGen spec (generates .xcodeproj)
  Config.xcconfig          # Build settings (bundle ID, deployment target)
  MacroTracker/
    MacroTrackerApp.swift   # @main App entry + passcode gate
    ContentView.swift       # TabView root (Today / ChatLog / Workouts)
    Models/
      Macros.swift           # MacroTotals, FoodEntry, MealRollup, DayPayload
      Preset.swift           # Preset model
      Workout.swift          # WorkoutEntry, WorkoutPlan, WorkoutStats, KnownExercise, LastWorkout
      Chat.swift             # ChatPayload, VisionPayload
      Target.swift           # MacroTarget
      Brief.swift            # BriefPayload
    Services/
      APIClient.swift        # URLSession + async/await, base URL + token, Codable
      Config.swift           # Reads MACRO_API_URL, APP_SHARED_TOKEN from Config.xcconfig
      ApiError.swift         # ApiError: status + message
      AuthService.swift      # Passcode check + Keychain/UserDefaults
    Views/
      TodayView.swift        # Rings + meal list + preset grid
      ChatLogView.swift      # Chat input + vision + meal log
      WorkoutsView.swift     # Dashboard + plan + logger + history
      Components/
        MacroRing.swift      # Individual macro ring (cal, protein, carbs, fat, fiber)
        MacroRingsView.swift # All rings grid
        MealRow.swift        # Single meal entry row
        MealListView.swift   # Today's meals grouped
        PresetCard.swift     # Single preset button
        PresetGridView.swift # Grid of presets
        ChatBubble.swift     # Chat message bubble
        FoodInput.swift      # Manual food entry form
        WorkoutPlanCard.swift
        WorkoutLoggerCard.swift
        WorkoutChart.swift
        DayPicker.swift      # Date selection for history
    Resources/
      Assets.xcassets        # Icons, splash, accent colors
```

## Data models (mirror web/lib/types.ts 1:1)

```swift
struct MacroTotals: Codable {
    var calories, protein, carbs, fat, fiber: Double
}

struct FoodEntry: Codable {
    var id, name, meal: String
    var calories, protein, carbs, fat, fiber: Double
    var macro_source: String
    var day: String?
    var created_time: String?
}

struct Preset: Codable {
    var id, name, emoji: String
    var calories, protein, carbs, fat, fiber: Double
    var meal: String
    var sort_order: Int
    var active: Bool
}

struct MealRollup: Codable {
    var id, day, meal_type: String
    var calories, protein, carbs, fat, fiber: Double
}

struct DayPayload: Codable {
    var date, day_label: String
    var totals: MacroTotals
    var targets: MacroTotals?
    var remaining: MacroTotals?
    var meals: [FoodEntry]
    var day_rollup: MealRollup?
    var meal_rollups: [MealRollup]?
    var presets: [Preset]?
}

struct MacroTarget: Codable {
    var id, name: String
    var calories, protein, carbs, fat, fiber: Double
    var effective_date: String?
}

struct WorkoutEntry: Codable {
    var id, exercise_name: String
    var workout_type: [String]
    var muscle_group: [String]
    var weight_1, reps_1, weight_2, reps_2, weight_3, reps_3, weight_4, reps_4: Int?
    var day: String?
    var created_at: String?
}

struct WorkoutPlan: Codable {
    var workout_type: String
    var exercises: [String]
    var rotation: [String]?
}

struct WorkoutStats: Codable {
    // Workout dashboard aggregated data
    var current_rotation: String?
    var this_week_count: Int?
    var known_exercises: [KnownExercise]?
    var recent_workouts: [String]?
}

struct KnownExercise: Codable {
    var name, workout_type: String
}

struct LastWorkoutData: Codable {
    var exercise, date: String?
    var weight, reps: Double?
}

struct LogMealBody: Codable {
    var name, meal: String
    var calories, protein, carbs, fat, fiber: Double
    var macro_source: String
    var day: String?
}

struct LogPresetBody: Codable {
    var id: String
    var servings: Double
    var meal: String?
    var day: String?
}

struct LogWorkoutBody: Codable {
    var exercise, workout_type: String
    var sets: [[String: Int?]]  // [{weight, reps}, ...]
    var muscle_group: String?
    var day: String?
    var note: String?
}

struct BriefPayload: Codable {
    var day, text: String?
}
```

## APIClient design

```swift
final class APIClient {
    static let shared = APIClient()
    private let base: String
    private let token: String
    private let session: URLSession
    private let decoder: JSONDecoder

    enum Endpoint {
        case today, presets, exercises, plan, workoutStats, brief(String?)
        case day(String), workouts(String), lastWorkout(String)
        case log(LogMealBody), logPreset(LogPresetBody), chat(String),
             visionLog(String, String?), deleteMeal(String),
             logWorkout(LogWorkoutBody), deleteWorkout(String),
             postBrief(String, String?)
    }

    func request<T: Decodable>(_ endpoint: Endpoint) async throws -> T
    func requestNoResponse(_ endpoint: Endpoint) async throws
}
```

Headers: `X-App-Token: <token>`, `Content-Type: application/json`.
Method: GET for reads, POST for creates, DELETE for deletes.
Error: throw `ApiError(status, message)` mirroring the web's error handling.

The token and URL come from `Config.xcconfig` (NEXT_PUBLIC_* style build
settings, embedded at compile time). No server-side proxy needed.

## Passcode gate (AuthService)

- First launch: show passcode creation screen
- Subsequent launches: show unlock screen
- Lock on background (ScenePhase.active → .background)
- Store hashed passcode in Keychain (Security framework)
- Token and API URL stored in Config.xcconfig

## Screen navigation (ContentView.swift)

TabView with 3 tabs:

1. **Today** (default)
   - MacroRingsView (cal/protein/carbs/fat/fiber rings + targets)
   - MealListView (food entries for today grouped by meal)
   - PresetGridView (quick-add buttons)
   - DayPicker to browse past days

2. **Chat & Log**
   - ChatLogView (message history: bot prompts + user inputs)
   - Text input + camera button for vision logging
   - Manual food entry form (name, meal, macros)
   - Auto-sends to POST /api/chat, POST /api/vision-log, POST /api/log

3. **Workouts**
   - WorkoutDashboard (current rotation, week count, PRs)
   - WorkoutPlanCard (today's exercises plan)
   - WorkoutLogger (exercise select + weight/reps input)
   - Workout history (past entries, delete)

## Navigation structure

```swift
TabView(selection: $selectedTab) {
    TodayView().tabItem { Label("Today", systemImage: "circle.grid.2x2") }
    ChatLogView().tabItem { Label("Log", systemImage: "plus.circle") }
    WorkoutsView().tabItem { Label("Workouts", systemImage: "figure.strengthtraining.traditional") }
}
```

Custom tab bar styling (iOS native TabView appearance). Accent color from
the web's green/protein color.

## Build order (implementation sequence)

1. `Models/*.swift` — all Codable data types
2. `Services/Config.swift` + `Services/ApiError.swift` — config + errors
3. `Services/APIClient.swift` — URLSession + all endpoints
4. `Services/AuthService.swift` — passcode gate
5. `MacroTrackerApp.swift` + `ContentView.swift` — app entry + tab nav
6. `Views/Components/*.swift` — reusable widgets (MacroRing, PresetCard, etc.)
7. `Views/TodayView.swift` — assemble rings + meals + presets
8. `Views/ChatLogView.swift` — chat + vision + manual
9. `Views/WorkoutsView.swift` — dashboard + plan + logger

## XcodeGen project.yml

```yaml
name: MacroTracker
targets:
  MacroTracker:
    type: application
    platform: iOS
    deploymentTarget: "17.0"
    sources: [MacroTracker]
    settings:
      PRODUCT_BUNDLE_IDENTIFIER: com.biz21.macrotracker
      DEVELOPMENT_TEAM: ""  # User fills in their Apple ID
      INFOPLIST_FILE: MacroTracker/Info.plist
      CODE_SIGN_STYLE: Automatic
      MARKETING_VERSION: 1.0
      CURRENT_PROJECT_VERSION: 1
      SWIFT_VERSION: "5"
      SWIFT_ACTIVE_COMPILATION_CONDITIONS: "DEBUG"  # Toggle for release
    preBuildScripts:
      - name: "Copy Config"
        script: |
          if [ ! -f "Config.xcconfig" ]; then
            cp Config.xcconfig.template Config.xcconfig
          fi
    info:
      path: MacroTracker/Info.plist
      properties:
        UIRequiredDeviceCapabilities: [arm64]
        UISupportedInterfaceOrientations: [UIInterfaceOrientationPortrait]
        UIStatusBarStyle: UIStatusBarStyleDefault
        NSCameraUsageDescription: "Take a photo of your food to log macros automatically"
        NSFaceIDUsageDescription: "Lock the app with Face ID"
        ITSAppUsesNonExemptEncryption: false
        UIApplicationSupportsMultipleScenes: false
        UILaunchScreen: {}
```

## Implementation notes

- Every `Codable` uses `CodingKeys` with `convertFromSnakeCase` strategy
  on the decoder so Swift field names are idiomatic (e.g., `macroSource`)
  but the JSON uses snake_case.
- Date formatting: ISO 8601 string → Date via date decoding strategy or
  manual formatter. Backend sends dates as "2026-08-14" or ISO strings.
- The APIClient is a singleton with shared session. For a personal app
  this is fine; for scale we'd add DI/repository layer.
- Errors map to user-facing alerts: 401/403 → "Session expired",
  503 → "Server is waking up (Render free tier)", 4xx/5xx → message from
  the error payload.
- No SwiftUI previews in the first pass (they slow the agent writes).
  Fable can add them in review.

## References

- Backend: https://ai-macro-tracker-nuoo.onrender.com
- API contract: web/lib/apiClient.ts (in repo)
- Type contract: web/lib/types.ts (in repo)
- Web app feature reference: web/components/ (in repo)