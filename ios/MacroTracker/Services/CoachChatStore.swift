import Foundation

/// The one meal/workout conversation: session, feed, input and serialized sends.
/// Navigation never creates another transcript or abandons a pending shortcut.
@MainActor
final class CoachChatStore: ObservableObject {
    static let sessionKey = "mmacros.chat.session.v1"
    static let welcomeText = "Tell me what you ate, or ask about your workout. One chat for both."

    private let api: APIClient
    private let defaults: UserDefaults
    @Published private(set) var sessionId: String
    @Published private(set) var messages: [CoachMessage] = []
    @Published var input = ""
    @Published private(set) var isSending = false
    private struct QueuedMessage {
        let text: String
        let authentication: APIClient.AuthenticationContext
    }
    private var queuedMessages: [QueuedMessage] = []
    private var drainTask: Task<Bool, Never>?
    private var approvalTask: Task<Bool, Never>?
    private var generation = 0
    struct Context: Equatable { fileprivate let generation: Int }
    var context: Context { Context(generation: generation) }
    private var historyLoaded = false
    private var historyTask: Task<Void, Never>?
    /// Requests the agent has not answered yet. The companion writes in the
    /// journal exactly while this is above zero; a reply, cancel or error
    /// brings it back down, so the pen stops on real agent state, not a timer.
    @Published private(set) var inFlight = 0
    var isResponding: Bool { inFlight > 0 }

    init(api: APIClient, defaults: UserDefaults = .standard) {
        self.api = api
        self.defaults = defaults
        sessionId = Self.storedSession(api: api, defaults: defaults)
    }

    /// Sends one message and returns the agent's turn. The server keeps
    /// sessions in memory, so a deploy forgets them; an "unknown session"
    /// rejection mints a fresh id and retries exactly once.
    private func send(_ text: String, authentication: APIClient.AuthenticationContext) async throws -> CoachTurn {
        let requestGeneration = generation
        return try await tracked {
            do {
                return try await api.canvasTurn(sessionId: sessionId, turnId: Self.newId(), message: text, authentication: authentication)
            } catch let error as APIError where error.status == 400 && error.message.lowercased().contains("session") {
                guard requestGeneration == generation else { throw CancellationError() }
                sessionId = Self.rotateSession(scope: Self.scope(api), defaults: defaults)
                return try await api.canvasTurn(sessionId: sessionId, turnId: Self.newId(), message: text, authentication: authentication)
            }
        }
    }

    /// Confirms or cancels a pending workout change by its approval id.
    private func respond(approval id: String, confirm: Bool, authentication: APIClient.AuthenticationContext) async throws -> CoachTurn {
        try await tracked {
            try await api.canvasAction(sessionId: sessionId,
                                       intent: CoachIntent(action: confirm ? "confirm" : "cancel", reference: id), authentication: authentication)
        }
    }

    private func tracked<T>(_ operation: () async throws -> T) async rethrows -> T {
        let requestGeneration = generation
        inFlight += 1
        defer { if requestGeneration == generation { inFlight -= 1 } }
        return try await operation()
    }

    private func history(limit: Int = 100, authentication: APIClient.AuthenticationContext) async throws -> [ChatHistoryMessage] {
        try await api.chatHistory(limit: limit, authentication: authentication)
    }

    /// Sign-out: the next user must never continue this session.
    func reset() {
        generation += 1
        drainTask?.cancel()
        drainTask = nil
        approvalTask?.cancel()
        approvalTask = nil
        historyTask?.cancel()
        historyTask = nil
        historyLoaded = false
        queuedMessages = []
        messages = []
        input = ""
        isSending = false
        inFlight = 0
        sessionId = Self.rotateSession(scope: Self.scope(api), defaults: defaults)
    }

    /// One history load shared by all observers, fenced against sign-out.
    func loadHistory() async {
        if historyLoaded { return }
        if let historyTask { await historyTask.value; return }
        let requestGeneration = generation
        let authentication = api.authenticationContext()
        guard requestGeneration == generation else { return }
        let task = Task { @MainActor [weak self] in
            guard let self, requestGeneration == self.generation, !Task.isCancelled else { return }
            let history = try? await self.history(authentication: authentication)
            guard requestGeneration == self.generation, !Task.isCancelled else { return }
            if self.messages.isEmpty {
                self.messages = (history ?? []).compactMap { item in
                    switch item.role {
                    case "user": return CoachMessage(role: .user, text: item.content)
                    case "assistant": return CoachMessage(role: .assistant, text: item.content)
                    default: return nil
                    }
                }
                if self.messages.isEmpty {
                    self.messages = [CoachMessage(role: .assistant, text: Self.welcomeText)]
                }
            }
            self.historyLoaded = true
        }
        historyTask = task
        await task.value
        if requestGeneration == generation { historyTask = nil }
    }

