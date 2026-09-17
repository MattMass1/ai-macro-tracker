import Foundation

// App-owned wire types. The renderer never receives unvalidated network JSON.
enum AgentCanvasError: LocalizedError, Equatable {
    case invalidPayload, invalidAction, wrongSession, unavailable
    var errorDescription: String? {
        switch self {
        case .invalidPayload: return "This task could not be displayed. Your standard screens are still available."
        case .invalidAction: return "That control is no longer available. Refresh the task and try again."
        case .wrongSession: return "This update belongs to an earlier session."
        case .unavailable: return "The agent is unavailable. Your standard screens are still available."
        }
    }
}

enum AgentComponentKind: String, Codable, CaseIterable {
    case dailyStatus = "DailyStatus", macroProgress = "MacroProgress", mealReceipt = "MealReceipt"
    case foodClarification = "FoodClarification", workoutOverview = "WorkoutOverview"
    case activeExercise = "ActiveExercise", setLogger = "SetLogger", restTimer = "RestTimer"
    case weeklyTrend = "WeeklyTrend", confirmationCard = "ConfirmationCard", agentMessage = "AgentMessage"
    case setupChecklist = "SetupChecklist", profileMetrics = "ProfileMetrics"
    case targetStatus = "TargetStatus", workoutPlanPreview = "WorkoutPlanPreview"
}

enum AgentLifecycle: String, Codable { case transient, task, pinned, approval }
enum AgentAction: String, Codable {
    case showMacros = "show_macros", startWorkout = "start_workout", showProgress = "show_progress"
    case nextExercise = "next_exercise", selectExercise = "select_exercise", openSetLogger = "open_set_logger"
    case exerciseLogged = "exercise_logged", confirm, cancel, dismiss, quiet, refresh
    case completeWorkout = "complete_workout"
    case showSetup = "show_setup", previewPlan = "preview_plan"
    case submitMetrics = "submit_metrics", submitTargets = "submit_targets"
}

struct AgentIntent: Codable, Equatable {
    var action: AgentAction
    var reference: String? = nil
    var surfaceId: String? = nil
    var numbers: [String: Double]? = nil
}

struct AgentTurnIdentity {
    private var pending: (message: String, id: String)?
    mutating func id(for message: String) -> String {
        if let pending, pending.message == message { return pending.id }
        let id = UUID().uuidString.lowercased()
        pending = (message, id)
        return id
    }
    mutating func acknowledge(_ id: String) {
        if pending?.id == id { pending = nil }
    }
}

struct AgentComponent: Codable, Identifiable, Equatable {
    struct Row: Codable, Equatable {
        let label: String
        let detail: String
        enum CodingKeys: String, CodingKey { case label, detail }
        init(from decoder: Decoder) throws {
            try CanvasValidation.keys(decoder, allowed: ["label", "detail"])
            let c = try decoder.container(keyedBy: CodingKeys.self)
            label = try c.decode(String.self, forKey: .label)
            detail = try c.decode(String.self, forKey: .detail)
        }
    }
    var id: String
    var component: AgentComponentKind
    var title: String? = nil
    var text: String? = nil
    var reference: String? = nil
    var actions: [AgentIntent] = []
    var rows: [Row] = []
}

struct AgentSurface: Codable, Identifiable, Equatable {
    var surfaceId: String
    var lifecycle: AgentLifecycle
    var components: [AgentComponent]
    var expiresAt: Double? = nil
    var id: String { surfaceId }
}

struct AgentWorkout: Codable, Equatable {
    struct Exercise: Codable, Identifiable, Equatable {
        var id: String
        var name: String
        var sets: Int
        var reps: String
        var restSec: Int
    }
    var date: String
    var type: String
    var done: Bool
    var exercises: [Exercise]
    var activeExerciseId: String?
    var loggedSets: [String: Int]
    var restEndsAt: Double?
    var activeExercise: Exercise? { exercises.first { $0.id == activeExerciseId } }
}

struct AgentApproval: Codable, Identifiable, Equatable {
    enum Status: String, Codable { case pending, uncertain }
    var id: String
    var title: String
    var detail: String
    var status: Status
}

struct AgentMealReceipt: Codable, Equatable {
    var operationId: String
    var recordId: String
    var label: String
}

