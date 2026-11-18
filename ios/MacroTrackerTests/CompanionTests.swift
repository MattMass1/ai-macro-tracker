import XCTest
import SwiftUI
@testable import MacroTracker

final class CompanionAnimationTests: XCTestCase {
    func testSequenceAndTimingMatchThePackageManifest() {
        XCTAssertEqual(CompanionAnimation.sequence, [0, 1, 2, 3, 1, 0, 0, 0])
        XCTAssertEqual(CompanionAnimation.slotDuration, 0.143, accuracy: 0.0001)
        XCTAssertEqual(CompanionAnimation.loopDuration, 1.144, accuracy: 0.001)
    }

    func testWritingPoseFollowsTheWallClock() {
        let slot = CompanionAnimation.slotDuration
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: 0), 0)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 1.5), 1)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 2.5), 2)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 3.5), 3)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 4.5), 1)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 5.5), 0)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 8.5), 0, "loops after eight slots")
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 9.5), 1)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: -1), 0)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: .nan), 0)
    }

    func testRestAndHoldStillShowTheGroundedRestPose() {
        for character in CompanionCharacter.allCases {
            let rest = CompanionAnimation.frame(character: character, state: .rest, elapsed: 0.5, holdStill: false)
            XCTAssertEqual(rest, CompanionFrame(body: character.restImage, accent: nil))
            let reduced = CompanionAnimation.frame(character: character, state: .writing, elapsed: 0.5, holdStill: true)
            XCTAssertEqual(reduced, CompanionFrame(body: character.restImage, accent: nil), "Reduce Motion / inactive scene never freezes mid-stroke")
        }
    }

    func testWritingFrameCarriesTheMatchingAccent() {
        let frame = CompanionAnimation.frame(character: .female, state: .writing, elapsed: CompanionAnimation.slotDuration * 3.2, holdStill: false)
        XCTAssertEqual(frame, CompanionFrame(body: "companion-female-writing-3", accent: "companion-female-accent-3"))
    }

    func testEveryCompanionAssetShipsAtItsLogicalSize() throws {
        let bundle = Bundle(for: AppStore.self)
        for character in CompanionCharacter.allCases {
            XCTAssertEqual(character.assetNames.count, 9)
            for name in character.assetNames {
                let image = try XCTUnwrap(UIImage(named: name, in: bundle, compatibleWith: nil), "missing \(name)")
                XCTAssertEqual(image.size.width * image.scale, 128, name)
                XCTAssertEqual(image.size.height * image.scale, 128, name)
            }
        }
        let studio = try XCTUnwrap(UIImage(named: "studio-background", in: bundle, compatibleWith: nil))
        XCTAssertEqual(studio.size.width * studio.scale, 144)
        XCTAssertEqual(studio.size.height * studio.scale, 256)
    }
}

@MainActor
final class CoachRespondingStateTests: XCTestCase {
    func testPendingHomeMessageIsClearedOnReset() {
        let store = AppStore()
        store.pendingCoachMessage = "What's left today?"
        store.reset()
        XCTAssertNil(store.pendingCoachMessage)
    }

    func testChatStoreStartsAtRest() {
        let store = AppStore()
        XCTAssertFalse(store.chat.isResponding)
        XCTAssertEqual(store.chat.inFlight, 0)
    }
}


/// Scripted transport only: these tests never contact the real service.
private final class ConsolidationStubProtocol: URLProtocol {
    static var handler: ((URLRequest) -> (status: Int, json: String, delay: TimeInterval))?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let handler = Self.handler, let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        let result = handler(request)
        DispatchQueue.global().asyncAfter(deadline: .now() + result.delay) {
            // A negative fixture status explicitly models transport cancellation.
            if result.status == -999 {
                self.client?.urlProtocol(self, didFailWithError: URLError(.cancelled))
                return
            }
            guard let response = HTTPURLResponse(url: url, statusCode: result.status, httpVersion: nil, headerFields: ["Content-Type": "application/json"]) else { return }
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: Data(result.json.utf8))
            self.client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func stopLoading() {}
}