    @discardableResult
    func sendMessage(_ rawText: String) async -> Bool {
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return false }
        let requestGeneration = generation
        let authentication = api.authenticationContext()
        guard requestGeneration == generation else { return false }
        queuedMessages.append(QueuedMessage(text: text, authentication: authentication))
        return await drainMessages()
    }

    /// The store owns this task. Cancelling a view's waiter cannot cancel it.
    private func drainMessages() async -> Bool {
        guard drainTask == nil, !isSending, !queuedMessages.isEmpty else { return false }
        let requestGeneration = generation
        isSending = true
        let task = Task { @MainActor [weak self] in
            guard let self else { return false }
            return await self.runDrainer(generation: requestGeneration)
        }
        drainTask = task
        return await task.value
    }

    private func runDrainer(generation requestGeneration: Int) async -> Bool {
        defer {
            if requestGeneration == generation {
                isSending = false
                drainTask = nil
            }
        }
        guard requestGeneration == generation, !Task.isCancelled else { return false }
        await loadHistory()
        var hasReply = false
        while requestGeneration == generation, !Task.isCancelled, !queuedMessages.isEmpty {
            let queued = queuedMessages.removeFirst()
            clearQuickReplies()
            messages.append(CoachMessage(role: .user, text: queued.text))
            do {
                let turn = try await send(queued.text, authentication: queued.authentication)
                guard requestGeneration == generation else { return false }
                append(turn)
                hasReply = true
            } catch {
                guard requestGeneration == generation else { return false }
                let text: String
                if error is CancellationError {
                    // The request may already have committed. Never replay it.
                    text = "That request was interrupted. Check your log before sending it again."
                } else if (error as? APIError)?.status == 429 {
                    text = "You're at today's coach limit."
                } else {
                    text = error.localizedDescription
                }
                messages.append(CoachMessage(role: .assistant, text: text))
            }
        }
        return requestGeneration == generation && hasReply
    }

    @discardableResult
    func respondToApproval(_ approval: CoachApproval, confirm: Bool, in messageID: UUID) async -> Bool {
        guard !isSending, approvalTask == nil,
              let message = messages.first(where: { $0.id == messageID }),
              message.approval?.id == approval.id, message.approval?.status != "resolved" else { return false }
        let requestGeneration = generation
        let authentication = api.authenticationContext()
        guard requestGeneration == generation else { return false }
        isSending = true
        let task = Task { @MainActor [weak self] in
            guard let self else { return false }
            return await self.performApproval(approval, confirm: confirm, messageID: messageID,
                                              generation: requestGeneration, authentication: authentication)
        }
        approvalTask = task
        return await task.value
    }

    private func performApproval(_ approval: CoachApproval, confirm: Bool, messageID: UUID,
                                 generation requestGeneration: Int, authentication: APIClient.AuthenticationContext) async -> Bool {
        guard requestGeneration == generation, !Task.isCancelled else { return false }
        var succeeded = false
        do {
            let turn = try await respond(approval: approval.id, confirm: confirm, authentication: authentication)
            guard requestGeneration == generation else { return false }
            if let index = messages.firstIndex(where: { $0.id == messageID }) {
                messages[index].approval = CoachApproval(id: approval.id, title: approval.title, detail: approval.detail, status: "resolved")
            }
            append(turn, fallback: confirm ? "Done. Your workout is updated." : "Cancelled. Nothing was changed.")
            succeeded = true
        } catch {
            guard requestGeneration == generation else { return false }
            let text = error is CancellationError ? "That request was interrupted. Check your workout before trying again." : error.localizedDescription
            messages.append(CoachMessage(role: .assistant, text: text))
        }
        approvalTask = nil
        isSending = false
        let drained = await drainMessages()
        return succeeded || drained
    }

    func recordLoggedMeal(name: String, calories: Double, context: Context) {
        guard context.generation == generation else { return }
        messages.append(CoachMessage(role: .assistant, text: "Logged \(name), \(Int(calories)) kcal."))
    }

    func dismissMetricsForm(_ id: UUID) {
        guard let index = messages.firstIndex(where: { $0.id == id }) else { return }
        messages[index].showsMetricsForm = false
    }

    private func clearQuickReplies() {
        for index in messages.indices { messages[index].quickReplies = [] }
    }

    private func append(_ turn: CoachTurn, fallback: String = "") {
        let text = turn.reply?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        messages.append(CoachMessage(role: .assistant, text: text.isEmpty ? fallback : text,
                                     approval: turn.approval?.status == "pending" || turn.approval?.status == nil ? turn.approval : nil,
                                     showsMetricsForm: turn.wantsMetricsForm, quickReplies: turn.quickReplies))
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