struct AgentCanvasEnvelope: Codable, Equatable {
    static let version = "mmacros.canvas.v1"
    static let catalogId = "mmacros.native.v1"
    var protocolVersion: String
    var catalog: String
    var sessionId: String
    var instanceId: String
    var revision: Int
    var serverTime: Double
    var surfaces: [AgentSurface]
    var workout: AgentWorkout? = nil
    var approval: AgentApproval? = nil
    var receipt: AgentMealReceipt? = nil
    var reply: String? = nil

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol"
        case catalog, sessionId, instanceId, revision, serverTime, surfaces, workout, approval, receipt, reply
    }

    static func decode(_ data: Data) throws -> Self {
        guard data.count <= 48_000 else { throw AgentCanvasError.invalidPayload }
        let value = try JSONDecoder().decode(Self.self, from: data)
        try value.validate()
        return value
    }

    func validate() throws {
        guard protocolVersion == Self.version, catalog == Self.catalogId,
              UUID(uuidString: sessionId)?.uuidString.lowercased() == sessionId,
              UUID(uuidString: instanceId)?.uuidString.lowercased() == instanceId,
              revision >= 0, serverTime.isFinite, serverTime >= 0, surfaces.count <= 4,
              Set(surfaces.map(\.surfaceId)).count == surfaces.count,
              (reply?.count ?? 0) <= 1000 else { throw AgentCanvasError.invalidPayload }
        for surface in surfaces {
            guard ["task", "approval", "receipt", "message"].contains(surface.surfaceId),
                  surface.lifecycle != .pinned,
                  (surface.surfaceId == "approval") == (surface.lifecycle == .approval),
                  !surface.components.isEmpty, surface.components.count <= 8,
                  Set(surface.components.map(\.id)).count == surface.components.count else {
                throw AgentCanvasError.invalidPayload
            }
            if let expiry = surface.expiresAt {
                guard expiry.isFinite, surface.lifecycle == .transient else { throw AgentCanvasError.invalidPayload }
            }
            for item in surface.components {
                guard CanvasValidation.validID(item.id), item.id != "root", item.component != .dailyStatus,
                      (item.title?.count ?? 0) <= 1000, (item.text?.count ?? 0) <= 1000,
                      item.actions.count <= 8, item.rows.count <= 24,
                      item.rows.allSatisfy({ $0.label.count <= 240 && $0.detail.count <= 240 }) else { throw AgentCanvasError.invalidPayload }
                if let reference = item.reference, !CanvasValidation.validID(reference) { throw AgentCanvasError.invalidPayload }
                for action in item.actions { try action.validateShape() }
            }
        }
        if let workout {
            guard CanvasValidation.validWorkoutLabel(workout.type),
                  workout.date.range(of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression) != nil,
                  workout.exercises.count <= 20,
                  Set(workout.exercises.map(\.id)).count == workout.exercises.count,
                  workout.loggedSets.count <= 40 else { throw AgentCanvasError.invalidPayload }
            for exercise in workout.exercises {
                guard CanvasValidation.validID(exercise.id), !exercise.name.isEmpty, exercise.name.count <= 160,
                      (0...10).contains(exercise.sets), (0...3600).contains(exercise.restSec),
                      exercise.reps.count <= 80 else { throw AgentCanvasError.invalidPayload }
            }
            if let active = workout.activeExerciseId, !workout.exercises.contains(where: { $0.id == active }) {
                throw AgentCanvasError.invalidPayload
            }
            if let rest = workout.restEndsAt, !rest.isFinite { throw AgentCanvasError.invalidPayload }
            guard workout.loggedSets.values.allSatisfy({ (0...1000).contains($0) }) else { throw AgentCanvasError.invalidPayload }
        }
        if let approval {
            guard CanvasValidation.validID(approval.id), approval.title.count <= 160, approval.detail.count <= 1000,
                  surfaces.contains(where: { $0.lifecycle == .approval }) else { throw AgentCanvasError.invalidPayload }
        } else if surfaces.contains(where: { $0.lifecycle == .approval }) {
            throw AgentCanvasError.invalidPayload
        }
        if let receipt {
            guard CanvasValidation.validID(receipt.operationId), CanvasValidation.validID(receipt.recordId),
                  receipt.label.count <= 240 else { throw AgentCanvasError.invalidPayload }
        }
    }
}

// Deliberately small value reducer: no navigation history disguised as surfaces.
struct AgentSurfaceState {
    let sessionId: String
    private(set) var envelope: AgentCanvasEnvelope?
    private(set) var transientDeadlines: [String: TimeInterval] = [:]
    private var restDeadline: TimeInterval?

