import Foundation
import SwiftUI

struct ChatMessage: Identifiable {
    enum Role { case user, coach }
    let id = UUID()
    let role: Role
    let text: String
    var quickReplies: [String] = []
    var surfaces: [CoachSurface] = []
}

struct PendingTurn {
    let id: UUID
    let message: String
}

@MainActor
final class AppStore: ObservableObject {
    @Published var isSignedIn = false
    @Published var isValidating = false
    @Published var isLoading = false
    @Published var isSending = false
    @Published var isMutating = false
    @Published var day: DayPayload?
    @Published var presets: [Preset] = []
    @Published var trends: TrendsPayload?
    @Published var messages: [ChatMessage] = []
    @Published var errorMessage: String?
    @Published var retryTurn: PendingTurn?
    @Published var scanQueue: [(UUID, String)] = []
    @Published var isFixture = false
    @Published var suggestions: [MealSuggestion] = []
    @Published var needsSetup = false
    private(set) var sessionID = UUID()
    private var chatDay: String?
    private let journal: ChatJournal
    var now: () -> Date = { Date() }
    private let vault: TokenVault
    private var api: APIClient
    var candidateClient: (@escaping () -> String?) -> APIClient = { APIClient(tokenProvider: $0) }
    private var authGeneration = UUID()
    private var scanEvents = Set<UUID>()
    @Published private(set) var conflictBlocked = false
    var effectiveDate: String? { day?.dayTiming?.effectiveDate }

    init(api: APIClient? = nil, vault: TokenVault = DeviceTokenVault(), journal: ChatJournal = FileChatJournal()) {
        self.vault = vault
        self.journal = journal
        self.api = api ?? APIClient(tokenProvider: { vault.read() })
    }

    var chatDayKey: String {
        let policy = day?.dayTiming ?? LoggingDayPolicy.bootstrap
        return policy.formatter.string(from: policy.currentDay(now: now()))
    }

    func restoreChat() {
        let today = chatDayKey
        guard let saved = journal.load(), saved.day == today else {
            journal.clear(); startFreshChat(day: today); return
        }
        sessionID = saved.sessionID; chatDay = today
        messages = saved.lines.map { ChatMessage(role: $0.fromUser ? .user : .coach, text: $0.text) }
    }

    func rollChatIfNewDay() {
        let today = chatDayKey
        guard let current = chatDay else { chatDay = today; return }
        guard current != today, !isSending else { return }
        journal.clear(); startFreshChat(day: today)
    }

    private func startFreshChat(day: String) {
        sessionID = UUID(); chatDay = day
        messages = []; retryTurn = nil; conflictBlocked = false
    }

    private func persistChat() {
        guard !isFixture, let day = chatDay else { return }
        let lines = messages.suffix(SavedChat.maxLines).map { SavedChat.Line(fromUser: $0.role == .user, text: $0.text) }
        journal.save(SavedChat(day: day, sessionID: sessionID, lines: Array(lines)))
    }

    func checkSetup() async {
        let generation = authGeneration
        do { let result = try await api.profile(); if generation == authGeneration { needsSetup = result.needsSetup } }
        catch { if generation == authGeneration { needsSetup = false } }
    }

    func saveProfile(_ answers: ProfileAnswers) async -> Bool {
        guard !isMutating else { return false }
        let generation = authGeneration
        isMutating = true; errorMessage = nil; defer { isMutating = false }
        do {
            let result = try await api.saveProfile(answers)
            guard generation == authGeneration else { return false }
            needsSetup = result.needsSetup
            await refreshToday()
            return true
        } catch { if generation == authGeneration { handle(error) }; return false }
    }

    func loadSuggestions() async {
        let generation = authGeneration
        do { let result = try await api.mealSuggestions(); if generation == authGeneration { suggestions = result.suggestions } }
        catch { if generation == authGeneration { suggestions = [] } }
    }

    func saveSuggestion(_ item: MealSuggestion) async -> Bool {
        guard !isMutating else { return false }
        let generation = authGeneration
        isMutating = true; errorMessage = nil; defer { isMutating = false }
        do {
            let result = try await api.saveMealAsPreset(item.entryId)
            guard generation == authGeneration else { return false }
            presets = result.presets
            suggestions.removeAll { $0.entryId == item.entryId }
            return true
        } catch { if generation == authGeneration { handle(error) }; return false }
    }

