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

/// Write payload for POST /api/plan. Matches `domain.validate_workout_plan`.
struct WorkoutPlanWrite: Encodable, Equatable {
    struct Day: Encodable, Equatable {
        struct Exercise: Encodable, Equatable {
            var name: String
            var sets: Int
            var reps: String
            var restSec: Int
            var swaps: [String]?
        }
        var label: String
        var exercises: [Exercise]
    }

    var version: Int
    var rotation: [String]
    var daysPerWeek: Int
    var days: [String: Day]
    var notes: String?
}

struct SavePlanPayload: Decodable, Equatable {
    var warnings: [String]?
}

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

struct ExerciseCardExercise: Codable, Equatable {
    var name: String?
    var muscleGroup: String?
    var equipment: String?
    var sets: String?
    var reps: String?
    var videoUrl: String?
    var instructions: String?
    var workoutType: String?

    enum CodingKeys: String, CodingKey {
        case name, muscleGroup, equipment, sets, reps, videoUrl, instructions, workoutType
    }

    init(
        name: String? = nil,
        muscleGroup: String? = nil,
        equipment: String? = nil,
        sets: String? = nil,
        reps: String? = nil,
        videoUrl: String? = nil,
        instructions: String? = nil,
        workoutType: String? = nil
    ) {
        self.name = name
        self.muscleGroup = muscleGroup
        self.equipment = equipment
        self.sets = sets
        self.reps = reps
        self.videoUrl = videoUrl
        self.instructions = instructions
        self.workoutType = workoutType
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = Self.decodeText(container, forKey: .name)
        muscleGroup = Self.decodeLabel(container, forKey: .muscleGroup)
        equipment = Self.decodeText(container, forKey: .equipment)
        sets = Self.decodeText(container, forKey: .sets)
        reps = Self.decodeText(container, forKey: .reps)
        videoUrl = Self.decodeText(container, forKey: .videoUrl)
        instructions = Self.decodeText(container, forKey: .instructions)
        workoutType = Self.decodeLabel(container, forKey: .workoutType)
    }

    var displayName: String {
        let trimmed = name?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? "Exercise" : trimmed
    }

    var loggerExerciseName: String {
        name?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    /// Prefer `workoutType`; muscle group is a reasonable fallback for the logger chips.
    var loggerType: String {
        if let type = Self.trimmed(workoutType) { return type }
        if let muscle = Self.trimmed(muscleGroup) { return muscle }
        return "Push"
    }

    var videoURL: URL? {
        guard let raw = Self.trimmed(videoUrl) else { return nil }
        return URL(string: raw)
    }

    private static func trimmed(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func decodeText(_ container: KeyedDecodingContainer<CodingKeys>, forKey key: CodingKeys) -> String? {
        if let value = try? container.decode(String.self, forKey: key) {
            return trimmed(value)
        }
        if let value = try? container.decode(Int.self, forKey: key) {
            return String(value)
        }
        if let value = try? container.decode(Double.self, forKey: key) {
            return value == value.rounded() ? String(Int(value)) : String(value)
        }
        return nil
    }

    private static func decodeLabel(_ container: KeyedDecodingContainer<CodingKeys>, forKey key: CodingKeys) -> String? {
        if let value = decodeText(container, forKey: key) { return value }
        if let values = try? container.decode([String].self, forKey: key) {
            return values.compactMap { trimmed($0) }.first
        }
        return nil
    }
}

struct ExerciseCardWidget: Codable, Equatable {
    var type: String
    var exercise: ExerciseCardExercise

    enum CodingKeys: String, CodingKey { case type, exercise }

    init(type: String = "exercise_card", exercise: ExerciseCardExercise) {
        self.type = type
        self.exercise = exercise
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        type = try container.decodeIfPresent(String.self, forKey: .type) ?? "exercise_card"
        exercise = try container.decodeIfPresent(ExerciseCardExercise.self, forKey: .exercise) ?? ExerciseCardExercise()
    }
}

/// In-chat widget payload. Unknown types decode as nil on `ChatReply` so additive backend fields cannot break chat.
enum ChatWidget: Equatable {
    case metricsForm(MetricsFormWidget)
    case exerciseCard(ExerciseCardWidget)

    var type: String {
        switch self {
        case .metricsForm(let widget): return widget.type
        case .exerciseCard(let widget): return widget.type
        }
    }

    var fields: [MetricsField] {
        switch self {
        case .metricsForm(let widget): return widget.fields
        case .exerciseCard: return []
        }
    }

    var exercise: ExerciseCardExercise? {
        switch self {
        case .exerciseCard(let widget): return widget.exercise
        case .metricsForm: return nil
        }
    }
}

extension ChatWidget: Codable {
    private enum CodingKeys: String, CodingKey { case type }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let type = try container.decodeIfPresent(String.self, forKey: .type) ?? ""
        switch type {
        case "exercise_card":
            self = .exerciseCard(try ExerciseCardWidget(from: decoder))
        case "metrics_form":
            self = .metricsForm(try MetricsFormWidget(from: decoder))
        default:
            throw DecodingError.dataCorrupted(.init(
                codingPath: decoder.codingPath,
                debugDescription: "Unsupported chat widget type: \(type)"
            ))
        }
    }

    func encode(to encoder: Encoder) throws {
        switch self {
        case .metricsForm(let widget): try widget.encode(to: encoder)
        case .exerciseCard(let widget): try widget.encode(to: encoder)
        }
    }
}

// Optionals keep decoding compatible while the coach backend rolls out the flags.
struct ChatHistoryMessage: Codable {
    var id: String
    var role: String
    var content: String
}

struct LoggedMeal: Codable {
    var name: String
    var calories: Double
    var protein: Double?
    var carbs: Double?
    var fat: Double?
}

struct ChatReply: Codable {
    var reply: String
    var hasPlan: Bool?
    var hasTargets: Bool?
    var widget: ChatWidget?
    var logged: [LoggedMeal]?

    enum CodingKeys: String, CodingKey { case reply, hasPlan, hasTargets, widget, logged }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        reply = try container.decode(String.self, forKey: .reply)
        hasPlan = try container.decodeIfPresent(Bool.self, forKey: .hasPlan)
        hasTargets = try container.decodeIfPresent(Bool.self, forKey: .hasTargets)
        widget = try Self.decodeWidget(from: container)
        logged = try container.decodeIfPresent([LoggedMeal].self, forKey: .logged)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(reply, forKey: .reply)
        try container.encodeIfPresent(hasPlan, forKey: .hasPlan)
        try container.encodeIfPresent(hasTargets, forKey: .hasTargets)
        try container.encodeIfPresent(widget, forKey: .widget)
        try container.encodeIfPresent(logged, forKey: .logged)
    }

    private static func decodeWidget(from container: KeyedDecodingContainer<CodingKeys>) throws -> ChatWidget? {
        guard container.contains(.widget) else { return nil }
        if try container.decodeNil(forKey: .widget) { return nil }
        enum TypeKey: String, CodingKey { case type }
        let type = try? container.nestedContainer(keyedBy: TypeKey.self, forKey: .widget)
            .decodeIfPresent(String.self, forKey: .type)
        switch type {
        case "metrics_form", "exercise_card":
            return try container.decode(ChatWidget.self, forKey: .widget)
        default:
            return nil
        }
    }
}
struct ClaimInviteBody: Codable { var code: String; var label: String?; var displayName: String? }
struct ClaimInvitePayload: Codable { var token: String; var displayName: String }
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
    var macrosPerServing: MacroTotals?
}
struct BriefPayload: Codable { var text: String?; var date: String }
struct BriefRequest: Codable { var text: String; var date: String? }