    init(sessionId: String) { self.sessionId = sessionId }

    @discardableResult
    mutating func apply(_ value: AgentCanvasEnvelope, authoritativeRefresh: Bool = false, at uptime: TimeInterval = ProcessInfo.processInfo.systemUptime) throws -> Bool {
        try value.validate()
        guard value.sessionId == sessionId else { throw AgentCanvasError.wrongSession }
        if let existing = envelope {
            if value.instanceId != existing.instanceId {
                // Only a serialized, authenticated GET may adopt a recreated
                // server session. Late voice/turn events cannot revive approvals.
                guard authoritativeRefresh else { throw AgentCanvasError.wrongSession }
                envelope = nil
                transientDeadlines = [:]
                restDeadline = nil
            } else if value.revision <= existing.revision { return false }
        }
        var deadlines: [String: TimeInterval] = [:]
        for surface in value.surfaces {
            if let expiry = surface.expiresAt {
                let old = envelope?.surfaces.first { $0.surfaceId == surface.surfaceId }
                if old?.expiresAt == expiry, old?.components == surface.components,
                   let retained = transientDeadlines[surface.surfaceId] {
                    deadlines[surface.surfaceId] = retained
                } else {
                    // Server-relative TTL plus a bounded first-delivery margin;
                    // device wall-clock skew cannot hide a committed receipt.
                    deadlines[surface.surfaceId] = uptime + min(30, max(10, expiry - value.serverTime))
                }
            }
        }
        transientDeadlines = deadlines
        if let expiry = value.workout?.restEndsAt {
            if envelope?.workout?.restEndsAt != expiry || restDeadline == nil {
                restDeadline = uptime + min(3600, max(0, expiry - value.serverTime))
            }
        } else { restDeadline = nil }
        envelope = value
        return true
    }

    func visibleSurfaces(at uptime: TimeInterval = ProcessInfo.processInfo.systemUptime) -> [AgentSurface] {
        (envelope?.surfaces ?? []).filter { (transientDeadlines[$0.surfaceId] ?? .infinity) > uptime }
    }

    func restRemaining(at uptime: TimeInterval = ProcessInfo.processInfo.systemUptime) -> TimeInterval {
        max(0, (restDeadline ?? uptime) - uptime)
    }

    mutating func dismiss(_ id: String) throws {
        guard id != "approval", envelope?.surfaces.contains(where: { $0.surfaceId == id }) == true else {
            throw AgentCanvasError.invalidAction
        }
        envelope?.surfaces.removeAll { $0.surfaceId == id }
        transientDeadlines.removeValue(forKey: id)
    }
}

enum AgentActionGateway {
    static func validate(_ intent: AgentIntent, envelope: AgentCanvasEnvelope?, authenticated: Bool) throws {
        guard authenticated else { throw AgentCanvasError.unavailable }
        try intent.validateShape()
        switch intent.action {
        case .confirm, .cancel:
            guard let approval = envelope?.approval, approval.id == intent.reference,
                  intent.action == .cancel || approval.status == .pending else { throw AgentCanvasError.invalidAction }
        case .selectExercise, .openSetLogger:
            guard let workout = envelope?.workout, !workout.done,
                  workout.exercises.contains(where: { $0.id == intent.reference }) else { throw AgentCanvasError.invalidAction }
        case .nextExercise:
            guard envelope?.workout?.activeExercise != nil, envelope?.workout?.done == false else { throw AgentCanvasError.invalidAction }
        case .completeWorkout:
            guard envelope?.workout != nil else { throw AgentCanvasError.invalidAction }
        case .dismiss:
            guard intent.surfaceId != "approval", envelope?.surfaces.contains(where: { $0.surfaceId == intent.surfaceId }) == true else {
                throw AgentCanvasError.invalidAction
            }
        case .exerciseLogged:
            guard intent.reference != nil, envelope?.workout != nil else { throw AgentCanvasError.invalidAction }
        case .showMacros, .startWorkout, .showProgress, .quiet, .refresh, .showSetup, .previewPlan: break
        case .submitMetrics, .submitTargets:
            guard envelope != nil, envelope?.approval == nil else { throw AgentCanvasError.invalidAction }
        }
    }
}

