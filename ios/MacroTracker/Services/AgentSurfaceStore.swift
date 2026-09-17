import Foundation

/// Only a random session UUID and opaque credential/backend scope survive launch.
/// No envelopes, drafts, approvals, tokens or pending write payloads go to disk.
final class AgentSessionIdentity {
    private let defaults: UserDefaults
    private let key = "mmacros.canvas.session.v1"
    init(defaults: UserDefaults = .standard) { self.defaults = defaults }
    func session(for scope: String?, fresh: Bool = false) -> String {
        guard let scope else { clear(); return UUID().uuidString.lowercased() }
        if !fresh, let saved = defaults.dictionary(forKey: key), saved["scope"] as? String == scope,
           let id = saved["id"] as? String, UUID(uuidString: id)?.uuidString.lowercased() == id { return id }
        let id = UUID().uuidString.lowercased()
        defaults.set(["scope": scope, "id": id], forKey: key)
        return id
    }
    func clear() { defaults.removeObject(forKey: key) }
}

struct AgentLoggerSelection: Identifiable {
    let id: String
    let exercise: String
    let type: String
}

/// Owned by AppStore, not by a transient View. Auto-lock and redraws do not
/// discard the active exercise, draft composer or pending approval.
@MainActor
final class AgentSurfaceStore: ObservableObject {
    @Published private(set) var state: AgentSurfaceState
    @Published private(set) var busy = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var recoveryNotice: String?
    @Published var draft = ""
    @Published var logger: AgentLoggerSelection?
    @Published var showsWorkouts = false
    @Published var showsMeals = false
    @Published var showsProgress = false
    @Published var showsLegacyCoach = false
    @Published private(set) var voice: LiveCoachController
    private let api: APIClient
    private let identity: AgentSessionIdentity
    private var identityScope: String?
    private let authenticated: () -> Bool
    private var generation = UUID()
    private var pendingLegacyCoach = false
    private var turnIdentity = AgentTurnIdentity()
    private var swapTurnIdentity = AgentTurnIdentity()
    private weak var app: AppStore?
    private var expiryTask: Task<Void, Never>?
    private(set) var instanceRecoveryTask: Task<Void, Never>?
    private var instanceRecoveryID: UUID?
    var sessionId: String { state.sessionId }
    var envelope: AgentCanvasEnvelope? { state.envelope }

    init(api: APIClient = .shared, authenticated: @escaping () -> Bool = { KeychainStore.deviceToken?.isEmpty == false },
         identity: AgentSessionIdentity = AgentSessionIdentity()) {
        self.api = api
        self.authenticated = authenticated
        self.identity = identity
        identityScope = api.canvasIdentityScope
        let sessionId = identity.session(for: api.canvasIdentityScope)
        state = AgentSurfaceState(sessionId: sessionId)
        voice = Self.makeVoice(sessionId)
        connectVoice()
    }

    private static func makeVoice(_ sessionId: String) -> LiveCoachController {
        LiveCoachController(permission: SystemMicrophonePermission(),
            transport: LiveCoachWebSocketTransport(canvasSessionId: sessionId), audio: LiveCoachAudioEngine())
    }

    private func connectVoice() {
        let voiceGeneration = generation
        voice.onCanvas = { [weak self] snapshot in
            guard let self, self.generation == voiceGeneration else { return }
            do {
                if try self.receive(snapshot) {
                    Task { [weak self] in await self?.reloadData() }
                }
            } catch { self.fail(error) }
        }
    }

    func attach(_ app: AppStore) { self.app = app }

    func handleLegacyDestination(_ destination: Int) -> Int {
        if destination == 1 {
            pendingLegacyCoach = true
            showsMeals = false
        }
        return 2 // TodayView only emits Coach (1); re-arm every tap.
    }

    func completeLegacyNavigation() {
        guard pendingLegacyCoach else { return }
        pendingLegacyCoach = false
        showsLegacyCoach = true
    }