    func restore() async {
        guard vault.read() != nil else { return }
        await refreshToday()
        if day != nil { isSignedIn = true; restoreChat(); await checkSetup(); await loadPresets(); await loadSuggestions() }
    }

    func signIn(_ raw: String) async -> Bool {
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty, !isValidating else { return false }
        isValidating = true; errorMessage = nil
        defer { isValidating = false }
        // Validation uses the entered value in memory. Persist only after success.
        let candidate = candidateClient({ token })
        do {
            let payload = try await candidate.today()
            try validateDay(payload)
            guard vault.save(token) else { throw APIError(status: 0, message: "Could not store the token securely. Try again.") }
            api = candidateClient({ [vault] in vault.read() })
            authGeneration = UUID(); day = payload; isSignedIn = true
            journal.clear(); startFreshChat(day: chatDayKey)
            await checkSetup(); await loadPresets(); await loadSuggestions()
            return true
        } catch {
            handle(error)
            return false
        }
    }

    func signInWithApple(identityToken: String, nonce: String, name: String?) async -> Bool {
        guard !isValidating else { return false }
        isValidating = true; errorMessage = nil
        defer { isValidating = false }
        do {
            let signedIn = try await api.signInWithApple(AppleSignInBody(identityToken: identityToken, nonce: nonce, name: name))
            isValidating = false
            return await signIn(signedIn.token)
        } catch {
            handle(error)
            return false
        }
    }

    func signOutEverywhere() async {
        await api.signOutDevice()
        signOut()
    }

    func signOut() {
        authGeneration = UUID(); vault.clear(); journal.clear()
        isSignedIn = false; day = nil; presets = []; trends = nil; suggestions = []; needsSetup = false
        sessionID = UUID(); chatDay = nil
        messages = []; retryTurn = nil; scanQueue = []; scanEvents = []
        conflictBlocked = false; errorMessage = nil
    }

    private func validateDay(_ payload: DayPayload) throws {
        guard let timing = payload.dayTiming, timing.isValid else { throw PayloadError.invalidPayload }
    }

    func refreshToday() async {
        let generation = authGeneration
        isLoading = true; defer { isLoading = false }
        do {
            let result = try await api.today(); try validateDay(result)
            guard generation == authGeneration else { return }
            day = result
        } catch { if generation == authGeneration { handle(error) } }
    }

    func loadPresets() async {
        let generation = authGeneration
        do { let result = try await api.presets(); if generation == authGeneration { presets = result.presets } }
        catch { if generation == authGeneration { handle(error) } }
    }

    func loadTrends(days: Int) async {
        let generation = authGeneration
        trends = nil
        do { let result = try await api.trends(days: days); if generation == authGeneration { trends = result } }
        catch { if generation == authGeneration { handle(error) } }
    }

    func logMeal(_ body: LogMealBody) async -> Bool {
        await mutate { _ = try await self.api.logMeal(body) }
    }
    func logPreset(_ body: LogPresetBody) async -> Bool {
        await mutate { _ = try await self.api.logPreset(body) }
    }
    func deleteMeal(_ id: String) async -> Bool {
        await mutate { _ = try await self.api.deleteMeal(id) }
    }
    func repeatMeal(_ entry: FoodEntry) async -> Bool {
        await mutate { _ = try await self.api.repeatMeal(entry.id) }
    }
    func saveMealAsPreset(_ entry: FoodEntry) async -> Bool {
        guard !isMutating else { return false }
        let generation = authGeneration
        isMutating = true; errorMessage = nil; defer { isMutating = false }
        do {
            let result = try await api.saveMealAsPreset(entry.id)
            guard generation == authGeneration else { return false }
            presets = result.presets
            return true
        } catch { if generation == authGeneration { handle(error) }; return false }
    }
    func editPreset(_ body: EditPresetBody) async -> Bool {
        await presetChange { try await self.api.editPreset(body) }
    }
    func removePreset(_ preset: Preset) async -> Bool {
        await presetChange { try await self.api.removePreset(RemovePresetBody(name: preset.name)) }
    }
    private func presetChange(_ work: () async throws -> PresetsPayload) async -> Bool {
        guard !isMutating else { return false }
        let generation = authGeneration
        isMutating = true; errorMessage = nil; defer { isMutating = false }
        do {
            let result = try await work()
            guard generation == authGeneration else { return false }
            presets = result.presets
            return true
        } catch { if generation == authGeneration { handle(error) }; return false }
    }
    func isSavedMeal(_ entry: FoodEntry) -> Bool {
        presets.contains { $0.name.trimmingCharacters(in: .whitespaces).lowercased() == entry.name.trimmingCharacters(in: .whitespaces).lowercased() }
    }
    private func mutate(_ work: () async throws -> Void) async -> Bool {
        guard !isMutating else { return false }
        let generation = authGeneration
        isMutating = true; errorMessage = nil; defer { isMutating = false }
        do {
            try await work()
            guard generation == authGeneration else { return false }
            await refreshToday()
            return true
        } catch { if generation == authGeneration { handle(error) }; return false }
    }

