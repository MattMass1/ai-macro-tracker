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

struct CoachTurn: Decodable {
    let reply: String?
    let approval: CoachApproval?
    /// True when the server presented its measurements form (a `ProfileMetrics`
    /// component); the chat then shows the inline metrics card.
    let wantsMetricsForm: Bool

    private enum Keys: String, CodingKey { case reply, approval, surfaces }
    private struct Surface: Decodable { let components: [Component]? }
    private struct Component: Decodable { let component: String? }

    init(reply: String?, approval: CoachApproval? = nil, wantsMetricsForm: Bool = false) {
        self.reply = reply; self.approval = approval; self.wantsMetricsForm = wantsMetricsForm
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: Keys.self)
        reply = try container.decodeIfPresent(String.self, forKey: .reply)
        approval = try? container.decodeIfPresent(CoachApproval.self, forKey: .approval)
        let surfaces = (try? container.decodeIfPresent([Surface].self, forKey: .surfaces)) ?? []
        wantsMetricsForm = surfaces.contains { surface in
            (surface.components ?? []).contains { $0.component == "ProfileMetrics" }
        }
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

struct MetricsFieldValues: Equatable {
    var numbers: [String: Double] = [:]
    var texts: [String: String] = [:]
}