private final class ConsolidationRequests {
    private let lock = NSLock()
    private var recorded: [URLRequest] = []
    func add(_ request: URLRequest) { lock.lock(); defer { lock.unlock() }; recorded.append(request) }
    var all: [URLRequest] { lock.lock(); defer { lock.unlock() }; return recorded }
    static func body(_ request: URLRequest) -> [String: Any] {
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(buffer, count: count)
            }
        }
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
}

private final class ConsolidationRejections {
    private let lock = NSLock()
    private var values: [Notification] = []
    func add(_ value: Notification) { lock.lock(); values.append(value); lock.unlock() }
    var last: Notification? { lock.lock(); defer { lock.unlock() }; return values.last }
}

@MainActor
private final class DestinationActivationFixture: ObservableObject {
    @Published var isActive = true
    @Published var initialWindow = 30
}

private struct DestinationActivationHarness: View {
    @ObservedObject var activation: DestinationActivationFixture
    let progress: Bool
    var body: some View {
        NavigationStack {
            if progress {
                ProgressDashboardView(isActive: activation.isActive, initialWindow: activation.initialWindow)
            } else {
                WorkoutsView(openCoach: { _ in }, isActive: activation.isActive)
            }
        }
        .opacity(activation.isActive ? 1 : 0)
        .allowsHitTesting(activation.isActive)
        .accessibilityHidden(!activation.isActive)
    }
}

@MainActor
final class RetainedDestinationRefreshTests: XCTestCase {
    func testProgressRevisitRefreshesOnceAndKeepsSelectedWindow() async throws {
        try await checkRevisit(progress: true)
    }

    func testWorkoutsRevisitRefreshesAggregateHistoryOnce() async throws {
        try await checkRevisit(progress: false)
    }

