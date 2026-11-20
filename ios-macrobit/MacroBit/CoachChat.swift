import Foundation

// Models for the classic Coach chat. The chat talks to the agent's turn and
// action endpoints and keeps only what a plain message feed needs: the reply
// text, a pending approval (workout changes need a confirm tap) and whether
// the server asked for measurements. Everything else in the envelope is
// ignored; decoding never fails on unknown keys.

struct CoachApproval: Decodable, Equatable, Identifiable {
    let id: String
    let title: String?
    let detail: String?
    let status: String?
}

enum CoachSurface: Decodable, Equatable {
    case mealPreview(MealPreviewSurface)
    case mealReceipt(MealReceiptSurface)
    case saveFailed(SaveFailedSurface)
    case dayTotals(DayTotalsSurface)

    private enum Keys: String, CodingKey { case kind }
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: Keys.self)
        switch try container.decode(String.self, forKey: .kind) {
        case "meal_preview":
            let value = try MealPreviewSurface(from: decoder)
            guard value.isValid else { throw PayloadError.invalidPayload }
            self = .mealPreview(value)
        case "meal_receipt":
            let value = try MealReceiptSurface(from: decoder)
            guard value.isValid else { throw PayloadError.invalidPayload }
            self = .mealReceipt(value)
        case "save_failed":
            let value = try SaveFailedSurface(from: decoder)
            guard surfaceText(value.reason, max: 300) else { throw PayloadError.invalidPayload }
            self = .saveFailed(value)
        case "day_totals":
            let value = try DayTotalsSurface(from: decoder)
            guard value.isValid else { throw PayloadError.invalidPayload }
            self = .dayTotals(value)
        default: throw PayloadError.invalidPayload
        }
    }
}

struct SurfaceNutrition: Decodable, Equatable {
    let calories: Double?
    let protein: Double?
    let carbs: Double?
    let fat: Double?
    let fiber: Double?
    private enum Keys: String, CodingKey { case calories, protein, carbs, fat, fiber }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        calories = try c.decode(Double?.self, forKey: .calories)
        protein = try c.decode(Double?.self, forKey: .protein)
        carbs = try c.decode(Double?.self, forKey: .carbs)
        fat = try c.decode(Double?.self, forKey: .fat)
        fiber = try c.decode(Double?.self, forKey: .fiber)
    }
    var values: [Double?] { [calories, protein, carbs, fat, fiber] }
    var isFinite: Bool { values.compactMap { $0 }.allSatisfy(\.isFinite) }
    var isNonnegative: Bool { isFinite && values.compactMap { $0 }.allSatisfy { $0 >= 0 } }
    var isComplete: Bool { values.allSatisfy { $0 != nil } }
    var isTotal: Bool { calories != nil && isNonnegative }
}

struct SurfaceDay: Decodable, Equatable {
    let totals: SurfaceNutrition
    let targets: SurfaceNutrition
    let macros_complete: Bool
    let target_source: String
    var isValid: Bool {
        totals.isTotal && targets.isNonnegative && targets.isComplete && macros_complete == totals.isComplete
            && ["configured", "fallback"].contains(target_source)
    }
}

struct SurfaceFood: Decodable, Equatable {
    let name: String
    let portion: String
    let assumed: Bool
    let source: String
    let not_counted_reason: String?
    let modifications: String?
    let recipe_note: String?
    let nutrition: SurfaceNutrition
    private enum Keys: String, CodingKey { case name, portion, assumed, source, not_counted_reason, modifications, recipe_note }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        name = try c.decode(String.self, forKey: .name)
        portion = try c.decode(String.self, forKey: .portion)
        assumed = try c.decode(Bool.self, forKey: .assumed)
        source = try c.decode(String.self, forKey: .source)
        not_counted_reason = try c.decode(String?.self, forKey: .not_counted_reason)
        modifications = try c.decodeIfPresent(String.self, forKey: .modifications)
        recipe_note = try c.decodeIfPresent(String.self, forKey: .recipe_note)
        nutrition = try SurfaceNutrition(from: decoder)
    }
    var isValid: Bool {
        surfaceText(name, max: 200) && surfaceText(portion, max: 300) && surfaceText(source, max: 1000)
            && nutrition.isNonnegative && (not_counted_reason.map { surfaceText($0, max: 300) } ?? true)
    }
}

struct SurfaceEntry: Decodable, Equatable {
    let id: String
    let name: String
    let nutrition: SurfaceNutrition
    private enum Keys: String, CodingKey { case id, name }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try c.decode(String.self, forKey: .name)
        nutrition = try SurfaceNutrition(from: decoder)
    }
    var isValid: Bool { UUID(uuidString: id) != nil && surfaceText(name, max: 200) && nutrition.isTotal }
}

struct MealPreviewSurface: Decodable, Equatable {
    let date: String
    let meal: String
    let notice: String?
    let foods: [SurfaceFood]
    let totals: SurfaceNutrition
    let macros_complete: Bool
    let day_after: SurfaceDay?
    var isValid: Bool {
        surfaceDate(date) && surfaceMeal(meal) && (1...24).contains(foods.count) && foods.allSatisfy(\.isValid)
            && totals.isTotal && macros_complete == totals.isComplete && (day_after?.isValid ?? true)
    }
}

