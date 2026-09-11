import AVFoundation
import Foundation
import SwiftUI
import UIKit
import XCTest
@testable import MacroTracker

private enum VoiceContractFixtures {
    static func data(_ name: String) throws -> Data {
        let fixture = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("shared/fixtures/\(name)")
        return try Data(contentsOf: fixture)
    }
}

private final class TestDeviceTokens: @unchecked Sendable {
    private let lock = NSLock()
    private var value: String?
    init(_ value: String?) { self.value = value }
    var token: String? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
    @discardableResult
    func save(_ token: String?) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        value = token
        return true
    }
}

private final class RejectedTokenURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "token-rejection.test" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        guard let url = request.url,
              let response = HTTPURLResponse(url: url, statusCode: 401, httpVersion: nil, headerFields: nil) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(#"{"error":"Invalid device token"}"#.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
}

private final class VoiceRefreshURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "voice-refresh.test" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        guard let url = request.url else { return }
        guard request.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-device-token",
              request.value(forHTTPHeaderField: "X-App-Token") == nil else {
            client?.urlProtocol(self, didFailWithError: URLError(.userAuthenticationRequired))
            return
        }
        let workout = #"{"id":"voice-set","exercise":"Dumbbell Bench Press","workout_type":["Push"],"muscle_group":["Chest"],"sets":[{"weight":120,"reps":10},{"weight":110,"reps":8}],"date":"2026-09-11","created_time":"2026-09-11T10:00:00Z"}"#
        let body: String
        switch url.path {
        case "/api/today":
            body = #"{"date":"2026-09-11","day_label":"Today","totals":{"calories":650,"protein":55,"carbs":60,"fat":21,"fiber":5},"targets":{"calories":2100,"protein":175,"carbs":210,"fat":70,"fiber":30},"remaining":{"calories":1450,"protein":120,"carbs":150,"fat":49,"fiber":25},"meals":[]}"#
        case "/api/presets": body = #"{"presets":[]}"#
        case "/api/brief": body = #"{"date":"2026-09-11","text":""}"#
        case "/api/exercises": body = #"{"exercises":[]}"#
        case "/api/plan":
            do {
                body = String(decoding: try VoiceContractFixtures.data("today-workout-plan.json"), as: UTF8.self)
            } catch {
                client?.urlProtocol(self, didFailWithError: error)
                return
            }
        case "/api/workout-stats":
            body = #"{"today":{"date":"2026-09-11","entries":1,"exercises":[]},"week":{"week_start":"2026-09-07","week_label":"This week","days_logged":1,"total_sets":2,"total_volume":2080,"streak_weeks":1},"coverage":{"muscle_groups":{},"workout_types":{},"untouched":[]},"prs":[],"plan":{"today":{"type":"Push","exercises":["Dumbbell Bench Press"]},"next":[]}}"#
        case "/api/workouts/history": body = "{\"workouts\":[\(workout)]}"
        case "/api/trends":
            body = #"{"days":[{"date":"2026-09-11","day_label":"Today","calories":650,"protein":55,"carbs":60,"fat":21,"target_calories":2100,"target_protein":175}],"weekly":[],"weight":{"current_kg":80,"goal_kg":75}}"#
        default:
            guard url.path.hasPrefix("/api/workouts/") else {
                client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
                return
            }
            body = "{\"date\":\"2026-09-11\",\"day_label\":\"Today\",\"workouts\":[\(workout)]}"
        }
        guard let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: ["Content-Type": "application/json"]) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
}

@MainActor
final class LiveCoachControllerTests: XCTestCase {
    func testLateOldTokenHTTPRejectionPreservesNewLoginAndCurrentRejectionSignsOut() async {
        let tokens = TestDeviceTokens("old-fixture-token")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RejectedTokenURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let auth = AuthService(
            tokenProvider: { tokens.token },
            tokenSaver: { tokens.save($0) },
            tokenDeleter: { tokens.save(nil) }
        )
        // The request captures the old credential, then another login replaces
        // it before the response arrives. No real Keychain values are touched.
        let oldAPI = APIClient(
            baseURL: URL(string: "https://token-rejection.test"),
            tokenProvider: {
                let captured = tokens.token
                tokens.save("new-fixture-token")
                return captured
            },
            session: session
        )
        do {
            _ = try await oldAPI.today()
            XCTFail("Expected HTTP rejection")
        } catch { XCTAssertEqual((error as? APIError)?.status, 401) }
        await drainTasks()
        XCTAssertTrue(auth.isAuthenticated)
        XCTAssertEqual(tokens.token, "new-fixture-token")
        XCTAssertNil(auth.errorMessage)

        let currentAPI = APIClient(
            baseURL: URL(string: "https://token-rejection.test"),
            tokenProvider: { tokens.token },
            session: session
        )
        _ = try? await currentAPI.today()
        await drainTasks()
        XCTAssertFalse(auth.isAuthenticated)
        XCTAssertNil(tokens.token)
    }