    func send(_ message: String) async {
        guard !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !isSending, retryTurn == nil, !conflictBlocked else { return }
        await perform(PendingTurn(id: UUID(), message: message), appendUser: true)
        await drainScans()
    }
    func sendQuickReply(_ label: String) async {
        guard messages.last?.quickReplies.contains(label) == true else { return }
        await send(label)
    }
    func retryLastTurn() async {
        guard let turn = retryTurn, !isSending else { return }
        await perform(turn, appendUser: false)
        await drainScans()
    }
    func abandonRetry() { retryTurn = nil }

    func resendAsNewTurn() async {
        guard let turn = retryTurn, !isSending else { return }
        retryTurn = nil
        await send(turn.message)
    }

    func continueAfterConflict() {
        // Explicit user continuation only. Never replay the conflicted turn.
        conflictBlocked = false
    }

    private func perform(_ turn: PendingTurn, appendUser: Bool) async {
        if retryTurn == nil { rollChatIfNewDay() }
        let generation = authGeneration
        isSending = true; retryTurn = nil
        defer { persistChat() }
        // Any later user turn invalidates earlier preview buttons.
        for index in messages.indices { messages[index].quickReplies = [] }
        if appendUser { messages.append(ChatMessage(role: .user, text: turn.message)) }
        defer { isSending = false }
        do {
            let result = try await api.turn(sessionID: sessionID, turnID: turn.id, message: turn.message)
            guard generation == authGeneration else { return }
            messages.append(ChatMessage(role: .coach, text: result.reply ?? "", quickReplies: result.quickReplies, surfaces: result.surfaces))
            // The fixed envelope has no saved flag. Refresh every successful reply.
            await refreshToday()
        } catch {
            guard generation == authGeneration else { return }
            if let apiError = error as? APIError, apiError.status == 401 { signOut(); errorMessage = "That token didn't work"; return }
            if let apiError = error as? APIError, apiError.status == 409 { conflictBlocked = true }
            if error is URLError { retryTurn = turn }
            messages.append(ChatMessage(role: .coach, text: chatError(error)))
        }
    }

    private func chatError(_ error: Error) -> String {
        if error is URLError { return "Connection interrupted. Retry the same message safely, or leave it pending. The server may still be working." }
        if error is PayloadError { return "This reply could not be read. Nothing was confirmed by the app." }
        return error.localizedDescription
    }

    func acceptScan(_ code: String, eventID: UUID) {
        guard (8...14).contains(code.utf8.count), code.utf8.allSatisfy({ (48...57).contains($0) }),
              scanEvents.insert(eventID).inserted else { return }
        scanQueue.append((eventID, code))
    }
    func drainScans() async {
        guard !isSending, retryTurn == nil, !conflictBlocked else { return }
        while !scanQueue.isEmpty && retryTurn == nil && !conflictBlocked {
            let (_, digits) = scanQueue.removeFirst()
            await perform(PendingTurn(id: UUID(), message: digits), appendUser: true)
        }
    }

    private func handle(_ error: Error) {
        if let apiError = error as? APIError, apiError.status == 401 { signOut() }
        errorMessage = error is URLError ? "The server is waking up or can't be reached. Give it about a minute, then try again." : error.localizedDescription
    }

    #if DEBUG
    func installOfflineFixtures() {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [OfflineFixtureProtocol.self]
        api = APIClient(baseURL: URL(string: "https://offline-fixtures.invalid"), tokenProvider: { "noncredential-fixture" }, session: URLSession(configuration: config))
        isFixture = true
        isSignedIn = true
        Task { await refreshToday(); await loadPresets() }
    }
    #endif
}

func nutrientText(_ value: Double?) -> String {
    guard let value else { return "Unknown" }
    return "\(value.formatted(.number.precision(.fractionLength(0...1))))g"
}
