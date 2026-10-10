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
    case text
}

struct MetricsField: Codable, Equatable {
    var key: String
    var label: String?
    var kind: MetricsFieldKind?
    var unit: String?
    var placeholder: String?
    var required: Bool?

    var isNumeric: Bool { (kind ?? .number) == .number }
    var displayLabel: String { label ?? Self.defaultLabel(for: key) }

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

    static let standard: [MetricsField] = [
        MetricsField(key: "height_cm", label: "Height", kind: .number, unit: "cm", placeholder: "178", required: true),
        MetricsField(key: "weight_kg", label: "Weight", kind: .number, unit: "kg", placeholder: "84", required: true),
        MetricsField(key: "goal_weight_kg", label: "Goal weight", kind: .number, unit: "kg", placeholder: "78", required: true),
        MetricsField(key: "age", label: "Age", kind: .number, unit: nil, placeholder: "34", required: false),
        MetricsField(key: "activity_level", label: "Activity level", kind: .text, unit: nil, placeholder: "moderate", required: false),
    ]
}

struct MetricsFieldValues: Equatable {
    var numbers: [String: Double] = [:]
    var texts: [String: String] = [:]
}