    func testQueuedTokenRejectionRechecksIdentityWhenHandled() async {
        let tokens = TestDeviceTokens("old-fixture-token")
        let auth = AuthService(
            tokenProvider: { tokens.token },
            tokenSaver: { tokens.save($0) },
            tokenDeleter: { tokens.save(nil) }
        )
        DeviceTokenRejection.post(token: "old-fixture-token")
        tokens.save("new-fixture-token")
        NotificationCenter.default.post(name: .deviceTokenRejected, object: nil)
        await drainTasks()
        XCTAssertTrue(auth.isAuthenticated)
        XCTAssertEqual(tokens.token, "new-fixture-token")
    }

    func testVoiceRefreshLoadsSavedMacrosWorkoutSwapAndSets() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [VoiceRefreshURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let store = AppStore(api: APIClient(
            baseURL: URL(string: "https://voice-refresh.test"),
            tokenProvider: { "fixture-device-token" },
            session: session
        ))
        store.selectedDate = Date(timeIntervalSince1970: 0)

        await store.refreshAfterVoice()

        XCTAssertNil(store.errorMessage)
        XCTAssertTrue(store.isToday)
        XCTAssertEqual(store.day?.totals.calories, 650)
        XCTAssertEqual(store.day?.remaining.protein, 120)
        XCTAssertEqual(store.plan?.upcoming.first?.exercises.first?.name, "Dumbbell Bench Press")
        XCTAssertEqual(store.plan?.upcoming.first?.exercises.first?.sets, 4)
        XCTAssertEqual(store.plan?.upcoming.first?.exercises.first?.reps, "10")
        XCTAssertEqual(store.plan?.upcoming.first?.exercises.first?.restSec, 90)
        // Planned sets do not create or replace completed workout records.
        XCTAssertEqual(store.workouts.first?.sets, [WorkoutSet(weight: 120, reps: 10), WorkoutSet(weight: 110, reps: 8)])
        XCTAssertEqual(store.workoutHistoryEntries.first?.exercise, "Dumbbell Bench Press")
        XCTAssertEqual(store.trendsPayload?.days.first?.calories, 650)
    }

    func testCanonicalDataChangeEventDecodes() throws {
        XCTAssertEqual(try LiveCoachWireCodec.decode(VoiceContractFixtures.data("live-data-changed.json")), .dataChanged)
    }

    func testSavedWorkoutPrescriptionDecodesAndRendersWithoutClaimingCompletion() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let plan = try decoder.decode(WorkoutPlanPayload.self, from: VoiceContractFixtures.data("today-workout-plan.json"))
        let exercise = try XCTUnwrap(plan.upcoming.first?.exercises.first)
        XCTAssertEqual(WorkoutPrescriptionPresentation(exercise: exercise).text, "Planned: 4 sets · 10 reps · 90 sec rest")
        let row = WorkoutExerciseRow(exercise: exercise, isCompleted: false, onToggle: {}, onLog: {}, onSwap: {})
        let renderer = ImageRenderer(content: row.padding(20).frame(width: 390).background(Theme.surface))
        renderer.scale = 2
        let image = try XCTUnwrap(renderer.uiImage)
        let attachment = XCTAttachment(image: image)
        attachment.name = "saved-today-workout-prescription"
        attachment.lifetime = .keepAlways
        add(attachment)