    func testCancelledDestinationReadsDoNotRaiseNavigationAlerts() async throws {
        for progress in [true, false] {
            let started = expectation(description: "destination read started")
            ConsolidationStubProtocol.handler = { _ in
                started.fulfill()
                return (200, progress ? #"{"days":[],"weekly":[],"weight":{"current_kg":1,"goal_kg":80}}"# : #"{"workouts":[]}"#, 0.2)
            }
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [ConsolidationStubProtocol.self]
            let session = URLSession(configuration: configuration)
            let api = APIClient(baseURL: URL(string: "https://tab-revisit.invalid"), tokenProvider: { "synthetic-revisit-only" }, session: session)
            let store = AppStore(api: api)
            let read = Task {
                if progress { await store.trends(days: 30) }
                else { await store.workoutHistory() }
            }
            await fulfillment(of: [started], timeout: 3)
            read.cancel()
            await read.value
            try await Task.sleep(nanoseconds: 250_000_000)
            XCTAssertNil(store.errorMessage, "Leaving a tab is not a service failure")
            XCTAssertNil(store.trendsPayload)
            XCTAssertTrue(store.workoutHistoryEntries.isEmpty)
            session.finishTasksAndInvalidate()
            ConsolidationStubProtocol.handler = nil
        }
    }

    private func eventually(_ condition: () -> Bool) async -> Bool {
        for _ in 0..<60 {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    private func checkRevisit(progress: Bool) async throws {
        let requests = ConsolidationRequests()
        let path = progress ? "/api/trends" : "/api/workouts/history"
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            let count = requests.all.filter { $0.url?.path == path }.count
            let json = progress
                ? String(format: #"{"days":[],"weekly":[],"weight":{"current_kg":%d,"goal_kg":80}}"#, count)
                : String(format: #"{"workouts":[{"id":"entry-%d","exercise":"Fixture row","workout_type":["Pull"],"muscle_group":["Back"],"sets":[],"date":"2026-11-16","created_time":"2026-11-16T12:00:00Z"}]}"#, count)
            return (200, json, 0.01)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ConsolidationStubProtocol.self]
        let session = URLSession(configuration: configuration)
        let api = APIClient(baseURL: URL(string: "https://tab-revisit.invalid"), tokenProvider: { "synthetic-revisit-only" }, session: session)
        let store = AppStore(api: api)
        let activation = DestinationActivationFixture()
        let host = UIHostingController(rootView: DestinationActivationHarness(activation: activation, progress: progress).environmentObject(store))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 390, height: 844)
        window.rootViewController = host
        window.makeKeyAndVisible()
        defer {
            window.isHidden = true
            window.rootViewController = nil
            session.finishTasksAndInvalidate()
            ConsolidationStubProtocol.handler = nil
        }
        let initial = await eventually {
            progress ? store.trendsPayload?.weight.currentKg == 1 : store.workoutHistoryEntries.first?.id == "entry-1"
        }
        XCTAssertTrue(initial)
        XCTAssertEqual(requests.all.filter { $0.url?.path == path }.count, 1, "First activation must not double-load")
        activation.isActive = false
        try await Task.sleep(nanoseconds: 150_000_000)
        activation.initialWindow = 7 // A retained Progress view must keep its chosen 30-day state.
        store.chat.input = "Draft kept while switching tabs"
        try await Task.sleep(nanoseconds: 150_000_000)
        XCTAssertEqual(requests.all.filter { $0.url?.path == path }.count, 1, "Inactive destination must not refresh")
        activation.isActive = true
        let refreshed = await eventually {
            progress ? store.trendsPayload?.weight.currentKg == 2 : store.workoutHistoryEntries.first?.id == "entry-2"
        }
        XCTAssertTrue(refreshed, "Returning to the retained view must fetch its changed response")
        XCTAssertEqual(requests.all.filter { $0.url?.path == path }.count, 2)
        XCTAssertEqual(store.chat.input, "Draft kept while switching tabs")
        XCTAssertNil(store.errorMessage)
        if progress {
            let windows = requests.all.filter { $0.url?.path == path }.compactMap { request -> String? in
                guard let url = request.url else { return nil }
                return URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "days" }?.value
            }
            XCTAssertEqual(windows, ["30", "30"], "Revisit must use the retained window, not reset to the new initial value")
        }
        XCTAssertTrue(requests.all.allSatisfy { $0.httpMethod == "GET" })
    }
}

/// A synthetic credential reader that exposes the old async token-read race.
/// Synchronous main-actor snapshots never block; background reads can be gated.
private final class ConsolidationCredentials {
    private let lock = NSLock()
    private let release = DispatchSemaphore(value: 0)
    private var value = "synthetic-A"
    private var target: Int?
    private var reads = 0
    private var onRead: (() -> Void)?
    func arm(read: Int, onRead: @escaping () -> Void) {
        lock.lock(); defer { lock.unlock() }
        reads = 0; target = read; self.onRead = onRead
    }
    func token() -> String? {
        lock.lock()
        reads += 1
        let pause = reads == target
        let callback = pause ? onRead : nil
        if pause { target = nil }
        lock.unlock()
        callback?()
        if pause && !Thread.isMainThread { _ = release.wait(timeout: .now() + 3) }
        lock.lock(); defer { lock.unlock() }
        return value
    }
    func switchAccount() { lock.lock(); value = "synthetic-B"; lock.unlock() }
    func resume() { release.signal() }
}

@MainActor
final class UnifiedConversationTests: XCTestCase {
    private func api() -> APIClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ConsolidationStubProtocol.self]
        return APIClient(baseURL: URL(string: "https://mmmacros-tests.invalid"), tokenProvider: { "synthetic-test-token" }, session: URLSession(configuration: configuration))
    }

