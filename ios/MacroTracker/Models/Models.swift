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

struct DayRollup: Codable, Equatable {
    var date: String
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
    var dayRollup: DayRollup?
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
struct ChatMetrics: Codable, Equatable {
    var heightCm: Double?
    var weightKg: Double?
    var goalWeightKg: Double?
    var age: Int?
    var activityLevel: String?

    init(heightCm: Double? = nil, weightKg: Double? = nil, goalWeightKg: Double? = nil,
         age: Int? = nil, activityLevel: String? = nil) {
        self.heightCm = heightCm
        self.weightKg = weightKg
        self.goalWeightKg = goalWeightKg
        self.age = age
        self.activityLevel = activityLevel
    }
}

struct ChatRequest: Codable {
    var message: String
    var date: String?
    var metrics: ChatMetrics?
}

struct ChatPayload: Codable { var reply: String; var logged: [FoodEntry]; var totals: MacroTotals }

enum MetricsFieldKind: String, Codable, Equatable {
    case number
    case string
}

struct MetricsField: Codable, Equatable {
    var key: String
    var label: String
    var unit: String?
    var placeholder: String?
    var type: MetricsFieldKind?

    var kind: MetricsFieldKind { type ?? Self.inferredKind(for: key) }
    var isNumeric: Bool { kind == .number }

    static func inferred(from key: String) -> MetricsField {
        MetricsField(
            key: key,
            label: defaultLabel(for: key),
            unit: defaultUnit(for: key),
            placeholder: defaultPlaceholder(for: key),
            type: inferredKind(for: key)
        )
    }

    static func inferredKind(for key: String) -> MetricsFieldKind {
        switch key {
        case "height_cm", "weight_kg", "goal_weight_kg", "age": return .number
        default: return .string
        }
    }

    static func defaultLabel(for key: String) -> String {
        switch key {
        case "height_cm": return "Height"
        case "weight_kg": return "Weight"
        case "goal_weight_kg": return "Goal weight"
        case "age": return "Age"
        case "activity_level": return "Activity level"
        default: return key.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    static func defaultUnit(for key: String) -> String? {
        switch key {
        case "height_cm": return "ft / in"
        case "weight_kg", "goal_weight_kg": return "lb"
        default: return nil
        }
    }

    static func defaultPlaceholder(for key: String) -> String? {
        switch key {
        case "height_cm": return "5 ft 10 in"
        case "weight_kg": return "175"
        case "goal_weight_kg": return "165"
        case "age": return "32"
        case "activity_level": return "Moderately active"
        default: return nil
        }
    }
}

struct MetricsFormWidget: Codable, Equatable {
    var type: String
    var fields: [MetricsField]

    enum CodingKeys: String, CodingKey { case type, fields }

    init(type: String, fields: [MetricsField]) {
        self.type = type
        self.fields = fields
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        type = try container.decode(String.self, forKey: .type)
        // Backend currently sends bare keys; object descriptors (with optional type) also decode.
        if let keys = try? container.decode([String].self, forKey: .fields) {
            fields = keys.map(MetricsField.inferred(from:))
        } else {
            fields = try container.decode([MetricsField].self, forKey: .fields)
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(type, forKey: .type)
        try container.encode(fields, forKey: .fields)
    }
}

// Optionals keep decoding compatible while the coach backend rolls out the flags.
struct ChatReply: Codable {
    var reply: String
    var hasPlan: Bool?
    var hasTargets: Bool?
    var widget: MetricsFormWidget?
}
struct ClaimInviteBody: Codable { var code: String; var label: String?; var displayName: String? }
struct ClaimInvitePayload: Codable { var token: String; var displayName: String }
struct VisionRequest: Codable { var image: String; var meal: String? }
struct VisionPayload: Codable { var name: String; var note: String; var meal: String?; var calories: Double; var protein: Double; var carbs: Double; var fat: Double; var fiber: Double }
struct BarcodeFoodRequest: Codable { var code: String }
struct BarcodeFoodPayload: Codable, Equatable {
    var name: String
    var calories: Double
    var protein: Double
    var carbs: Double
    var fat: Double
    var fiber: Double
    var source: String
    var servingSize: String?
}
struct BriefPayload: Codable { var text: String?; var date: String }
struct BriefRequest: Codable { var text: String; var date: String? }