        var updated = exercise
        updated.reps = "8-12"
        updated.restSec = 60
        XCTAssertNotEqual([exercise], [updated], "Same-name prescription edits must trigger the row-state refresh")
        XCTAssertEqual(WorkoutPrescriptionPresentation(exercise: updated).text, "Planned: 4 sets · 8-12 reps · 60 sec rest")
    }

    func testLegacyNameOnlyWorkoutDoesNotInventPrescription() throws {
        let exercise = try JSONDecoder().decode(WorkoutPlanPayload.Exercise.self, from: Data(#"{"name":"Bench Press"}"#.utf8))
        XCTAssertNil(WorkoutPrescriptionPresentation(exercise: exercise).text)
    }

    func testSavedActionsCoalesceRefreshAndRefreshAgainAfterEnd() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        var refreshCount = 0
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: AudioStub(calls: calls),
            refreshData: { refreshCount += 1 }
        )
        await subject.start()
        transport.emit(.dataChanged)
        transport.emit(.dataChanged)
        transport.emit(.dataChanged)
        try? await Task.sleep(for: .milliseconds(450))
        XCTAssertEqual(refreshCount, 1)
        await subject.end()
        try? await Task.sleep(for: .milliseconds(450))
        XCTAssertEqual(refreshCount, 2)
        await subject.end()
        try? await Task.sleep(for: .milliseconds(450))
        XCTAssertEqual(refreshCount, 2)
    }

    func testConnectionErrorsExplainRejectedTokenAndMissingRoute() {
        XCTAssertEqual(LiveCoachWebSocketTransport.connectionError(status: 401).retryable, false)
        XCTAssertTrue(LiveCoachWebSocketTransport.connectionError(status: 401).message.contains("invite code"))
        XCTAssertTrue(LiveCoachWebSocketTransport.connectionError(status: 404).message.contains("server"))
        XCTAssertEqual(LiveCoachWebSocketTransport.connectionError(status: 503).retryable, true)
        XCTAssertEqual(LiveCoachWebSocketTransport.connectionError(status: 403).retryable, true)
        XCTAssertFalse(LiveCoachWebSocketTransport.connectionError(status: 403).message.contains("session expired"))
    }

    func testRefusedWebSocketUpgradePreservesDeviceLogin() async {
        let tokens = TestDeviceTokens("denied-fixture-token")
        let auth = AuthService(
            tokenProvider: { tokens.token },
            tokenSaver: { tokens.save($0) },
            tokenDeleter: { tokens.save(nil) }
        )
        let transport = LiveCoachWebSocketTransport(
            baseURL: URL(string: "http://127.0.0.1:18765"),
            tokenProvider: { tokens.token }
        )
        do {
            _ = try await transport.connect()
            XCTFail("The fixture must refuse this token's WebSocket upgrade")
        } catch {
            XCTAssertEqual(error as? LiveCoachConnectionError, LiveCoachWebSocketTransport.connectionError(status: 403))
        }
        await drainTasks()
        XCTAssertTrue(auth.isAuthenticated)
        XCTAssertEqual(tokens.token, "denied-fixture-token")
    }

    func testMutationDuringRefreshRunsFollowUpWithoutConcurrentReads() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        var refreshCount = 0
        var pending: CheckedContinuation<Void, Never>?
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: AudioStub(calls: calls),
            refreshData: {
                refreshCount += 1
                if refreshCount == 1 { await withCheckedContinuation { pending = $0 } }
            }
        )
        await subject.start()
        transport.emit(.dataChanged)
        try? await Task.sleep(for: .milliseconds(450))
        XCTAssertNotNil(pending)
        transport.emit(.dataChanged)
        try? await Task.sleep(for: .milliseconds(450))
        XCTAssertEqual(refreshCount, 1)
        pending?.resume()
        try? await Task.sleep(for: .milliseconds(450))
        XCTAssertEqual(refreshCount, 2)
        await subject.end()
    }

    func testAuthorizedStartConnectsBeforeOpeningMicrophone() async {
        let calls = CallRecorder()
        let permission = PermissionStub(granted: true, calls: calls)
        let transport = TransportStub(calls: calls)
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: permission,
            transport: transport,
            audio: audio
        )

        await subject.start()

        XCTAssertEqual(subject.state, .listening)
        XCTAssertEqual(calls.values, ["permission", "connect", "audio.start"])
    }

    func testPermissionDenialDoesNotOpenTransportOrMicrophone() async {
        let calls = CallRecorder()
        let subject = LiveCoachController(
            permission: PermissionStub(granted: false, calls: calls),
            transport: TransportStub(calls: calls),
            audio: AudioStub(calls: calls)
        )

        await subject.start()

        XCTAssertEqual(
            subject.state,
            .failed(
                message: "Microphone access is required for voice coaching.",
                retryable: false,
                settingsAvailable: true
            )
        )
        XCTAssertEqual(calls.values, ["permission"])
    }

    func testEndWhilePermissionIsPendingPreventsLateTransportConnect() async {
        let calls = CallRecorder()
        let permission = SuspendedPermissionStub(calls: calls)
        let transport = TransportStub(calls: calls)
        let subject = LiveCoachController(
            permission: permission,
            transport: transport,
            audio: AudioStub(calls: calls)
        )
        let startTask = Task { await subject.start() }
        for _ in 0..<100 where !permission.isPending {
            await Task.yield()
        }

        await subject.end()
        permission.complete(granted: true)
        await startTask.value

        XCTAssertEqual(subject.state, .ended)
        XCTAssertEqual(calls.values, ["permission"])
        XCTAssertEqual(transport.closeCount, 0)
    }

    func testServerFailureUsesSafeRetryabilityFromTransport() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        transport.connectError = LiveCoachConnectionError(
            message: "Voice coach is not configured on the server.",
            retryable: false
        )
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: AudioStub(calls: calls)
        )

        await subject.start()

        XCTAssertEqual(
            subject.state,
            .failed(
                message: "Voice coach is not configured on the server.",
                retryable: false
            )
        )
        XCTAssertEqual(calls.values, ["permission", "connect"])
    }

    func testEndDuringFailedStartClosePreventsStaleFailureState() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        transport.connectError = LiveCoachConnectionError(
            message: "Voice coach is not configured on the server.",
            retryable: false
        )
        transport.suspendClose = true
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: AudioStub(calls: calls)
        )
        let startTask = Task { await subject.start() }
        for _ in 0..<100 where !transport.isClosePending {
            await Task.yield()
        }

        await subject.end()
        transport.completeClose()
        await startTask.value

        XCTAssertEqual(subject.state, .ended)
        XCTAssertEqual(transport.closeCount, 1)
    }

    func testEndStopsAudioAndClosesTransportExactlyOnce() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: audio
        )
        await subject.start()

        await subject.end()
        await subject.end()

        XCTAssertEqual(subject.state, .ended)
        XCTAssertEqual(audio.stopCount, 1)
        XCTAssertEqual(transport.closeCount, 1)
    }

    func testConcurrentEndCallsShareCleanupBeforeReconnect() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        transport.suspendClose = true
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: AudioStub(calls: calls)
        )
        await subject.start()

        let firstEnd = Task { await subject.end() }
        for _ in 0..<100 where !transport.isClosePending {
            await Task.yield()
        }
        let secondEnd = Task { await subject.end() }
        await drainTasks()
        let reconnect = Task { await subject.start() }
        await drainTasks()
        let connectedBeforeCleanupFinished = calls.values.filter { $0 == "connect" }.count > 1

        transport.completeClose()
        await firstEnd.value
        await secondEnd.value
        await reconnect.value

        XCTAssertFalse(connectedBeforeCleanupFinished)
        XCTAssertEqual(subject.state, .listening)
        XCTAssertEqual(calls.values.filter { $0 == "connect" }.count, 2)
        XCTAssertEqual(transport.closeCount, 1)
    }

    func testEndCancelsReconnectAlreadyQueuedBehindCleanup() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        transport.suspendClose = true
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: AudioStub(calls: calls)
        )
        await subject.start()

        let firstEnd = Task { await subject.end() }
        for _ in 0..<100 where !transport.isClosePending {
            await Task.yield()
        }
        let queuedReconnect = Task { await subject.start() }
        await drainTasks()
        let cancellingEnd = Task { await subject.end() }
        await drainTasks()

        transport.completeClose()
        await firstEnd.value
        await queuedReconnect.value
        await cancellingEnd.value

        XCTAssertEqual(subject.state, .ended)
        XCTAssertEqual(calls.values.filter { $0 == "connect" }.count, 1)
        XCTAssertEqual(transport.closeCount, 1)
    }

    func testEndDuringConnectIgnoresStaleCompletion() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        transport.suspendConnect = true
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: audio
        )
        let startTask = Task { await subject.start() }
        for _ in 0..<100 where transport.pendingConnectCount == 0 {
            await Task.yield()
        }

        await subject.end()
        transport.completeConnect()
        await startTask.value

        XCTAssertEqual(subject.state, .ended)
        XCTAssertEqual(transport.closeCount, 1)
        XCTAssertFalse(calls.values.contains("audio.start"))
    }

    func testReconnectWaitsForEndedPendingConnectBeforeStartingNextConnect() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        transport.suspendConnect = true
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: audio
        )
        let firstStart = Task { await subject.start() }
        for _ in 0..<100 where transport.pendingConnectCount != 1 {
            await Task.yield()
        }
        await subject.end()

        let reconnect = Task { await subject.start() }
        await drainTasks()
        XCTAssertEqual(calls.values.filter { $0 == "connect" }.count, 1)
        XCTAssertEqual(transport.pendingConnectCount, 1)

        transport.completeConnect()
        await firstStart.value
        for _ in 0..<100 where transport.pendingConnectCount != 1 {
            await Task.yield()
        }
        transport.completeConnect()
        await reconnect.value

        XCTAssertEqual(subject.state, .listening)
        XCTAssertEqual(calls.values.filter { $0 == "connect" }.count, 2)
        XCTAssertEqual(audio.stopCount, 0)
    }

    func testEndCancelsStartQueuedBehindPendingPermission() async {
        let calls = CallRecorder()
        let permission = SuspendedPermissionStub(calls: calls)
        let transport = TransportStub(calls: calls)
        let subject = LiveCoachController(
            permission: permission,
            transport: transport,
            audio: AudioStub(calls: calls)
        )
        let firstStart = Task { await subject.start() }
        for _ in 0..<100 where !permission.isPending {
            await Task.yield()
        }
        await subject.end()

        let queuedStart = Task { await subject.start() }
        await drainTasks()
        await subject.end()
        permission.complete(granted: true)
        await firstStart.value
        await queuedStart.value

        XCTAssertEqual(subject.state, .ended)
        XCTAssertEqual(calls.values.filter { $0 == "connect" }.count, 0)
        XCTAssertEqual(transport.closeCount, 0)
    }

    func testDuplicateStartWhileListeningDoesNotOpenAnotherSession() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: audio
        )

        await subject.start()
        await subject.start()

        XCTAssertEqual(subject.state, .listening)
        XCTAssertEqual(calls.values.filter { $0 == "permission" }.count, 1)
        XCTAssertEqual(calls.values.filter { $0 == "connect" }.count, 1)
        XCTAssertEqual(calls.values.filter { $0 == "audio.start" }.count, 1)
        XCTAssertEqual(transport.closeCount, 0)
        XCTAssertEqual(audio.stopCount, 0)
    }

    func testSpeakingStateFollowsActualPlaybackQueueActivity() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: audio
        )
        await subject.start()

        let pcm = Data([0, 0, 1, 0])
        transport.emit(.outputTranscript("Keep the pace steady."))
        transport.emit(.outputAudio(pcm))
        await drainTasks()
        XCTAssertEqual(subject.coachCaption, "Keep the pace steady.")
        XCTAssertEqual(audio.played, [pcm])
        XCTAssertEqual(subject.state, .listening)

        audio.emitPlayback(true)
        await drainTasks()
        XCTAssertEqual(subject.state, .speaking)

        audio.emitPlayback(false)
        await drainTasks()
        XCTAssertEqual(subject.state, .listening)
    }

    func testWireCodecAllowsOnlyDocumentedClientFields() throws {
        let pcm = Data([0, 0, 1, 0])

        XCTAssertEqual(
            try LiveCoachWireCodec.decode(
                Data(#"{"type":"session.output_audio.delta","delta":"AAABAA==","private":"drop"}"#.utf8)
            ),
            .outputAudio(pcm)
        )
        XCTAssertEqual(
            try LiveCoachWireCodec.decode(
                Data(#"{"type":"session.output_transcript.delta","delta":"Stay steady.","response":{"private":true}}"#.utf8)
            ),
            .outputTranscript("Stay steady.")
        )
        XCTAssertNil(
            try LiveCoachWireCodec.decode(
                Data(#"{"type":"response.event","response":{"output":["private"]}}"#.utf8)
            )
        )
    }

    func testPlaybackQueueFlushesAtBoundAndIgnoresOldCompletions() {
        var queue = LiveCoachPlaybackQueueState(capacity: 2)

        XCTAssertEqual(queue.enqueue(), .accepted(generation: 0, becameActive: true))
        XCTAssertEqual(queue.enqueue(), .accepted(generation: 0, becameActive: false))
        XCTAssertEqual(queue.enqueue(), .flushed(generation: 1))
        XCTAssertFalse(queue.complete(generation: 0))
        XCTAssertTrue(queue.complete(generation: 1))
        XCTAssertEqual(queue.pendingCount, 0)
    }

    func testAudioGraphRendersS16LEWirePCMWithoutDeviceInput() throws {
        let engine = AVAudioEngine()
        let player = AVAudioPlayerNode()
        guard let wireFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: 24_000,
            channels: 1,
            interleaved: false
        ) else {
            return XCTFail("Could not create the documented wire format")
        }
        guard let renderFormat = AVAudioFormat(
            standardFormatWithSampleRate: 48_000,
            channels: 2
        ) else {
            return XCTFail("Could not create a hardware-like mixer format")
        }
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: wireFormat)
        try engine.enableManualRenderingMode(
            .offline,
            format: renderFormat,
            maximumFrameCount: 960
        )
        engine.prepare()
        try engine.start()
        guard let buffer = AVAudioPCMBuffer(pcmFormat: wireFormat, frameCapacity: 480),
              let bytes = buffer.mutableAudioBufferList.pointee.mBuffers.mData else {
            return XCTFail("Could not allocate a silent PCM buffer")
        }
        bytes.initializeMemory(as: UInt8.self, repeating: 0, count: 960)
        buffer.frameLength = 480
        player.scheduleBuffer(buffer)
        player.play()
        guard let output = AVAudioPCMBuffer(
            pcmFormat: engine.manualRenderingFormat,
            frameCapacity: 960
        ) else {
            return XCTFail("Could not allocate a render buffer")
        }

        let status = try engine.renderOffline(960, to: output)

        XCTAssertEqual(status, .success)
        XCTAssertEqual(output.frameLength, 960)
    }

    func testRealAudioEngineStopIsolatesStreamsForNextSession() async throws {
        let audio = LiveCoachAudioEngine()
        guard let oldCaptured = continuation(
            named: "capturedContinuation",
            as: AsyncStream<Data>.Continuation.self,
            from: audio
        ), let oldPlayback = continuation(
            named: "playbackContinuation",
            as: AsyncStream<Bool>.Continuation.self,
            from: audio
        ), let oldLifecycle = continuation(
            named: "lifecycleContinuation",
            as: AsyncStream<LiveCoachAudioLifecycleEvent>.Continuation.self,
            from: audio
        ) else {
            return XCTFail("Could not inspect the real audio stream continuations")
        }
        let firstCaptureConsumer = Task {
            for await _ in audio.capturedAudio {}
        }
        let firstPlaybackConsumer = Task {
            for await _ in audio.playbackActivity {}
        }
        let firstLifecycleConsumer = Task {
            for await _ in audio.lifecycleEvents {}
        }
        await Task.yield()
        firstCaptureConsumer.cancel()
        firstPlaybackConsumer.cancel()
        firstLifecycleConsumer.cancel()
        await firstCaptureConsumer.value
        await firstPlaybackConsumer.value
        await firstLifecycleConsumer.value

        audio.stop()
        let stalePCM = Data([1, 0, 1, 0])
        oldCaptured.yield(stalePCM)
        oldPlayback.yield(false)
        oldLifecycle.yield(.interrupted)

        guard let currentCaptured = continuation(
            named: "capturedContinuation",
            as: AsyncStream<Data>.Continuation.self,
            from: audio
        ), let currentPlayback = continuation(
            named: "playbackContinuation",
            as: AsyncStream<Bool>.Continuation.self,
            from: audio
        ), let currentLifecycle = continuation(
            named: "lifecycleContinuation",
            as: AsyncStream<LiveCoachAudioLifecycleEvent>.Continuation.self,
            from: audio
        ) else {
            return XCTFail("Could not inspect the replacement audio stream continuations")
        }
        let captureValue = Task { await audio.capturedAudio.first(where: { _ in true }) }
        let playbackValue = Task { await audio.playbackActivity.first(where: { _ in true }) }
        let lifecycleValue = Task { await audio.lifecycleEvents.first(where: { _ in true }) }
        await Task.yield()
        let freshPCM = Data([2, 0, 2, 0])
        currentCaptured.yield(freshPCM)
        currentPlayback.yield(true)
        currentLifecycle.yield(.routeChanged)

        let receivedCapture = await captureValue.value
        let receivedPlayback = await playbackValue.value
        let receivedLifecycle = await lifecycleValue.value
        XCTAssertEqual(receivedCapture, freshPCM)
        XCTAssertEqual(receivedPlayback, true)
        XCTAssertEqual(receivedLifecycle, .routeChanged)
    }

    func testAudioRouteChangeEndsMicrophoneAndSession() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: audio
        )
        await subject.start()

        audio.emitLifecycle(.routeChanged)
        await drainTasks()

        XCTAssertEqual(subject.state, .ended)
        XCTAssertEqual(audio.stopCount, 1)
        XCTAssertEqual(transport.closeCount, 1)
    }

    func testFocusedSheetPresentationCoversEveryLifecycleState() {
        XCTAssertEqual(LiveCoachPresentation(state: .ready).status, "Ready")
        XCTAssertEqual(LiveCoachPresentation(state: .connecting).status, "Connecting")
        XCTAssertEqual(LiveCoachPresentation(state: .listening).status, "Listening")
        XCTAssertEqual(LiveCoachPresentation(state: .speaking).status, "Coach speaking")
        XCTAssertEqual(
            LiveCoachPresentation(state: .failed(message: "Try again.", retryable: true)).primaryAction,
            "Reconnect"
        )
        XCTAssertEqual(LiveCoachPresentation(state: .ended).status, "Session ended")
    }

    func testFailurePresentationOffersSettingsOnlyForPermissionDenial() {
        let denied = LiveCoachPresentation(state: .failed(
            message: "Microphone access is required for voice coaching.",
            retryable: false,
            settingsAvailable: true
        ))
        let missingKey = LiveCoachPresentation(state: .failed(
            message: "Voice coach is not configured on the server.",
            retryable: false
        ))

        XCTAssertTrue(denied.showsSettingsAction)
        XCTAssertFalse(missingKey.showsSettingsAction)
    }

    func testVoiceSheetRendersReadyMissingConfigurationAndPermissionDenied() async {
        let readyCalls = CallRecorder()
        let ready = LiveCoachController(
            permission: PermissionStub(granted: true, calls: readyCalls),
            transport: TransportStub(calls: readyCalls),
            audio: AudioStub(calls: readyCalls)
        )

        let missingCalls = CallRecorder()
        let missingTransport = TransportStub(calls: missingCalls)
        missingTransport.connectError = LiveCoachConnectionError(
            message: "Voice coach is not configured on the server.",
            retryable: false
        )
        let missing = LiveCoachController(
            permission: PermissionStub(granted: true, calls: missingCalls),
            transport: missingTransport,
            audio: AudioStub(calls: missingCalls)
        )
        await missing.start()

        let deniedCalls = CallRecorder()
        let denied = LiveCoachController(
            permission: PermissionStub(granted: false, calls: deniedCalls),
            transport: TransportStub(calls: deniedCalls),
            audio: AudioStub(calls: deniedCalls)
        )
        await denied.start()

        XCTAssertEqual(ready.state, .ready)
        XCTAssertEqual(missing.state, .failed(
            message: "Voice coach is not configured on the server.",
            retryable: false
        ))
        XCTAssertEqual(denied.state, .failed(
            message: "Microphone access is required for voice coaching.",
            retryable: false,
            settingsAvailable: true
        ))
        attachVoiceSheet(controller: ready, name: "voice-coach-ready")
        attachVoiceSheet(controller: missing, name: "voice-coach-missing-key")
        attachVoiceSheet(controller: denied, name: "voice-coach-permission-denied")
    }

    func testWebSocketRequestUsesBearerHeaderAndNoCredentialQuery() throws {
        let request = try LiveCoachWebSocketTransport.makeRequest(
            baseURL: URL(string: "https://macro.example/base"),
            token: "device-token-test"
        )

        XCTAssertEqual(request.url?.absoluteString, "wss://macro.example/base/api/live-coach")
        XCTAssertNil(request.url?.query)
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Authorization"),
            "Bearer device-token-test"
        )
    }

    func testRealURLSessionTransportExchangesTextEventsWithMountedBridge() async throws {
        let transport = LiveCoachWebSocketTransport(
            baseURL: URL(string: "http://127.0.0.1:18765"),
            tokenProvider: { "fixture-device-token" }
        )
        let stream = try await transport.connect()
        let collector = Task { () throws -> [LiveCoachServerEvent] in
            var received: [LiveCoachServerEvent] = []
            for try await event in stream {
                received.append(event)
                if case .closed = event { return received }
            }
            return received
        }
        let pcm = Data([0, 0, 1, 0])

        try await transport.sendAudio(pcm)
        try await transport.setMuted(true)
        try await transport.setMuted(false)
        await transport.close()
        let received = try await collector.value

        XCTAssertEqual(received, [
            .outputTranscript("Keep going."),
            .outputAudio(pcm),
            .inputMuted(true),
            .inputMuted(false),
            .closed(reason: "close_requested"),
        ])
    }

    func testMuteUsesProviderControlAndMirrorsAcknowledgement() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: audio
        )
        await subject.start()

        await subject.toggleMute()
        XCTAssertTrue(subject.isMuted)
        XCTAssertEqual(transport.mutedCommands, [true])
        XCTAssertEqual(audio.mutedValues, [true])

        transport.emit(.inputMuted(false))
        await drainTasks()
        XCTAssertFalse(subject.isMuted)
        XCTAssertEqual(audio.mutedValues, [true, false])
    }

    func testStaleMuteCompletionCannotMutateReconnectedSession() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        transport.suspendMute = true
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: audio
        )
        await subject.start()
        let oldMute = Task { await subject.toggleMute() }
        for _ in 0..<100 where transport.pendingMuteCount == 0 {
            await Task.yield()
        }

        await subject.end()
        await subject.start()
        transport.completeMute()
        await oldMute.value

        XCTAssertEqual(subject.state, .listening)
        XCTAssertFalse(subject.isMuted)
        XCTAssertFalse(audio.mutedValues.contains(true))
    }

    func testMicrophoneContinuesSendingDuringPlaybackForBargeIn() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: audio
        )
        await subject.start()
        audio.emitPlayback(true)
        let pcm = Data([2, 0, 3, 0])
        audio.emitCaptured(pcm)
        await drainTasks()

        XCTAssertEqual(subject.state, .speaking)
        XCTAssertEqual(transport.sentAudio, [pcm])
    }

    func testServerFailureEventStopsAudioAndTransport() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        let audio = AudioStub(calls: calls)
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: audio
        )
        await subject.start()

        transport.emit(.failure(
            code: "provider_busy",
            message: "Voice coach is busy. Try again shortly."
        ))
        await drainTasks()

        XCTAssertEqual(
            subject.state,
            .failed(
                message: "Voice coach is busy. Try again shortly.",
                retryable: true
            )
        )
        XCTAssertEqual(audio.stopCount, 1)
        XCTAssertEqual(transport.closeCount, 1)
    }

    func testReconnectWaitsForFailedTransportCleanup() async {
        let calls = CallRecorder()
        let transport = TransportStub(calls: calls)
        transport.suspendClose = true
        let subject = LiveCoachController(
            permission: PermissionStub(granted: true, calls: calls),
            transport: transport,
            audio: AudioStub(calls: calls)
        )
        await subject.start()
        transport.emit(.failure(
            code: "provider_busy",
            message: "Voice coach is busy. Try again shortly."
        ))
        await drainTasks()

        let reconnect = Task { await subject.start() }
        await drainTasks()
        XCTAssertEqual(calls.values.filter { $0 == "connect" }.count, 1)

        transport.completeClose()
        await reconnect.value
        XCTAssertEqual(calls.values.filter { $0 == "connect" }.count, 2)
    }

    private func drainTasks() async {
        for _ in 0..<20 { await Task.yield() }
    }

    private func continuation<T>(
        named name: String,
        as type: T.Type,
        from audio: LiveCoachAudioEngine
    ) -> T? {
        Mirror(reflecting: audio).children.first { $0.label == name }?.value as? T
    }

    private func attachVoiceSheet(controller: LiveCoachController, name: String) {
        let bounds = CGRect(x: 0, y: 0, width: 393, height: 852)
        let host = UIHostingController(
            rootView: LiveCoachView(controller: controller)
                .tint(Theme.accent)
                .preferredColorScheme(.light)
        )
        guard let scene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first else {
            return XCTFail("The simulator test has no window scene")
        }
        let window = UIWindow(windowScene: scene)
        window.frame = bounds
        window.overrideUserInterfaceStyle = .light
        window.rootViewController = host
        window.makeKeyAndVisible()
        host.view.frame = bounds
        host.view.setNeedsLayout()
        host.view.layoutIfNeeded()
        let format = UIGraphicsImageRendererFormat()
        format.scale = 2
        let image = UIGraphicsImageRenderer(bounds: bounds, format: format).image { _ in
            window.drawHierarchy(in: bounds, afterScreenUpdates: true)
        }
        guard let png = image.pngData() else {
            window.isHidden = true
            return XCTFail("Could not encode the hosted voice sheet")
        }
        XCTAssertGreaterThan(png.count, 10_000)
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        window.isHidden = true
    }
}