    @discardableResult
    func receive(_ snapshot: AgentCanvasEnvelope, authoritativeRefresh: Bool = false) throws -> Bool {
        let recreated = envelope.map { $0.instanceId != snapshot.instanceId } ?? false
        let changed: Bool
        do {
            changed = try state.apply(snapshot, authoritativeRefresh: authoritativeRefresh)
        } catch {
            if !authoritativeRefresh, snapshot.sessionId == sessionId,
               recreated, (error as? AgentCanvasError) == .wrongSession {
                scheduleInstanceRecovery()
            }
            throw error
        }
        if changed && recreated {
            let previousVoice = voice
            previousVoice.onCanvas = nil
            Task { await previousVoice.end() }
            generation = UUID()
            busy = false
            logger = nil
            recoveryNotice = "Session refreshed. Old previews are unavailable. Check saved meals and workouts before repeating a request with an uncertain result."
            voice = Self.makeVoice(sessionId)
            connectVoice()
            // Retain any unacknowledged meal turn identity for explicit retry;
            // never send it, an approval, or a queued logger acknowledgement.
        }
        if changed {
            errorMessage = nil
            expiryTask?.cancel()
            expiryTask = Task { [weak self] in
                while !Task.isCancelled {
                    let now = ProcessInfo.processInfo.systemUptime
                    guard let next = self?.state.transientDeadlines.values.filter({ $0 > now }).min() else { return }
                    try? await Task.sleep(for: .seconds(min(next - now, 3600) + 0.01))
                    guard !Task.isCancelled else { return }
                    self?.objectWillChange.send()
                }
            }
        }
        return changed
    }

