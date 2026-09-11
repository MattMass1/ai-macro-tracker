import Foundation

enum LiveCoachState: Equatable {
    case ready
    case connecting
    case listening
    case speaking
    case failed(message: String, retryable: Bool, settingsAvailable: Bool = false)
    case ended
}

enum LiveCoachServerEvent: Equatable {
    case started
    case dataChanged
    case inputTranscript(String)
    case outputTranscript(String)
    case outputAudio(Data)
    case inputMuted(Bool)
    case closed(reason: String)
    case failure(code: String, message: String)
}

enum LiveCoachAudioLifecycleEvent: Equatable {
    case interrupted
    case routeChanged
}

struct LiveCoachConnectionError: Error, Equatable {
    let message: String
    let retryable: Bool
}

@MainActor
protocol LiveCoachPermissionChecking {
    func requestPermission() async -> Bool
}

@MainActor
protocol LiveCoachTransporting: AnyObject {
    /// Returns only after the server confirms `session.started`.
    func connect() async throws -> AsyncThrowingStream<LiveCoachServerEvent, Error>
    func sendAudio(_ data: Data) async throws
    func setMuted(_ muted: Bool) async throws
    func close() async
}

@MainActor
protocol LiveCoachAudioHandling: AnyObject {
    var capturedAudio: AsyncStream<Data> { get }
    var playbackActivity: AsyncStream<Bool> { get }
    var lifecycleEvents: AsyncStream<LiveCoachAudioLifecycleEvent> { get }
    func start() throws
    func play(_ data: Data) throws
    func setMuted(_ muted: Bool)
    func stop()
}

@MainActor
final class LiveCoachController: ObservableObject {
    @Published private(set) var state: LiveCoachState = .ready
    @Published private(set) var isMuted = false
    @Published private(set) var userCaption = ""
    @Published private(set) var coachCaption = ""

    private let permission: LiveCoachPermissionChecking
    private let transport: LiveCoachTransporting
    private let audio: LiveCoachAudioHandling
    private let refreshData: @MainActor () async -> Void
    private var refreshTask: Task<Void, Never>?
    private var refreshRequested = false
    private var sessionGeneration = UUID()
    private var sessionTasks: [Task<Void, Never>] = []
    private var closeTask: Task<Void, Never>?
    private var endTask: Task<Void, Never>?
    private var endTaskID: UUID?
    private var audioIsRunning = false
    private var transportIsOpen = false
    private var startAttemptInProgress = false
    private var startAttemptWaiters: [CheckedContinuation<Void, Never>] = []

    init(
        permission: LiveCoachPermissionChecking,
        transport: LiveCoachTransporting,
        audio: LiveCoachAudioHandling,
        refreshData: @escaping @MainActor () async -> Void = {}
    ) {
        self.permission = permission
        self.transport = transport
        self.audio = audio
        self.refreshData = refreshData
    }

    func start() async {
        let requestGeneration = sessionGeneration
        if let endTask {
            await endTask.value
            guard requestGeneration == sessionGeneration else { return }
        }
        guard !isActiveState else { return }
        if startAttemptInProgress {
            await withCheckedContinuation { startAttemptWaiters.append($0) }
            guard requestGeneration == sessionGeneration else { return }
            guard !isActiveState else { return }
        }
        guard !startAttemptInProgress else { return }
        startAttemptInProgress = true
        defer { finishStartAttempt() }

        stopLocalSession()
        let generation = UUID()
        sessionGeneration = generation
        state = .connecting
        isMuted = false
        userCaption = ""
        coachCaption = ""

        let pendingClose = closeTask
        closeTask = nil
        if let pendingClose {
            await pendingClose.value
            guard generation == sessionGeneration else { return }
        }

        let permissionGranted = await permission.requestPermission()
        guard generation == sessionGeneration else { return }
        guard permissionGranted else {
            state = .failed(
                message: "Microphone access is required for voice coaching.",
                retryable: false,
                settingsAvailable: true
            )
            return
        }

        do {
            transportIsOpen = true
            let events = try await transport.connect()
            guard generation == sessionGeneration else { return }
            try audio.start()
            audioIsRunning = true
            state = .listening
            launchSessionTasks(events: events, generation: generation)
        } catch {
            guard generation == sessionGeneration else { return }
            audio.stop()
            audioIsRunning = false
            if transportIsOpen {
                transportIsOpen = false
                await transport.close()
                guard generation == sessionGeneration else { return }
            }
            if let connectionError = error as? LiveCoachConnectionError {
                state = .failed(
                    message: connectionError.message,
                    retryable: connectionError.retryable,
                    settingsAvailable: false
                )
            } else {
                state = .failed(
                    message: "Voice coach could not connect. Try again.",
                    retryable: true,
                    settingsAvailable: false
                )
            }
        }
    }

    func toggleMute() async {
        guard state == .listening || state == .speaking else { return }
        let generation = sessionGeneration
        let next = !isMuted
        do {
            try await transport.setMuted(next)
            guard !Task.isCancelled,
                  generation == sessionGeneration,
                  state == .listening || state == .speaking else { return }
            isMuted = next
            audio.setMuted(next)
        } catch {
            guard generation == sessionGeneration else { return }
            failCurrentSession(
                message: "Voice coach lost the connection. Try again.",
                retryable: true
            )
        }
    }