@MainActor
private final class CallRecorder {
    var values: [String] = []
}

private struct PermissionStub: LiveCoachPermissionChecking {
    let granted: Bool
    let calls: CallRecorder

    @MainActor
    func requestPermission() async -> Bool {
        calls.values.append("permission")
        return granted
    }
}

@MainActor
private final class SuspendedPermissionStub: LiveCoachPermissionChecking {
    let calls: CallRecorder
    private var continuation: CheckedContinuation<Bool, Never>?
    private var immediateResult: Bool?
    var isPending: Bool { continuation != nil }

    init(calls: CallRecorder) {
        self.calls = calls
    }

    func requestPermission() async -> Bool {
        calls.values.append("permission")
        if let immediateResult { return immediateResult }
        return await withCheckedContinuation { continuation = $0 }
    }

    func complete(granted: Bool) {
        immediateResult = granted
        continuation?.resume(returning: granted)
        continuation = nil
    }
}

private final class TransportStub: LiveCoachTransporting {
    let calls: CallRecorder
    var connectError: Error?
    var closeCount = 0
    var mutedCommands: [Bool] = []
    var sentAudio: [Data] = []
    var suspendConnect = false
    var suspendClose = false
    var suspendMute = false
    private var connectContinuations: [CheckedContinuation<AsyncThrowingStream<LiveCoachServerEvent, Error>, Error>] = []
    private var closeContinuation: CheckedContinuation<Void, Never>?
    private var muteContinuations: [CheckedContinuation<Void, Never>] = []
    private var eventContinuation: AsyncThrowingStream<LiveCoachServerEvent, Error>.Continuation?
    var pendingConnectCount: Int { connectContinuations.count }
    var pendingMuteCount: Int { muteContinuations.count }
    var isClosePending: Bool { closeContinuation != nil }

