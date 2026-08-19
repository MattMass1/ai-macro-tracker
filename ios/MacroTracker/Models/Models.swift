import Foundation

struct MacroTotals: Codable, Equatable {
    var calories: Double = 0
    var protein: Double = 0
    var carbs: Double = 0
    var fat: Double = 0
    var fiber: Double = 0

    static let zero = MacroTotals()
}

struct FoodEntry: Codable, Identifiable, Equatable {
    let id: String
    var name: String
    var meal: String
    var calories: Double
    var protein: Double
    var carbs: Double
    var fat: Double
    var fiber: Double
    var macroSource: String?
    var date: String?
    var day: String?
    var createdTime: String?
}

struct Preset: Codable, Identifiable, Equatable {
    var id: String { name }
    var name: String
    var emoji: String
    var calories: Double
    var protein: Double
    var carbs: Double
    var fat: Double
    var fiber: Double
    var meal: String
    var sortOrder: Int
    var active: Bool?
}

struct PresetsPayload: Codable { var presets: [Preset] }

struct MealRollup: Codable, Identifiable, Equatable {
    var id: String
    var day: String
    var mealType: String
    var calories: Double
    var protein: Double
    var carbs: Double
    var fat: Double
    var fiber: Double
}

struct DayPayload: Codable, Equatable {
    var date: String
    var dayLabel: String
    var totals: MacroTotals
    var targets: MacroTotals
    var remaining: MacroTotals
    var meals: [FoodEntry]
    var presets: [Preset]?
    var warning: String?
    var dayRollup: MealRollup?
    var mealRollups: [MealRollup]?
}

struct MacroTarget: Codable, Identifiable {
    var id: String
    var name: String
    var calories: Double
    var protein: Double
    var carbs: Double
    var fat: Double
    var fiber: Double
    var effectiveDate: String?
}

struct WorkoutSet: Codable, Equatable {
    var weight: Double
    var reps: Int
}

struct WorkoutEntry: Codable, Identifiable, Equatable {
    let id: String
    var exercise: String
    var workoutType: [String]
    var muscleGroup: [String]?
    var sets: [WorkoutSet]
    var date: String?
    var createdTime: String?
}

struct KnownExercise: Codable, Identifiable, Hashable {
    var id: String { name }
    var name: String
    var workoutType: [String]
}

struct ExercisesPayload: Codable { var exercises: [KnownExercise] }
struct WorkoutsPayload: Codable { var date: String; var dayLabel: String; var workouts: [WorkoutEntry] }
struct LastWorkoutPayload: Codable { var exercise: String?; var sets: [WorkoutSet]; var date: String?; var workoutType: [String]? }
struct DeletedPayload: Codable { var deleted: String }

struct WorkoutPlanPayload: Codable {
    struct Exercise: Codable, Identifiable { var id: String { name }; var name: String }
    struct PlannedDay: Codable, Identifiable { var id: String { type }; var type: String; var exercises: [Exercise] }
    var rotation: [String]
    var lastWorkout: String?
    var upcoming: [PlannedDay]
    var core: [Exercise]
    // Optional keeps decoding compatible with servers deployed before `has_plan`.
    var hasPlan: Bool?
}

struct LibraryExercise: Codable, Identifiable, Hashable {
    var id: String { name }
    var name: String
    var muscleGroup: [String]
    var workoutType: String
    var equipment: String
    var difficulty: String
    var swaps: [String]
}

struct LibraryPayload: Codable { var exercises: [LibraryExercise] }

struct WorkoutStatsPayload: Codable {
    struct Today: Codable { var date: String; var entries: Int; var exercises: [ExerciseSummary] }
    struct ExerciseSummary: Codable, Identifiable { var id: String { name }; var name: String; var sets: Int; var weight: String; var reps: String }
    struct Week: Codable { var weekStart: String; var weekLabel: String; var daysLogged: Int; var totalSets: Int; var totalVolume: Double; var streakWeeks: Int }
    struct Coverage: Codable { var muscleGroups: [String: Int]; var workoutTypes: [String: Int]; var untouched: [String] }
    struct PR: Codable, Identifiable { var id: String { exercise }; var exercise: String; var maxWeight: Double; var date: String }
    struct Plan: Codable { var today: PlanDay; var next: [PlanDay] }
    struct PlanDay: Codable, Identifiable { var id: String { type }; var type: String; var exercises: [String] }
    var today: Today
    var week: Week
    var coverage: Coverage
    var prs: [PR]
    var plan: Plan
}

struct LogMealBody: Codable {
    var name: String
    var calories: Double
    var protein: Double
    var carbs: Double
    var fat: Double
    var fiber: Double
    var macroSource: String
    var meal: String?
    var day: String?
    enum CodingKeys: String, CodingKey {
        case name, calories, protein, carbs, fat, fiber, macroSource, meal
        case day = "date"
    }
}

struct LogPresetBody: Codable {
    var presetName: String; var servings: Double; var meal: String?; var day: String?
    enum CodingKeys: String, CodingKey { case presetName, servings, meal; case day = "date" }
}
struct LogWorkoutBody: Codable { var exercise: String; var sets: [WorkoutSet]; var workoutType: String; var date: String? }
struct ChatRequest: Codable { var message: String; var date: String? }
struct ChatPayload: Codable { var reply: String; var logged: [FoodEntry]; var totals: MacroTotals }
// Optionals keep decoding compatible while the coach backend rolls out the flags.
struct ChatReply: Codable { var reply: String; var hasPlan: Bool?; var hasTargets: Bool? }
struct ClaimInviteBody: Codable { var code: String; var label: String?; var displayName: String? }
struct ClaimInvitePayload: Codable { var token: String; var displayName: String }
struct VisionRequest: Codable { var image: String; var meal: String? }
struct VisionPayload: Codable { var name: String; var note: String; var meal: String?; var calories: Double; var protein: Double; var carbs: Double; var fat: Double; var fiber: Double }
struct BriefPayload: Codable { var text: String?; var date: String }
struct BriefRequest: Codable { var text: String; var date: String? }