enum CanvasValidation {
    struct Key: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { return nil }
    }
    static func keys(_ decoder: Decoder, allowed: Set<String>) throws {
        let container = try decoder.container(keyedBy: Key.self)
        guard Set(container.allKeys.map(\.stringValue)).isSubset(of: allowed) else { throw AgentCanvasError.invalidPayload }
    }
    static func validID(_ text: String) -> Bool {
        text.range(of: #"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$"#, options: .regularExpression) != nil
    }
    static func validWorkoutLabel(_ text: String) -> Bool {
        // Same bounded display-label grammar as the server, not a split enum.
        text.range(of: #"\A[A-Za-z0-9][A-Za-z0-9 /&()+.'_\-]{0,79}\z"#, options: .regularExpression) != nil
    }
}

extension AgentIntent {
    private enum CodingKeys: String, CodingKey { case action, reference, surfaceId, numbers }
    init(from decoder: Decoder) throws {
        try CanvasValidation.keys(decoder, allowed: ["action", "reference", "surfaceId", "numbers"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        action = try c.decode(AgentAction.self, forKey: .action)
        reference = try c.decodeIfPresent(String.self, forKey: .reference)
        surfaceId = try c.decodeIfPresent(String.self, forKey: .surfaceId)
        numbers = try c.decodeIfPresent([String: Double].self, forKey: .numbers)
        try validateShape()
    }
    func validateShape() throws {
        if action == .submitMetrics || action == .submitTargets {
            let required: Set<String> = action == .submitMetrics ? ["height_cm", "weight_kg", "goal_weight_kg"] : ["calories", "protein", "carbs", "fat", "fiber"]
            let allowed = action == .submitMetrics ? required.union(["age"]) : required
            guard let numbers, required.isSubset(of: Set(numbers.keys)), Set(numbers.keys).isSubset(of: allowed),
                  numbers.values.allSatisfy({ $0.isFinite && $0 >= 0 }) else { throw AgentCanvasError.invalidAction }
        } else if numbers != nil { throw AgentCanvasError.invalidAction }
        if let reference, !CanvasValidation.validID(reference) { throw AgentCanvasError.invalidAction }
        if let surfaceId, !CanvasValidation.validID(surfaceId) { throw AgentCanvasError.invalidAction }
    }
}

extension AgentComponent {
    private enum CodingKeys: String, CodingKey { case id, component, title, text, reference, actions, rows }
    init(from decoder: Decoder) throws {
        try CanvasValidation.keys(decoder, allowed: ["id", "component", "title", "text", "reference", "actions", "rows"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        component = try c.decode(AgentComponentKind.self, forKey: .component)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        text = try c.decodeIfPresent(String.self, forKey: .text)
        reference = try c.decodeIfPresent(String.self, forKey: .reference)
        actions = try c.decodeIfPresent([AgentIntent].self, forKey: .actions) ?? []
        rows = try c.decodeIfPresent([Row].self, forKey: .rows) ?? []
    }
}

extension AgentSurface {
    private enum CodingKeys: String, CodingKey { case surfaceId, lifecycle, components, expiresAt }
    init(from decoder: Decoder) throws {
        try CanvasValidation.keys(decoder, allowed: ["surfaceId", "lifecycle", "components", "expiresAt"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        surfaceId = try c.decode(String.self, forKey: .surfaceId)
        lifecycle = try c.decode(AgentLifecycle.self, forKey: .lifecycle)
        components = try c.decode([AgentComponent].self, forKey: .components)
        expiresAt = try c.decodeIfPresent(Double.self, forKey: .expiresAt)
    }
}

extension AgentCanvasEnvelope {
    init(from decoder: Decoder) throws {
        try CanvasValidation.keys(decoder, allowed: ["protocol", "catalog", "sessionId", "instanceId", "revision", "serverTime", "surfaces", "workout", "approval", "receipt", "reply"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        protocolVersion = try c.decode(String.self, forKey: .protocolVersion)
        catalog = try c.decode(String.self, forKey: .catalog)
        sessionId = try c.decode(String.self, forKey: .sessionId)
        instanceId = try c.decode(String.self, forKey: .instanceId)
        revision = try c.decode(Int.self, forKey: .revision)
        serverTime = try c.decode(Double.self, forKey: .serverTime)
        surfaces = try c.decode([AgentSurface].self, forKey: .surfaces)
        workout = try c.decodeIfPresent(AgentWorkout.self, forKey: .workout)
        approval = try c.decodeIfPresent(AgentApproval.self, forKey: .approval)
        receipt = try c.decodeIfPresent(AgentMealReceipt.self, forKey: .receipt)
        reply = try c.decodeIfPresent(String.self, forKey: .reply)
        try validate()
    }
}
