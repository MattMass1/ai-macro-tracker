import Foundation

/// The classic Coach chat's connection to the agent: one turn in, one reply
/// out, on the same server session the app has used all along. There is no
/// canvas state here -- the feed owns its messages; this store owns only the
/// session identity and the two calls.
@MainActor
final class CoachChatStore: ObservableObject {
    static let sessionKey = "mmacros.chat.session.v1"

    private let api: APIClient
    private let defaults: UserDefaults
    @Published private(set) var sessionId: String

    init(api: APIClient, defaults: UserDefaults = .standard) {
        self.api = api
        self.defaults = defaults
        sessionId = Self.storedSession(api: api, defaults: defaults)
    }

    /// Sends one message and returns the agent's turn. The server keeps
    /// sessions in memory, so a deploy forgets them; an "unknown session"
    /// rejection mints a fresh id and retries exactly once.
    func send(_ text: String) async throws -> CoachTurn {
        do {
            return try await api.canvasTurn(sessionId: sessionId, turnId: Self.newId(), message: text)
        } catch let error as APIError where error.status == 400 && error.message.lowercased().contains("session") {
            sessionId = Self.rotateSession(scope: Self.scope(api), defaults: defaults)
            return try await api.canvasTurn(sessionId: sessionId, turnId: Self.newId(), message: text)
        }
    }

    /// Confirms or cancels a pending workout change by its approval id.
    func respond(approval id: String, confirm: Bool) async throws -> CoachTurn {
        try await api.canvasAction(sessionId: sessionId,
                                   intent: CoachIntent(action: confirm ? "confirm" : "cancel", reference: id))
    }

    func history(limit: Int = 100) async throws -> [ChatHistoryMessage] {
        try await api.chatHistory(limit: limit)
    }

    /// Sign-out: the next user must never continue this session.
    func reset() {
        sessionId = Self.rotateSession(scope: Self.scope(api), defaults: defaults)
    }

    // MARK: - Session identity (one per credential + server origin)

    private static func scope(_ api: APIClient) -> String { api.canvasIdentityScope ?? "anonymous" }

    private static func newId() -> String { UUID().uuidString.lowercased() }

    private static func storedSession(api: APIClient, defaults: UserDefaults) -> String {
        let scope = scope(api)
        if let stored = defaults.dictionary(forKey: sessionKey) as? [String: String],
           let id = stored[scope], UUID(uuidString: id) != nil {
            return id
        }
        return rotateSession(scope: scope, defaults: defaults)
    }

    @discardableResult
    private static func rotateSession(scope: String, defaults: UserDefaults) -> String {
        let id = newId()
        var stored = defaults.dictionary(forKey: sessionKey) as? [String: String] ?? [:]
        stored[scope] = id
        defaults.set(stored, forKey: sessionKey)
        return id
    }
}