    init(calls: CallRecorder) {
        self.calls = calls
    }

    @MainActor
    func connect() async throws -> AsyncThrowingStream<LiveCoachServerEvent, Error> {
        calls.values.append("connect")
        if let connectError { throw connectError }
        if suspendConnect {
            return try await withCheckedThrowingContinuation { continuation in
                connectContinuations.append(continuation)
            }
        }
        return AsyncThrowingStream { eventContinuation = $0 }
    }

    func completeConnect() {
        guard !connectContinuations.isEmpty else { return }
        connectContinuations.removeFirst().resume(
            returning: AsyncThrowingStream { eventContinuation = $0 }
        )
    }


    func emit(_ event: LiveCoachServerEvent) {
        eventContinuation?.yield(event)
    }

    func sendAudio(_ data: Data) async throws { sentAudio.append(data) }
    func setMuted(_ muted: Bool) async throws {
        mutedCommands.append(muted)
        if suspendMute {
            await withCheckedContinuation { muteContinuations.append($0) }
        }
    }
    func close() async {
        closeCount += 1
        if suspendClose {
            await withCheckedContinuation { continuation in
                closeContinuation = continuation
            }
        }
    }

    func completeClose() {
        suspendClose = false
        closeContinuation?.resume()
        closeContinuation = nil
    }


