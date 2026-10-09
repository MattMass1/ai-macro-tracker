import XCTest
@testable import MacroTracker

@MainActor
final class FakeElevenLabsConversation: ElevenLabsConversationDriving {
    var onUserTranscript: ((String) -> Void)?
    var onAgentTranscript: ((String) -> Void)?
    var onAgentSpeaking: ((Bool) -> Void)?
    var onStarted: (() -> Void)?
    var onClosed: ((String) -> Void)?
    var onFailure: ((String, String) -> Void)?

    var startError: Error?
    private(set) var muted: Bool?
    private(set) var stopped = false
    private(set) var interrupted = false

    func start() async throws {
        if let startError { throw startError }
        onStarted?()
    }
    func stop() async { stopped = true }
    func setMuted(_ muted: Bool) async { self.muted = muted }
    func interrupt() async { interrupted = true }
}

@MainActor
final class ElevenLabsVoiceTests: XCTestCase {
    func testTransportTranslatesConversationEventsToServerEvents() async throws {
        let fake = FakeElevenLabsConversation()
        let coordinator = ElevenLabsCoordinator(conversation: fake)
        let transport = ElevenLabsVoiceTransport(coordinator: coordinator)

        let stream = try await transport.connect()  // fires onStarted -> .started
        fake.onUserTranscript?("hi coach")
        fake.onAgentTranscript?("hello there")
        fake.onClosed?("ended")  // finishes the stream

        var collected: [LiveCoachServerEvent] = []
        for try await event in stream { collected.append(event) }
        XCTAssertEqual(collected, [
            .started,
            .inputTranscript("hi coach"),
            .outputTranscript("hello there"),
            .closed(reason: "ended"),
        ])
    }

    func testAgentSpeakingDrivesPlaybackActivity() async throws {
        let fake = FakeElevenLabsConversation()
        let coordinator = ElevenLabsCoordinator(conversation: fake)
        let audio = ElevenLabsVoiceAudio(coordinator: coordinator)
        _ = try await ElevenLabsVoiceTransport(coordinator: coordinator).connect()  // wires sinks

        fake.onAgentSpeaking?(true)
        fake.onAgentSpeaking?(false)

        var states: [Bool] = []
        for await active in audio.playbackActivity {
            states.append(active)
            if states.count == 2 { break }
        }
        XCTAssertEqual(states, [true, false])
    }

    func testFailureBecomesServerFailureEvent() async throws {
        let fake = FakeElevenLabsConversation()
        let coordinator = ElevenLabsCoordinator(conversation: fake)
        let transport = ElevenLabsVoiceTransport(coordinator: coordinator)
        let stream = try await transport.connect()
        fake.onFailure?("provider_access", "no access")
        fake.onClosed?("ended")

        var collected: [LiveCoachServerEvent] = []
        for try await event in stream { collected.append(event) }
        XCTAssertTrue(collected.contains(.failure(code: "provider_access", message: "no access")))
    }

    func testMuteAndCloseRouteToConversation() async throws {
        let fake = FakeElevenLabsConversation()
        let coordinator = ElevenLabsCoordinator(conversation: fake)
        let transport = ElevenLabsVoiceTransport(coordinator: coordinator)
        _ = try await transport.connect()

        try await transport.setMuted(true)
        XCTAssertEqual(fake.muted, true)
        // Audio shim never captures; sendAudio is a no-op and must not throw.
        try await transport.sendAudio(Data([0, 0]))

        await transport.close()
        XCTAssertTrue(fake.stopped)
    }

    func testConnectThrowsConnectionErrorWhenStartFails() async {
        let fake = FakeElevenLabsConversation()
        fake.startError = LiveCoachConnectionError(message: "boom", retryable: true)
        let transport = ElevenLabsVoiceTransport(coordinator: ElevenLabsCoordinator(conversation: fake))
        do {
            _ = try await transport.connect()
            XCTFail("connect should throw when the conversation fails to start")
        } catch let error as LiveCoachConnectionError {
            XCTAssertTrue(error.retryable)
        } catch {
            XCTFail("expected LiveCoachConnectionError, got \(error)")
        }
    }

    func testProviderPreferenceResolvesOverrideThenCacheThenDefault() {
        let suite = UserDefaults(suiteName: "el-voice-tests-\(UUID().uuidString)")!
        XCTAssertEqual(VoiceProviderPreference.resolved(suite), "openai")
        VoiceProviderPreference.cacheServerValue("elevenlabs", suite)
        XCTAssertEqual(VoiceProviderPreference.resolved(suite), "elevenlabs")
        VoiceProviderPreference.cacheServerValue("garbage", suite)  // ignored
        XCTAssertEqual(VoiceProviderPreference.resolved(suite), "elevenlabs")
        suite.set("openai", forKey: "mmacros.voice.provider.override")
        XCTAssertEqual(VoiceProviderPreference.resolved(suite), "openai")  // override wins
    }
}