    private func scheduleInstanceRecovery() {
        guard instanceRecoveryTask == nil else { return }
        let current = generation
        let scope = identityScope
        let recoveryID = UUID()
        instanceRecoveryID = recoveryID
        instanceRecoveryTask = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.instanceRecoveryID == recoveryID {
                    self.instanceRecoveryTask = nil
                    self.instanceRecoveryID = nil
                }
            }
            // A mismatched POST reply is rejected while busy is still true.
            // Wait for its defer (or the in-flight voice/HTTP operation), then
            // perform exactly one authoritative GET, never a write retry.
            while self.busy {
                do { try await Task.sleep(for: .milliseconds(50)) } catch { return }
                guard !Task.isCancelled, current == self.generation else { return }
            }
            guard !Task.isCancelled, current == self.generation,
                  scope == self.api.canvasIdentityScope else { return }
            await self.refresh()
            guard !Task.isCancelled, scope == self.api.canvasIdentityScope else { return }
            await self.reloadData()
        }
    }

    @discardableResult
    private func synchronizeIdentity() -> Bool {
        guard api.canvasIdentityScope != identityScope else { return false }
        reset()
        identityScope = api.canvasIdentityScope
        let id = identity.session(for: api.canvasIdentityScope)
        state = AgentSurfaceState(sessionId: id)
        voice.onCanvas = nil
        voice = Self.makeVoice(id)
        connectVoice()
        return true
    }

    func refresh() async {
        synchronizeIdentity()
        guard !busy, authenticated() else { return }
        let current = generation
        busy = true
        defer { if current == generation { busy = false } }
        do {
            let value = try await api.canvasSnapshot(sessionId: sessionId)
            guard current == generation else { return }
            _ = try receive(value, authoritativeRefresh: true)
            errorMessage = nil
        } catch { if current == generation { fail(error) } }
    }

    func send() async {
        await sendMessage(draft, clearsComposer: true)
    }

    func requestSwap(_ replacement: String) async {
        let name = replacement.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name.count <= 160 else { return }
        await sendMessage("Replace this exercise with \(name)", clearsComposer: false)
    }

    private func sendMessage(_ text: String, clearsComposer: Bool) async {
        guard !synchronizeIdentity() else { return }
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !busy, instanceRecoveryTask == nil, authenticated(), !message.isEmpty, message.count <= 1000 else { return }
        let current = generation
        busy = true
        defer { if current == generation { busy = false } }
        do {
            let turnId = clearsComposer ? turnIdentity.id(for: message) : swapTurnIdentity.id(for: message)
            let value = try await api.canvasTurn(sessionId: sessionId, turnId: turnId, message: message)
            guard current == generation else { return }
            _ = try receive(value)
            if clearsComposer { turnIdentity.acknowledge(turnId) }
            else { swapTurnIdentity.acknowledge(turnId) }
            if clearsComposer && draft.trimmingCharacters(in: .whitespacesAndNewlines) == message { draft = "" }
            await reloadData()
        } catch {
            // Do not auto-retry a possibly committed write. Keep the user's text.
            if current == generation { fail(error) }
        }
    }

    func perform(_ intent: AgentIntent) async {
        guard !synchronizeIdentity() else { return }
        guard !busy, instanceRecoveryTask == nil else { return }
        let current = generation
        busy = true
        defer { if current == generation { busy = false } }
        do {
            try AgentActionGateway.validate(intent, envelope: envelope, authenticated: authenticated())
            if errorMessage != nil, [.confirm, .openSetLogger, .completeWorkout].contains(intent.action) {
                throw AgentCanvasError.unavailable
            }
            let value = try await api.canvasAction(sessionId: sessionId, intent: intent)
            guard current == generation else { return }
            _ = try receive(value)
            if intent.action == .openSetLogger, let exercise = envelope?.workout?.activeExercise,
               let workout = envelope?.workout {
                // Only type/name are prefilled. The established logger owns sets,
                // Fill Last Time, suggestions, validation and the original API.
                app?.selectCurrentDay()
                logger = AgentLoggerSelection(id: exercise.id, exercise: exercise.name, type: workout.type)
            }
            await reloadData()
        } catch {
            if current == generation {
                fail(error)
                // Readback after an uncertain approval can disable its Confirm
                // control. This GET never retries the write.
                if intent.action == .confirm, instanceRecoveryTask == nil, let value = try? await api.canvasSnapshot(sessionId: sessionId), current == generation {
                    _ = try? receive(value)
                    fail(error)
                }
            }
        }
    }

    func acknowledgeWorkout(_ recordId: String) async {
        // A logger can finish while a typed request is in flight. Queue this
        // readback rather than dropping a successfully saved set's UI update.
        let current = generation
        while busy {
            try? await Task.sleep(for: .milliseconds(100))
            guard !Task.isCancelled, current == generation else { return }
        }
        guard current == generation else { return }
        await perform(AgentIntent(action: .exerciseLogged, reference: recordId))
    }

    private func reloadData() async {
        if envelope?.surfaces.contains(where: { $0.components.contains(where: { $0.component == .macroProgress }) }) == true {
            app?.selectCurrentDay()
        }
        await app?.loadAll()
        if envelope?.surfaces.contains(where: { $0.components.contains(where: { $0.component == .weeklyTrend }) }) == true {
            await app?.trends(days: 7)
        }
    }

    func fail(_ error: Error) {
        errorMessage = (error as? APIError)?.errorDescription ?? (error as? AgentCanvasError)?.errorDescription
            ?? "This task could not be displayed. Check your saved data before retrying a write."
        #if DEBUG
        // Deliberately never print the rejected payload or conversation.
        print("[AgentCanvas] Rejected update or failed operation; standard screens remain available.")
        #endif
    }

    func startFreshSession() async {
        await voice.end()
        reset(keepDraft: true)
        await refresh()
    }

    func reset(keepDraft: Bool = false) {
        let previousVoice = voice
        previousVoice.onCanvas = nil
        Task { await previousVoice.end() }
        generation = UUID()
        turnIdentity = AgentTurnIdentity()
        swapTurnIdentity = AgentTurnIdentity()
        instanceRecoveryTask?.cancel()
        instanceRecoveryTask = nil
        instanceRecoveryID = nil
        expiryTask?.cancel()
        identity.clear()
        identityScope = keepDraft ? api.canvasIdentityScope : nil
        let id = identity.session(for: identityScope, fresh: true)
        state = AgentSurfaceState(sessionId: id)
        voice = Self.makeVoice(id)
        connectVoice()
        busy = false
        errorMessage = nil
        recoveryNotice = nil
        logger = nil
        pendingLegacyCoach = false
        showsWorkouts = false; showsMeals = false; showsProgress = false; showsLegacyCoach = false
        if !keepDraft { draft = "" }
    }
}