struct MealReceiptSurface: Decodable, Equatable {
    let date: String
    let meal: String
    let source: String?
    let entries: [SurfaceEntry]
    let totals: SurfaceNutrition
    let macros_complete: Bool
    let day_after: SurfaceDay?
    var isValid: Bool {
        surfaceDate(date) && surfaceMeal(meal) && (1...24).contains(entries.count) && entries.allSatisfy(\.isValid)
            && totals.isTotal && macros_complete == totals.isComplete && (day_after?.isValid ?? true)
    }
}

struct SaveFailedSurface: Decodable, Equatable {
    let reason: String
    let retry_safe: Bool
}

struct DayTotalsSurface: Decodable, Equatable {
    let date: String
    let totals: SurfaceNutrition
    let targets: SurfaceNutrition
    let remaining: SurfaceNutrition
    let macros_complete: Bool
    let target_source: String
    var isValid: Bool {
        surfaceDate(date) && totals.isTotal && targets.isComplete && targets.isNonnegative && remaining.isFinite
            && remaining.calories != nil && macros_complete == totals.isComplete && ["configured", "fallback"].contains(target_source)
    }
}

private func surfaceText(_ value: String, max: Int) -> Bool {
    !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && value.count <= max && !value.contains("\0")
}

func surfaceNumber(_ value: Double?) -> String {
    guard let value else { return "Unknown" }
    let text = String(value)
    return text.hasSuffix(".0") ? String(text.dropLast(2)) : text
}

func surfaceNutrientText(_ value: Double?) -> String {
    value == nil ? "Unknown" : "\(surfaceNumber(value))g"
}

private func surfaceMeal(_ value: String) -> Bool { ["Breakfast", "Lunch", "Dinner", "Snack"].contains(value) }

private func surfaceDate(_ value: String) -> Bool {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.calendar = Calendar(identifier: .gregorian)
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    formatter.dateFormat = "yyyy-MM-dd"
    formatter.isLenient = false
    guard value.count == 10, let date = formatter.date(from: value) else { return false }
    return formatter.string(from: date) == value
}

struct CoachTurn: Decodable {
    let reply: String?
    let approval: CoachApproval?
    /// True when the server presented its measurements form (a `ProfileMetrics`
    /// component); the chat then shows the inline metrics card.
    let wantsMetricsForm: Bool
    /// One-tap answers for this reply ("Log it", "Cancel", "6 oz"). A tap sends
    /// the label as an ordinary message, exactly as if it had been typed.
    let quickReplies: [String]
    let surfaces: [CoachSurface]

    private enum Keys: String, CodingKey { case reply, approval, surfaces, quickReplies }
    private struct Surface: Decodable {
        let components: [Component]?
        let card: CoachSurface?
        private enum Keys: String, CodingKey { case components }
        init(from decoder: Decoder) throws {
            let container = try? decoder.container(keyedBy: Keys.self)
            components = try? container?.decodeIfPresent([Component].self, forKey: .components)
            card = try? CoachSurface(from: decoder)
        }
    }
    private struct Component: Decodable { let component: String? }

    init(reply: String?, approval: CoachApproval? = nil, wantsMetricsForm: Bool = false, quickReplies: [String] = [], surfaces: [CoachSurface] = []) {
        self.reply = reply; self.approval = approval; self.wantsMetricsForm = wantsMetricsForm
        self.quickReplies = quickReplies
        self.surfaces = Array(surfaces.prefix(4))
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: Keys.self)
        reply = try container.decodeIfPresent(String.self, forKey: .reply)
        approval = try? container.decodeIfPresent(CoachApproval.self, forKey: .approval)
        let surfaces = (try? container.decodeIfPresent([Surface].self, forKey: .surfaces)) ?? []
        wantsMetricsForm = surfaces.contains { surface in
            (surface.components ?? []).contains { $0.component == "ProfileMetrics" }
        }
        self.surfaces = Array(surfaces.compactMap(\.card).prefix(4))
        let labels = (try? container.decodeIfPresent([String].self, forKey: .quickReplies)) ?? []
        quickReplies = Self.cleanQuickReplies(labels)
    }

    /// At most four short, distinct, non-empty labels; anything else is dropped.
    static func cleanQuickReplies(_ labels: [String]) -> [String] {
        var seen = Set<String>()
        return labels
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty && $0.count <= 40 && seen.insert($0.lowercased()).inserted }
            .prefix(4)
            .map { $0 }
    }
}

/// A server payload failed the app's own validation (day timing, chat body size).
enum PayloadError: LocalizedError, Equatable {
    case invalidPayload
    var errorDescription: String? { "This data could not be displayed. Your standard screens are still available." }
}

struct CoachIntent: Encodable {
    let action: String
    let reference: String?
}

struct ChatHistoryMessage: Codable {
    var role: String
    var content: String
}

struct ChatHistoryPayload: Codable { var messages: [ChatHistoryMessage] }

// Measurements form (restored from the pre-canvas app). The server lists the
// fields it wants; the card collects them and the chat sends a plain-text
// summary the agent stages through its normal set_metrics + Confirm path.

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

    /// The fields the server's measurements form asks for.
    static let standard: [MetricsField] = ["height_cm", "weight_kg", "goal_weight_kg", "age", "activity_level"]
        .map(inferred(from:))

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