    func end() async {
        if let endTask {
            sessionGeneration = UUID()
            await endTask.value
            return
        }
        let pendingClose = closeTask
        closeTask = nil
        let hadTransport = transportIsOpen
        sessionGeneration = UUID()
        guard hadTransport || audioIsRunning || !sessionTasks.isEmpty || pendingClose != nil else {
            state = .ended
            return
        }
        stopLocalSession()
        transportIsOpen = false
        let taskID = UUID()
        endTaskID = taskID
        let task = Task { [weak self, transport] in
            if hadTransport {
                await transport.close()
            }
            if let pendingClose {
                await pendingClose.value
            }
            guard let self, self.endTaskID == taskID else { return }
            self.endTask = nil
            self.endTaskID = nil
            self.state = .ended
            self.requestDataRefresh()
        }
        endTask = task
        await task.value
    }

    private func launchSessionTasks(
        events: AsyncThrowingStream<LiveCoachServerEvent, Error>,
        generation: UUID
    ) {
        sessionTasks = [
            Task { [weak self] in
                do {
                    for try await event in events {
                        guard !Task.isCancelled else { return }
                        self?.handle(event, generation: generation)
                    }
                    self?.finishRemoteSession(generation: generation)
                } catch {
                    self?.failIfCurrent(generation: generation)
                }
            },
            Task { [weak self, audio, transport] in
                for await chunk in audio.capturedAudio {
                    guard !Task.isCancelled else { return }
                    do {
                        try await transport.sendAudio(chunk)
                    } catch {
                        self?.failIfCurrent(generation: generation)
                        return
                    }
                }
            },
            Task { [weak self, audio] in
                for await active in audio.playbackActivity {
                    guard !Task.isCancelled else { return }
                    self?.handlePlayback(active: active, generation: generation)
                }
            },
            Task { [weak self, audio] in
                for await _ in audio.lifecycleEvents {
                    guard !Task.isCancelled else { return }
                    guard self?.sessionGeneration == generation else { return }
                    Task { [weak self] in await self?.end() }
                    return
                }
            },
        ]
    }

    private func handle(_ event: LiveCoachServerEvent, generation: UUID) {
        guard generation == sessionGeneration else { return }
        switch event {
        case .started:
            break
        case .dataChanged:
            requestDataRefresh()
        case .inputTranscript(let delta):
            userCaption = Self.appending(delta, to: userCaption)
        case .outputTranscript(let delta):
            coachCaption = Self.appending(delta, to: coachCaption)
        case .outputAudio(let data):
            do {
                try audio.play(data)
            } catch {
                failCurrentSession(
                    message: "Voice playback could not continue.",
                    retryable: true
                )
            }
        case .inputMuted(let muted):
            isMuted = muted
            audio.setMuted(muted)
        case .closed:
            finishRemoteSession(generation: generation)
        case .failure(let code, let message):
            let terminalCodes = ["not_configured", "provider_access"]
            failCurrentSession(
                message: message,
                retryable: !terminalCodes.contains(code)
            )
        }
    }

    private func handlePlayback(active: Bool, generation: UUID) {
        guard generation == sessionGeneration else { return }
        guard state == .listening || state == .speaking else { return }
        state = active ? .speaking : .listening
    }

    private func failIfCurrent(generation: UUID) {
        guard generation == sessionGeneration else { return }
        failCurrentSession(
            message: "Voice coach lost the connection. Try again.",
            retryable: true
        )
    }

    private func failCurrentSession(message: String, retryable: Bool) {
        let shouldClose = transportIsOpen
        transportIsOpen = false
        sessionGeneration = UUID()
        stopLocalSession()
        state = .failed(
            message: message,
            retryable: retryable,
            settingsAvailable: false
        )
        if shouldClose {
            requestDataRefresh()
            closeTask = Task { [transport] in await transport.close() }
        }
    }

    private func finishRemoteSession(generation: UUID) {
        guard generation == sessionGeneration else { return }
        sessionGeneration = UUID()
        transportIsOpen = false
        stopLocalSession()
        state = .ended
        requestDataRefresh()
    }

    /// The server sends this signal only after an action has been saved. Serial,
    /// coalesced refreshes keep a spoken sequence of sets from flooding the API.
    func requestDataRefresh() {
        refreshRequested = true
        guard refreshTask == nil else { return }
        refreshTask = Task { [weak self] in
            guard let self else { return }
            while self.refreshRequested {
                try? await Task.sleep(for: .milliseconds(300))
                self.refreshRequested = false
                await self.refreshData()
            }
            self.refreshTask = nil
        }
    }

    private func stopLocalSession() {
        sessionTasks.forEach { $0.cancel() }
        sessionTasks.removeAll()
        if audioIsRunning {
            audio.stop()
            audioIsRunning = false
        }
    }

    private var isActiveState: Bool {
        state == .connecting || state == .listening || state == .speaking
    }

    private func finishStartAttempt() {
        startAttemptInProgress = false
        let waiters = startAttemptWaiters
        startAttemptWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }

    private static func appending(_ delta: String, to existing: String) -> String {
        let combined = existing + delta
        return String(combined.suffix(4_000))
    }
}
