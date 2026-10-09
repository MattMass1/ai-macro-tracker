import Foundation

/// ElevenLabs voice backend. It implements the SAME `LiveCoachTransporting` and
/// `LiveCoachAudioHandling` protocols the GPT path uses, so `LiveCoachController`
/// drives either backend unchanged. The ElevenLabs SDK (LiveKit WebRTC) owns the
/// microphone and speaker, so the audio shim here is deliberately inert: there is
/// no PCM capture or playback to do on our side.
///
/// The SDK itself is reached only through `ElevenLabsConversationDriving`, so all
/// of this translation logic is unit-testable with a fake, no SDK required.

// MARK: - Provider preference

enum VoiceProviderPreference {
    /// Local debug override (e.g. set from a launch argument or a debug toggle).
    private static let overrideKey = "mmacros.voice.provider.override"
    /// Last value the server sent on /api/today. Applied on the next launch.
    private static let cacheKey = "mmacros.voice.provider"
    private static let allowed: Set<String> = ["openai", "elevenlabs"]

    static func resolved(_ defaults: UserDefaults = .standard) -> String {
        // ElevenLabs is THE voice. Only an explicit debug override changes it.
        if let override = defaults.string(forKey: overrideKey), allowed.contains(override) {
            return override
        }
        return "elevenlabs"
    }

    static func cacheServerValue(_ value: String?, _ defaults: UserDefaults = .standard) {
        guard let value, allowed.contains(value) else { return }
        defaults.set(value, forKey: cacheKey)
    }
}

// MARK: - SDK abstraction

/// The minimal surface we use from the ElevenLabs SDK. The real adapter
/// (`LiveElevenLabsConversation`) wraps the SDK; tests use a fake.
@MainActor
protocol ElevenLabsConversationDriving: AnyObject {
    var onUserTranscript: ((String) -> Void)? { get set }
    var onAgentTranscript: ((String) -> Void)? { get set }
    /// true = agent speaking, false = listening.
    var onAgentSpeaking: ((Bool) -> Void)? { get set }
    var onStarted: (() -> Void)? { get set }
    var onClosed: ((String) -> Void)? { get set }
    var onFailure: ((_ code: String, _ message: String) -> Void)? { get set }
    func start() async throws
    func stop() async
    func setMuted(_ muted: Bool) async
    func interrupt() async
}

// MARK: - Coordinator

/// Bridges one `ElevenLabsConversationDriving` to the controller's two streams:
/// a `LiveCoachServerEvent` stream (transcripts, started, closed, failure) and a
/// playback-activity stream (agent speaking → .speaking state).
@MainActor
final class ElevenLabsCoordinator {
    private let conversation: ElevenLabsConversationDriving
    private var eventContinuation: AsyncThrowingStream<LiveCoachServerEvent, Error>.Continuation?
    private let playbackContinuation: AsyncStream<Bool>.Continuation
    let playbackActivity: AsyncStream<Bool>

    init(conversation: ElevenLabsConversationDriving) {
        self.conversation = conversation
        (playbackActivity, playbackContinuation) = AsyncStream<Bool>.makeStream(
            bufferingPolicy: .bufferingNewest(8)
        )
    }

    func connect() async throws -> AsyncThrowingStream<LiveCoachServerEvent, Error> {
        let pair = AsyncThrowingStream<LiveCoachServerEvent, Error>.makeStream(
            bufferingPolicy: .bufferingNewest(32)
        )
        eventContinuation = pair.continuation
        conversation.onStarted = { [weak self] in self?.eventContinuation?.yield(.started) }
        conversation.onUserTranscript = { [weak self] text in
            self?.eventContinuation?.yield(.inputTranscript(String(text.prefix(4_096))))
        }
        conversation.onAgentTranscript = { [weak self] text in
            self?.eventContinuation?.yield(.outputTranscript(String(text.prefix(4_096))))
        }
        conversation.onAgentSpeaking = { [weak self] speaking in
            self?.playbackContinuation.yield(speaking)
        }
        conversation.onClosed = { [weak self] reason in
            self?.eventContinuation?.yield(.closed(reason: String(reason.prefix(64))))
            self?.eventContinuation?.finish()
        }
        conversation.onFailure = { [weak self] code, message in
            self?.eventContinuation?.yield(.failure(
                code: String(code.prefix(64)),
                message: String(message.prefix(240))
            ))
        }
        do {
            try await conversation.start()
        } catch let error as LiveCoachConnectionError {
            eventContinuation?.finish()
            throw error
        } catch {
            eventContinuation?.finish()
            throw LiveCoachConnectionError(
                message: "Voice coach could not connect. Try again.",
                retryable: true
            )
        }
        return pair.stream
    }

    func setMuted(_ muted: Bool) async { await conversation.setMuted(muted) }

    func close() async {
        await conversation.stop()
        playbackContinuation.finish()
        eventContinuation?.finish()
        eventContinuation = nil
    }
}

// MARK: - Transport (LiveCoachTransporting)