    private func chat() throws -> CoachChatStore {
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "mmmacros-ui-test-" + UUID().uuidString))
        return CoachChatStore(api: api(), defaults: defaults)
    }

    private func assertCredentialBinding(meal: Bool, secondExercise: Bool = false) async throws {
        let acquisition = expectation(description: "credential acquisition")
        let credentials = ConsolidationCredentials()
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            if request.url?.path == "/api/log" {
                return (200, #"{"date":"2026-11-14","day_label":"Fixture","totals":{"calories":123,"protein":0,"carbs":0,"fat":0,"fiber":0},"targets":{"calories":2000,"protein":100,"carbs":200,"fat":60,"fiber":25},"remaining":{"calories":1877,"protein":100,"carbs":200,"fat":60,"fiber":25},"meals":[]}"#, 0.05)
            }
            if request.url?.path == "/api/workout" {
                return (200, #"{"id":"fixture-workout","exercise":"Fixture exercise","workout_type":["Push"],"sets":[{"weight":135,"reps":8}],"date":"2026-11-14"}"#, 0.05)
            }
            if request.url?.path == "/api/workout-stats" { return (503, #"{"error":"Fixture stats unavailable"}"#, 0) }
            return (200, #"{"workouts":[]}"#, 0)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ConsolidationStubProtocol.self]
        let api = APIClient(baseURL: URL(string: "https://mmmacros-tests.invalid"), tokenProvider: credentials.token, session: URLSession(configuration: configuration))
        let store = AppStore(api: api)
        _ = store.chat // Bootstrap preferences before arming the synthetic reader.
        let account = store.accountSession
        credentials.arm(read: secondExercise ? 4 : 1) { acquisition.fulfill() }
        let operation = Task {
            if meal {
                return await store.logMeal(LogMealBody(name: "Synthetic scan", calories: 123, protein: 0, carbs: 0, fat: 0, fiber: 0, macroSource: "Fixture", meal: "Snack", day: nil), session: account)
            }
            for name in (secondExercise ? ["Synthetic bench", "Synthetic row"] : ["Synthetic bench"]) {
                guard await store.logWorkout(exercise: name, sets: [.init(weight: 135, reps: 8)], type: "Push", date: "2026-11-14", session: account) else { return false }
            }
            return true
        }
        await fulfillment(of: [acquisition], timeout: 4)
        credentials.switchAccount()
        store.reset()
        credentials.resume()
        _ = await operation.value
        let writes = requests.all.filter { $0.httpMethod == "POST" }
        XCTAssertFalse(writes.contains { $0.value(forHTTPHeaderField: "Authorization") == "Bearer synthetic-B" }, "Old accepted work must never acquire the replacement account's credential")
        XCTAssertTrue(writes.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer synthetic-A" })
    }

    func testFirstWorkoutCredentialIsBoundBeforeAsyncDispatch() async throws {
        try await assertCredentialBinding(meal: false)
    }

    func testScanCredentialIsBoundBeforeAsyncDispatch() async throws {
        try await assertCredentialBinding(meal: true)
    }

    func testLaterExerciseCredentialIsBoundBeforeAsyncDispatch() async throws {
        try await assertCredentialBinding(meal: false, secondExercise: true)
    }

    private func assertQueueSurvivesCallerCancellation(duringHistory: Bool) async throws {
        let started = expectation(description: "owning caller reached suspension")
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            if request.url?.path == "/api/chat/history" {
                if duringHistory { started.fulfill() }
                return (200, #"{"messages":[]}"#, duringHistory ? 0.15 : 0)
            }
            let turns = requests.all.filter { $0.url?.path.hasSuffix("/turn") == true }
            if !duringHistory && turns.count == 1 { started.fulfill() }
            return (200, #"{"reply":"Completed fixture turn"}"#, 0.15)
        }
        let chat = try chat()
        let caller = Task { await chat.sendMessage("First accepted message") }
        await fulfillment(of: [started], timeout: 3)
        _ = await chat.sendMessage("Second accepted message")
        caller.cancel()
        let completed = await caller.value
        XCTAssertTrue(completed)
        XCTAssertEqual(chat.messages.filter { $0.text == "Completed fixture turn" }.count, 2)
        XCTAssertEqual(chat.messages.filter { $0.role == .user }.map(\.text), ["First accepted message", "Second accepted message"])
        XCTAssertFalse(chat.isSending)
        XCTAssertEqual(chat.inFlight, 0)
    }

    func testCallerCancellationDuringHistoryDoesNotDropAcceptedQueue() async throws {
        try await assertQueueSurvivesCallerCancellation(duringHistory: true)
    }

    func testCallerCancellationDuringTurnDoesNotDropAcceptedQueue() async throws {
        try await assertQueueSurvivesCallerCancellation(duringHistory: false)
    }

    func testMountedStartupContextSurvivesNavigatingAway() async throws {
        let historyStarted = expectation(description: "startup history reached transport")
        let disappeared = expectation(description: "Coach removed from mounted navigation")
        let replied = expectation(description: "accepted contextual turn replied after navigation")
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            if request.url?.path == "/api/chat/history" {
                historyStarted.fulfill()
                return (200, #"{"messages":[]}"#, 0.3)
            }
            return (200, #"{"reply":"Mounted contextual reply"}"#, 0.05)
        }
        let store = AppStore(api: api())
        store.pendingCoachMessage = "Initial contextual shortcut"
        let observer = store.chat.$messages.sink { messages in
            if messages.last?.text == "Mounted contextual reply" { replied.fulfill() }
        }
        defer { observer.cancel() }
        let host = UIHostingController(rootView: AnyView(
            NavigationStack { HomeView() }
                .environmentObject(store).environmentObject(AuthService())
                .onDisappear { disappeared.fulfill() }
        ))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 393, height: 852)
        window.rootViewController = host
        window.makeKeyAndVisible()
        defer { window.isHidden = true; window.rootViewController = nil }
        await fulfillment(of: [historyStarted], timeout: 3)
        host.rootView = AnyView(Text("Workouts destination"))
        host.view.setNeedsLayout(); host.view.layoutIfNeeded()
        await fulfillment(of: [disappeared, replied], timeout: 4)
        XCTAssertNil(store.pendingCoachMessage)
        let turns = requests.all.filter { $0.url?.path.hasSuffix("/turn") == true }
        XCTAssertEqual(turns.count, 1)
        XCTAssertEqual(turns.first.map(ConsolidationRequests.body)?["message"] as? String, "Initial contextual shortcut")
    }

    func testCapturedMissingCredentialNeverFallsThroughToALaterLogin() async {
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            return (500, #"{"error":"Unexpected request"}"#, 0)
        }
        var token: String?
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ConsolidationStubProtocol.self]
        let api = APIClient(baseURL: URL(string: "https://mmmacros-tests.invalid"), tokenProvider: { token }, session: URLSession(configuration: configuration))
        let missing = api.authenticationContext()
        token = "synthetic-B"
        do {
            _ = try await api.logWorkout(LogWorkoutBody(exercise: "Old action", sets: [], workoutType: "Rest", date: nil), authentication: missing)
            XCTFail("A missing initiating credential must not acquire a later login")
        } catch let error as APIError {
            XCTAssertEqual(error.status, 401)
        } catch { XCTFail("Unexpected error: \(error)") }
        XCTAssertTrue(requests.all.isEmpty)
    }

    func testLateUnauthorizedResponseCannotRejectReplacementAuthentication() async throws {
        let started = expectation(description: "old authenticated POST started")
        let rejections = ConsolidationRejections()
        let observer = NotificationCenter.default.addObserver(forName: .deviceTokenRejected, object: nil, queue: nil) { rejections.add($0) }
        defer { NotificationCenter.default.removeObserver(observer) }
        ConsolidationStubProtocol.handler = { _ in
            started.fulfill()
            return (401, #"{"error":"Synthetic old credential rejected"}"#, 0.15)
        }
        var token: String? = "synthetic-A"
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ConsolidationStubProtocol.self]
        let api = APIClient(baseURL: URL(string: "https://mmmacros-tests.invalid"), tokenProvider: { token }, session: URLSession(configuration: configuration))
        let authentication = api.authenticationContext()
        let operation = Task {
            try? await api.logWorkout(LogWorkoutBody(exercise: "Old workout", sets: [], workoutType: "Rest", date: nil), authentication: authentication)
        }
        await fulfillment(of: [started], timeout: 3)
        token = "synthetic-B"
        _ = await operation.value
        let rejection = try XCTUnwrap(rejections.last)
        XCTAssertFalse(api.isCurrentAuthenticationRejection(rejection))
        token = "synthetic-A"
        XCTAssertTrue(api.isCurrentAuthenticationRejection(rejection), "A rejection of the current credential still closes that session")
    }

    func testApprovalAndQueuedMessagesSurviveCallerCancellation() async throws {
        let started = expectation(description: "approval action started")
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            if request.url?.path == "/api/chat/history" { return (200, #"{"messages":[]}"#, 0) }
            if request.url?.path.hasSuffix("/action") == true {
                started.fulfill()
                return (200, #"{"reply":"Fixture workout updated"}"#, 0.15)
            }
            let turns = requests.all.filter { $0.url?.path.hasSuffix("/turn") == true }
            if turns.count == 1 { return (200, #"{"reply":"Change workout?","approval":{"id":"fixture-approval","status":"pending"}}"#, 0) }
            return (200, #"{"reply":"Queued fixture reply"}"#, 0)
        }
        let chat = try chat()
        _ = await chat.sendMessage("Seed approval")
        let message = try XCTUnwrap(chat.messages.last)
        let approval = try XCTUnwrap(message.approval)
        let caller = Task { await chat.respondToApproval(approval, confirm: true, in: message.id) }
        await fulfillment(of: [started], timeout: 3)
        _ = await chat.sendMessage("Queued contextual message")
        caller.cancel()
        let completed = await caller.value
        XCTAssertTrue(completed)
        XCTAssertTrue(chat.messages.contains { $0.text == "Fixture workout updated" })
        XCTAssertTrue(chat.messages.contains { $0.text == "Queued fixture reply" })
        XCTAssertEqual(requests.all.filter { $0.url?.path.hasSuffix("/action") == true }.count, 1)
        XCTAssertFalse(chat.isSending)
    }

    func testAmbiguousTransportCancellationIsNotRetriedOrCalledServiceWaking() async throws {
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            if request.url?.path == "/api/chat/history" { return (200, #"{"messages":[]}"#, 0) }
            return (-999, "", 0)
        }
        let chat = try chat()
        let completed = await chat.sendMessage("Potentially committed fixture turn")
        XCTAssertFalse(completed)
        XCTAssertEqual(requests.all.filter { $0.url?.path.hasSuffix("/turn") == true }.count, 1)
        XCTAssertTrue(chat.messages.last?.text.contains("interrupted") == true)
        XCTAssertFalse(chat.messages.last?.text.contains("waking") == true)
    }

    func testContextualMessageQueuedWhileBusyIsSentExactlyOnce() async throws {
        let requests = ConsolidationRequests()
        let started = expectation(description: "first turn started")
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            if request.url?.path.hasSuffix("/turn") == true {
                if requests.all.filter({ $0.url?.path.hasSuffix("/turn") == true }).count == 1 { started.fulfill() }
                return (200, #"{"reply":"Fixture reply"}"#, 0.15)
            }
            return (200, #"{"messages":[]}"#, 0)
        }
        let chat = try chat()
        let first = Task { await chat.sendMessage("Meal one") }
        await fulfillment(of: [started], timeout: 3)
        let queued = await chat.sendMessage("Workout help")
        XCTAssertFalse(queued, "The existing drainer owns this queued request")
        _ = await first.value
        let turns = requests.all.filter { $0.url?.path.hasSuffix("/turn") == true }
        XCTAssertEqual(turns.map { ConsolidationRequests.body($0)["message"] as? String }, ["Meal one", "Workout help"])
        XCTAssertEqual(chat.messages.filter { $0.role == .user }.map(\.text), ["Meal one", "Workout help"])
        XCTAssertEqual(requests.all.filter { $0.url?.path == "/api/chat/history" }.count, 1)
        XCTAssertFalse(chat.isSending)
        XCTAssertEqual(chat.inFlight, 0)
    }

    func testResetDiscardsLateReplyQueuedTextAndInput() async throws {
        let started = expectation(description: "old turn started")
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            if request.url?.path.hasSuffix("/turn") == true {
                started.fulfill()
                return (200, #"{"reply":"Previous account reply"}"#, 0.15)
            }
            return (200, #"{"messages":[]}"#, 0)
        }
        let chat = try chat()
        let oldSession = chat.sessionId
        let first = Task { await chat.sendMessage("Old account") }
        await fulfillment(of: [started], timeout: 3)
        _ = await chat.sendMessage("Queued old shortcut")
        chat.input = "Unsent old text"
        chat.reset()
        _ = await first.value
        XCTAssertNotEqual(chat.sessionId, oldSession)
        XCTAssertTrue(chat.messages.isEmpty)
        XCTAssertTrue(chat.input.isEmpty)
        XCTAssertFalse(chat.isSending)
        XCTAssertEqual(chat.inFlight, 0)
        XCTAssertEqual(requests.all.filter { $0.url?.path.hasSuffix("/turn") == true }.count, 1)
    }

    func testLateUnknownSessionCannotRotateTheNewUsersSession() async throws {
        let started = expectation(description: "old turn started")
        ConsolidationStubProtocol.handler = { request in
            if request.url?.path.hasSuffix("/turn") == true {
                started.fulfill()
                return (400, #"{"error":"Unknown session"}"#, 0.15)
            }
            return (200, #"{"messages":[]}"#, 0)
        }
        let chat = try chat()
        let first = Task { await chat.sendMessage("Old account") }
        await fulfillment(of: [started], timeout: 3)
        chat.reset()
        let newSession = chat.sessionId
        _ = await first.value
        XCTAssertEqual(chat.sessionId, newSession)
        XCTAssertTrue(chat.messages.isEmpty)
        XCTAssertEqual(chat.inFlight, 0)
    }

    func testHistoryAndDraftBelongToOneConversationNotAView() async throws {
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            return (200, #"{"messages":[{"role":"user","content":"Earlier meal"},{"role":"assistant","content":"Earlier reply"}]}"#, 0.05)
        }
        let chat = try chat()
        let first = Task { await chat.loadHistory() }
        let second = Task { await chat.loadHistory() }
        await first.value
        await second.value
        chat.input = "Still drafting"
        await chat.loadHistory()
        XCTAssertEqual(requests.all.count, 1)
        XCTAssertEqual(chat.messages.map(\.text), ["Earlier meal", "Earlier reply"])
        XCTAssertEqual(chat.input, "Still drafting")
    }

    func testWorkoutApprovalCannotBeConfirmedTwice() async throws {
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            if request.url?.path.hasSuffix("/turn") == true {
                return (200, #"{"reply":"Change workout?","approval":{"id":"fixture-approval","title":"Change workout","status":"pending"}}"#, 0)
            }
            if request.url?.path.hasSuffix("/action") == true { return (200, #"{"reply":"Updated"}"#, 0) }
            return (200, #"{"messages":[]}"#, 0)
        }
        let chat = try chat()
        _ = await chat.sendMessage("Change workout")
        let message = try XCTUnwrap(chat.messages.last)
        let approval = try XCTUnwrap(message.approval)
        let first = await chat.respondToApproval(approval, confirm: true, in: message.id)
        let duplicate = await chat.respondToApproval(approval, confirm: true, in: message.id)
        XCTAssertTrue(first)
        XCTAssertFalse(duplicate)
        XCTAssertEqual(requests.all.filter { $0.url?.path.hasSuffix("/action") == true }.count, 1)
    }

    func testLateMealSaveDoesNotAcknowledgeIntoAnotherConversation() async throws {
        let started = expectation(description: "old meal save started")
        ConsolidationStubProtocol.handler = { request in
            if request.url?.path == "/api/log" {
                started.fulfill()
                return (200, #"{"date":"2026-11-14","day_label":"Fixture","totals":{"calories":123,"protein":0,"carbs":0,"fat":0,"fiber":0},"targets":{"calories":2000,"protein":100,"carbs":200,"fat":60,"fiber":25},"remaining":{"calories":1877,"protein":100,"carbs":200,"fat":60,"fiber":25},"meals":[]}"#, 0.15)
            }
            return (200, #"{"messages":[{"role":"assistant","content":"Account B history"}]}"#, 0)
        }
        let store = AppStore(api: api())
        let scanContext = store.chat.context
        let save = Task {
            let saved = await store.logMeal(LogMealBody(name: "Account A private scan", calories: 123, protein: 0, carbs: 0, fat: 0, fiber: 0, macroSource: "Fixture barcode", meal: "Snack", day: nil))
            if saved { store.chat.recordLoggedMeal(name: "Account A private scan", calories: 123, context: scanContext) }
            return saved
        }
        await fulfillment(of: [started], timeout: 3)
        store.reset()
        await store.chat.loadHistory()
        let saved = await save.value
        XCTAssertFalse(saved, "An obsolete account's save must not be acknowledged as current success")
        XCTAssertEqual(store.chat.messages.map(\.text), ["Account B history"])
        XCTAssertNil(store.day)
    }

    func testFinishStopsIfAccountChangesDuringTheFirstStatsRefresh() async {
        let refreshing = expectation(description: "first exercise stats refresh started")
        let requests = ConsolidationRequests()
        var credential = "synthetic-A"
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            if request.url?.path == "/api/workout" {
                return (200, #"{"id":"fixture-workout","exercise":"Fixture exercise","workout_type":["Upper A"],"sets":[{"weight":135,"reps":8}],"date":"2026-11-14"}"#, 0)
            }
            if request.url?.path == "/api/workout-stats" {
                if requests.all.filter({ $0.url?.path == "/api/workout-stats" }).count == 1 { refreshing.fulfill() }
                return (503, #"{"error":"Fixture stats unavailable"}"#, 0.15)
            }
            return (200, #"{"workouts":[]}"#, 0)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ConsolidationStubProtocol.self]
        let api = APIClient(baseURL: URL(string: "https://mmmacros-tests.invalid"), tokenProvider: { credential }, session: URLSession(configuration: configuration))
        let store = AppStore(api: api)
        let finish = Task {
            for exercise in ["Account A Bench", "Account A Row"] {
                guard await store.logWorkout(exercise: exercise, sets: [.init(weight: 135, reps: 8)], type: "Upper A", date: "2026-11-14") else { return false }
            }
            return true
        }
        await fulfillment(of: [refreshing], timeout: 3)
        store.reset()
        credential = "synthetic-B"
        let finished = await finish.value
        XCTAssertFalse(finished)
        let writes = requests.all.filter { $0.httpMethod == "POST" }
        XCTAssertEqual(writes.count, 1, "An old workout must never continue under the new account")
        XCTAssertEqual(writes.first?.value(forHTTPHeaderField: "Authorization"), "Bearer synthetic-A")
        XCTAssertFalse(writes.contains { $0.value(forHTTPHeaderField: "Authorization") == "Bearer synthetic-B" })
        XCTAssertTrue(store.workouts.isEmpty)
    }

    func testReceiptFromDismissedOldConversationIsRejectedEvenWithoutASaveCallback() throws {
        let chat = try chat()
        let oldContext = chat.context
        chat.reset()
        chat.recordLoggedMeal(name: "Old scan", calories: 123, context: oldContext)
        XCTAssertTrue(chat.messages.isEmpty)
        chat.recordLoggedMeal(name: "Current scan", calories: 123, context: chat.context)
        XCTAssertEqual(chat.messages.map(\.text), ["Logged Current scan, 123 kcal."])
    }

    func testObsoleteWorkoutAndMealSessionsCannotStartWrites() async {
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            return (500, #"{"error":"Unexpected request"}"#, 0)
        }
        let store = AppStore(api: api())
        let oldSession = store.accountSession
        store.reset()
        let workout = await store.logWorkout(exercise: "Old exercise", sets: [.init(weight: 135, reps: 8)], type: "Push", session: oldSession)
        let meal = await store.logMeal(LogMealBody(name: "Old meal", calories: 123, protein: 0, carbs: 0, fat: 0, fiber: 0, macroSource: "Fixture", meal: "Snack", day: nil), session: oldSession)
        XCTAssertFalse(workout)
        XCTAssertFalse(meal)
        XCTAssertTrue(requests.all.isEmpty, "Presented actions stay bound to their original account before the first request too")
    }

    func testWorkoutSaveUsesCapturedDayAndDoesNotPolluteAnotherDay() async {
        let requests = ConsolidationRequests()
        ConsolidationStubProtocol.handler = { request in
            requests.add(request)
            if request.url?.path == "/api/workout" {
                return (200, #"{"id":"fixture-workout","exercise":"Bench Press","workout_type":["Upper A"],"sets":[{"weight":135,"reps":8}],"date":"2026-11-14"}"#, 0)
            }
            if request.url?.path == "/api/workouts/history" { return (200, #"{"workouts":[]}"#, 0) }
            return (503, #"{"error":"Fixture stats unavailable"}"#, 0)
        }
        let store = AppStore(api: api())
        let saved = await store.logWorkout(exercise: "Bench Press", sets: [.init(weight: 135, reps: 8)], type: "Upper A", date: "2026-11-14")
        XCTAssertTrue(saved)
        let writes = requests.all.filter { $0.httpMethod == "POST" }
        XCTAssertEqual(writes.count, 1)
        let body = writes.first.map(ConsolidationRequests.body) ?? [:]
        XCTAssertEqual(body["date"] as? String, "2026-11-14")
        XCTAssertTrue(store.workouts.isEmpty, "A historical save must not appear in today's list")
    }
}