    func completeMute() {
        guard !muteContinuations.isEmpty else { return }
        muteContinuations.removeFirst().resume()
    }
}

private final class AudioStub: LiveCoachAudioHandling {
    let calls: CallRecorder
    let capturedAudio: AsyncStream<Data>
    let playbackActivity: AsyncStream<Bool>
    let lifecycleEvents: AsyncStream<LiveCoachAudioLifecycleEvent>
    var stopCount = 0
    var played: [Data] = []
    var mutedValues: [Bool] = []
    private let playbackContinuation: AsyncStream<Bool>.Continuation
    private let lifecycleContinuation: AsyncStream<LiveCoachAudioLifecycleEvent>.Continuation
    private let capturedContinuation: AsyncStream<Data>.Continuation

    init(calls: CallRecorder) {
        self.calls = calls
        (capturedAudio, capturedContinuation) = AsyncStream<Data>.makeStream()
        (playbackActivity, playbackContinuation) = AsyncStream<Bool>.makeStream()
        (lifecycleEvents, lifecycleContinuation) = AsyncStream<LiveCoachAudioLifecycleEvent>.makeStream()
    }

    @MainActor
    func start() throws {
        calls.values.append("audio.start")
    }

    func play(_ data: Data) throws { played.append(data) }
    func emitPlayback(_ active: Bool) { playbackContinuation.yield(active) }
    func emitCaptured(_ data: Data) { capturedContinuation.yield(data) }
    func emitLifecycle(_ event: LiveCoachAudioLifecycleEvent) { lifecycleContinuation.yield(event) }
    func setMuted(_ muted: Bool) { mutedValues.append(muted) }
    func stop() { stopCount += 1 }
}