@MainActor
final class ElevenLabsVoiceTransport: LiveCoachTransporting {
    private let coordinator: ElevenLabsCoordinator

    init(coordinator: ElevenLabsCoordinator) { self.coordinator = coordinator }

    func connect() async throws -> AsyncThrowingStream<LiveCoachServerEvent, Error> {
        try await coordinator.connect()
    }

    /// The SDK captures and uploads microphone audio itself over WebRTC.
    func sendAudio(_ data: Data) async throws {}

    func setMuted(_ muted: Bool) async throws { await coordinator.setMuted(muted) }

    func close() async { await coordinator.close() }
}

// MARK: - Audio shim (LiveCoachAudioHandling)

/// Inert audio handler: the ElevenLabs SDK owns capture and playback. Only
/// `playbackActivity` carries real signal (agent speaking), sourced from the
/// coordinator so the controller still toggles listening/speaking.
@MainActor
final class ElevenLabsVoiceAudio: LiveCoachAudioHandling {
    private let coordinator: ElevenLabsCoordinator
    let capturedAudio: AsyncStream<Data>
    let lifecycleEvents: AsyncStream<LiveCoachAudioLifecycleEvent>

    var playbackActivity: AsyncStream<Bool> { coordinator.playbackActivity }

    init(coordinator: ElevenLabsCoordinator) {
        self.coordinator = coordinator
        // We never capture PCM or receive audio-route lifecycle events; the SDK
        // manages the AVAudioSession. Both streams finish immediately so the
        // controller's sender/lifecycle tasks complete cleanly.
        capturedAudio = AsyncStream { $0.finish() }
        lifecycleEvents = AsyncStream { $0.finish() }
    }

    func start() throws {}
    func play(_ data: Data) throws {}
    func setMuted(_ muted: Bool) {}
    func recoverFromConfigurationChange() throws {}
    func stop() {}
}

// MARK: - Real SDK adapter

// The ONLY code that imports the ElevenLabs SDK. Guarded so the app still builds
// (GPT-only) when the package is absent. It wraps a LiveKit WebRTC `Conversation`
// and maps it onto `ElevenLabsConversationDriving`. The SDK owns mic + speaker;
// we only mint a short-lived token from our server and relay transcripts +
// speaking state. Token minting and all reasoning stay server-side.
#if canImport(ElevenLabs)
import Combine
import ElevenLabs

@MainActor
final class LiveElevenLabsConversation: ElevenLabsConversationDriving {
    var onUserTranscript: ((String) -> Void)?
    var onAgentTranscript: ((String) -> Void)?
    var onAgentSpeaking: ((Bool) -> Void)?
    var onStarted: (() -> Void)?
    var onClosed: ((String) -> Void)?
    var onFailure: ((_ code: String, _ message: String) -> Void)?

    private let tokenProvider: @Sendable () async throws -> String
    private var conversation: Conversation?
    private var cancellables = Set<AnyCancellable>()

    /// `sessionId` is the app's canvas session. It is sent to our token endpoint
    /// so the server binds this conversation to that session; voice turns then
    /// mutate the session the app is viewing. The default provider captures it.
    init(sessionId: String,
         tokenProvider: (@Sendable () async throws -> String)? = nil) {
        self.tokenProvider = tokenProvider ?? {
            try await APIClient.shared.elevenLabsToken(sessionId: sessionId).token
        }
    }

    func start() async throws {
        let token: String
        do {
            token = try await tokenProvider()
        } catch {
            throw LiveCoachConnectionError(
                message: "Voice coach could not connect. Try again.", retryable: true)
        }
        var config = ConversationConfig()
        config.onUserTranscript = { [weak self] text, _ in
            Task { @MainActor in self?.onUserTranscript?(text) }
        }
        config.onAgentResponse = { [weak self] text, _ in
            Task { @MainActor in self?.onAgentTranscript?(text) }
        }
        config.onError = { [weak self] error in
            Task { @MainActor in self?.onFailure?("provider_error", String(describing: error)) }
        }
        do {
            let convo = try await ElevenLabs.startConversation(
                conversationToken: token,
                config: config,
                onAgentReady: { [weak self] in Task { @MainActor in self?.onStarted?() } },
                onDisconnect: { [weak self] reason in
                    Task { @MainActor in self?.onClosed?(String(describing: reason)) }
                }
            )
            conversation = convo
            convo.$agentState
                .sink { [weak self] state in
                    Task { @MainActor in self?.onAgentSpeaking?(state == .speaking) }
                }
                .store(in: &cancellables)
        } catch {
            throw LiveCoachConnectionError(
                message: "Voice coach could not connect. Try again.", retryable: true)
        }
    }

    func stop() async {
        await conversation?.endConversation()
        cancellables.removeAll()
        conversation = nil
    }

    func setMuted(_ muted: Bool) async { try? await conversation?.setMuted(muted) }
    func interrupt() async { try? await conversation?.interruptAgent() }
}
#endif
