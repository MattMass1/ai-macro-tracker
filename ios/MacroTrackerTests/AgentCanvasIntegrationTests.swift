import XCTest
import A2UISwiftUI
@testable import MacroTracker

private final class CanvasFixtureURLProtocol: URLProtocol {
    static var response: Data?
    static var responder: ((URLRequest) throws -> Data)?
    static var methods: [String] = []
    static var urls: [URL] = []
    static var holdToday = false
    static var pendingToday: CanvasFixtureURLProtocol?
    static var observedRequest: ((URLRequest) -> Void)?
    private static let lock = NSLock()
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        Self.methods.append(request.httpMethod ?? "GET")
        if let url = request.url { Self.urls.append(url) }
        Self.lock.unlock()
        Self.observedRequest?(request)
        if Self.holdToday, request.url?.path == "/api/today" {
            Self.pendingToday = self
            return
        }
        finishLoading()
    }
    func finishLoading() {
        let data: Data?
        do { data = try Self.responder?(request) ?? Self.response }
        catch { client?.urlProtocol(self, didFailWithError: error); return }
        guard let data, let url = request.url,
              let http = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil) else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
            return
        }
        client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@MainActor
final class AgentCanvasIntegrationTests: XCTestCase {
    private func snapshot() throws -> AgentCanvasEnvelope {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "snapshot", withExtension: "json"))
        return try AgentCanvasEnvelope.decode(Data(contentsOf: url))
    }

    func testRefreshAdoptsRecreatedSessionWithoutPostingWrites() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); CanvasFixtureURLProtocol.response = nil; CanvasFixtureURLProtocol.methods = [] }
        let api = APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-only" }, session: session)
        let store = AgentSurfaceStore(api: api, authenticated: { true })
        var old = try snapshot()
        old.sessionId = store.sessionId
        _ = try store.receive(old)
        var fresh = old
        fresh.instanceId = UUID().uuidString.lowercased(); fresh.revision = 0
        fresh.surfaces = []; fresh.approval = nil; fresh.workout = nil; fresh.receipt = nil
        CanvasFixtureURLProtocol.response = try JSONEncoder().encode(fresh)
        CanvasFixtureURLProtocol.methods = []
        await store.refresh()
        XCTAssertNil(store.errorMessage)
        XCTAssertEqual(store.envelope?.revision, 0)
        XCTAssertEqual(store.envelope?.instanceId, fresh.instanceId)
        XCTAssertNotNil(store.recoveryNotice)
        XCTAssertNil(store.envelope?.approval)
        XCTAssertEqual(CanvasFixtureURLProtocol.methods, ["GET"])
        XCTAssertThrowsError(try store.receive(old))
        await store.instanceRecoveryTask?.value
        await store.perform(.init(action: .confirm, reference: old.approval?.id))
        XCTAssertTrue(CanvasFixtureURLProtocol.methods.allSatisfy { $0 == "GET" }, "Only readback, never stale approval or uncertain write replay")
    }

    func testFencedTurnRecoversWithOneGETAndNeverReplaysPOST() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); CanvasFixtureURLProtocol.response = nil; CanvasFixtureURLProtocol.methods = [] }
        let api = APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-only" }, session: session)
        let store = AgentSurfaceStore(api: api, authenticated: { true })
        var old = try snapshot()
        old.sessionId = store.sessionId
        _ = try store.receive(old)
        var fresh = old
        fresh.instanceId = UUID().uuidString.lowercased(); fresh.revision = 0
        fresh.surfaces = []; fresh.approval = nil; fresh.workout = nil; fresh.receipt = nil
        CanvasFixtureURLProtocol.response = try JSONEncoder().encode(fresh)
        CanvasFixtureURLProtocol.methods = []
        store.draft = "Log this fixture meal"
        await store.send()
        await store.instanceRecoveryTask?.value
        XCTAssertEqual(CanvasFixtureURLProtocol.methods, ["POST", "GET"])
        XCTAssertEqual(store.envelope?.instanceId, fresh.instanceId)
        XCTAssertNil(store.errorMessage)
        XCTAssertNotNil(store.recoveryNotice)
        XCTAssertEqual(store.draft, "Log this fixture meal", "Uncertain write is not acknowledged or replayed")
        XCTAssertFalse(store.busy)
        XCTAssertNil(store.envelope?.approval)
    }

    func testRepeatedFencedEventsCoalesceAndResetCancelsReadback() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); CanvasFixtureURLProtocol.response = nil; CanvasFixtureURLProtocol.methods = [] }
        let api = APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-only" }, session: session)
        let store = AgentSurfaceStore(api: api, authenticated: { true })
        var original = try snapshot()
        original.sessionId = store.sessionId
        _ = try store.receive(original)
        var fresh = original
        fresh.instanceId = UUID().uuidString.lowercased()
        CanvasFixtureURLProtocol.response = try JSONEncoder().encode(fresh)
        CanvasFixtureURLProtocol.methods = []
        for _ in 0..<5 { XCTAssertThrowsError(try store.receive(fresh)) }
        await store.instanceRecoveryTask?.value
        XCTAssertEqual(CanvasFixtureURLProtocol.methods, ["GET"])
        XCTAssertEqual(store.envelope?.instanceId, fresh.instanceId)
        // Rejected late event schedules readback, but a reset must invalidate it.
        XCTAssertThrowsError(try store.receive(original))
        let pending = store.instanceRecoveryTask
        store.reset()
        await pending?.value
        XCTAssertEqual(CanvasFixtureURLProtocol.methods, ["GET"])
        XCTAssertNil(store.envelope)
    }

    func testSessionIdentitySurvivesLaunchButCannotCrossAccountOrBackend() throws {
        let suite = "canvas-test-" + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let identity = AgentSessionIdentity(defaults: defaults)
        let a = APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-account-a" })
        let b = APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-account-b" })
        let otherBackend = APIClient(baseURL: URL(string: "https://untrusted.invalid"), tokenProvider: { "fixture-account-a" })
        let first = identity.session(for: a.canvasIdentityScope)
        XCTAssertEqual(AgentSessionIdentity(defaults: defaults).session(for: a.canvasIdentityScope), first)
        XCTAssertNotEqual(a.canvasIdentityScope, "fixture-account-a")
        XCTAssertNotEqual(identity.session(for: b.canvasIdentityScope), first)
        XCTAssertNotEqual(identity.session(for: otherBackend.canvasIdentityScope), first)
        identity.clear()
        XCTAssertNotEqual(identity.session(for: a.canvasIdentityScope), first)
        XCTAssertNotEqual(identity.session(for: nil), identity.session(for: nil))
        let store = AgentSurfaceStore(api: a, authenticated: { true }, identity: identity)
        XCTAssertEqual(store.sessionId, AgentSurfaceStore(api: a, authenticated: { true }, identity: identity).sessionId)
        let previous = store.sessionId
        store.reset()
        XCTAssertNotEqual(store.sessionId, previous)
        XCTAssertNotEqual(identity.session(for: a.canvasIdentityScope), previous)
    }

    func testAccountChangeCannotSendPreviousAccountsDraft() async throws {
        let suite = "canvas-test-" + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite); CanvasFixtureURLProtocol.methods = [] }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        var token = "fixture-account-a"
        let api = APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { token }, session: session)
        let store = AgentSurfaceStore(api: api, authenticated: { true }, identity: AgentSessionIdentity(defaults: defaults))
        store.draft = "Log previous account meal"
        token = "fixture-account-b"
        CanvasFixtureURLProtocol.methods = []
        await store.send()
        XCTAssertTrue(store.draft.isEmpty)
        XCTAssertTrue(CanvasFixtureURLProtocol.methods.isEmpty)
    }

    func testRepeatedMealsCoachNavigation() {
        let store = AgentSurfaceStore(authenticated: { true })
        var destination = 2
        for _ in 0..<2 {
            store.showsMeals = true
            destination = 1 // TodayView's Coach link
            destination = store.handleLegacyDestination(destination)
            XCTAssertEqual(destination, 2)
            XCTAssertFalse(store.showsMeals)
            XCTAssertFalse(store.showsLegacyCoach, "Do not present two sheets in one transaction")
            store.completeLegacyNavigation()
            XCTAssertTrue(store.showsLegacyCoach)
            store.showsLegacyCoach = false
            store.completeLegacyNavigation()
            XCTAssertFalse(store.showsLegacyCoach)
        }
    }

    func testDayReadinessAndTimingDecodeSharedServerFixture() throws {
        struct Case: Decodable { let payload: DayPayload }
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "day-readiness", withExtension: "json"))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let cases = try decoder.decode([Case].self, from: Data(contentsOf: url))
        XCTAssertEqual(cases.count, 3)
        XCTAssertEqual(cases.map { $0.payload.hasTargets }, [false, false, true])
        XCTAssertEqual(cases[0].payload.targets.calories, 2400, "Default values do not mean configured targets")
        for item in cases {
            XCTAssertTrue(try XCTUnwrap(item.payload.dayTiming).isValid)
            XCTAssertEqual(item.payload.dayTiming?.rolloverHour, 4)
            XCTAssertEqual(item.payload.dayTiming?.effectiveDate, "2026-09-16")
        }
    }

    func testFreshClaimAndMissingTargetsStayInEstablishedOnboarding() {
        // A local fresh-claim bit or contradictory display calories cannot
        // replace affirmative server readiness.
        XCTAssertEqual(AgentRootRoute.initial(hasTargets: true, calories: 2000, hasPlan: true), .canvas)
        XCTAssertEqual(AgentRootRoute.initial(hasTargets: false, calories: 2400, hasPlan: true), .onboarding)
        XCTAssertEqual(AgentRootRoute.initial(hasTargets: nil, calories: nil, hasPlan: nil), .loading)
        XCTAssertEqual(AgentRootRoute.initial(hasTargets: true, calories: 0, hasPlan: true), .loading)
        XCTAssertEqual(AgentRootRoute.initial(hasTargets: true, calories: 2000, hasPlan: false), .onboarding)
        XCTAssertEqual(AgentRootRoute.initial(hasTargets: true, calories: 2000, hasPlan: true), .canvas)
        XCTAssertFalse(AgentRootRoute.isReady(hasTargets: false, calories: 2400, hasPlan: true))
        XCTAssertTrue(AgentRootRoute.isReady(hasTargets: true, calories: 2000, hasPlan: true))
    }

    func testUnknownReadinessRetriesThenRecoversWithoutRelaunch() async {
        let loader = AgentRootLoader()
        var calls = 0
        var waits: [Double] = []
        await loader.load(readiness: {
            calls += 1
            return .loading
        }, sleep: { waits.append($0) })
        XCTAssertEqual(calls, 3)
        XCTAssertEqual(waits, [5, 15])
        XCTAssertEqual(loader.route, .loading)
        XCTAssertNotNil(loader.errorMessage)
        XCTAssertFalse(loader.isLoading)
        // Same loader: Retry and scene-active both call this, not a relaunch.
        await loader.load(readiness: { .canvas }, sleep: { _ in })
        XCTAssertEqual(loader.route, .canvas)
        XCTAssertNil(loader.errorMessage)
    }

    func testRootOnboardingLatchesUntilExplicitCompletion() async {
        let loader = AgentRootLoader()
        await loader.load(readiness: { .onboarding }, sleep: { _ in })
        XCTAssertEqual(loader.route, .onboarding)
        await loader.load(readiness: { .canvas }, sleep: { _ in })
        XCTAssertEqual(loader.route, .onboarding, "Do not unmount an unfinished exercise picker")
        loader.completeOnboarding()
        XCTAssertEqual(loader.route, .canvas)
    }

    func testLoadingAndOnboardingShareToastLifetimeAndErrorClearingAcrossTransition() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer {
            session.invalidateAndCancel()
            CanvasFixtureURLProtocol.responder = nil
            CanvasFixtureURLProtocol.response = nil
            CanvasFixtureURLProtocol.methods = []
        }
        let api = APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-only" }, session: session)
        let app = AppStore(api: api)
        let loader = AgentRootLoader()
        // Real AppStore error path; the one shared root binding clears this
        // value, rather than leaving it for a later Canvas-only presenter.
        CanvasFixtureURLProtocol.response = nil
        await app.saveBrief("Fixture note")
        XCTAssertEqual(loader.route, .loading)
        XCTAssertNotNil(app.errorMessage)
        app.errorMessage = nil // the root alert's dismiss/OK action
        await loader.load(readiness: { .onboarding }, sleep: { _ in })
        XCTAssertNil(app.errorMessage)
        CanvasFixtureURLProtocol.responder = { _ in
            try JSONSerialization.data(withJSONObject: ["text": "Fixture note", "date": "2026-09-17"])
        }
        await app.saveBrief("Fixture note")
        XCTAssertEqual(loader.route, .onboarding)
        XCTAssertEqual(app.toast, "Note saved")
        loader.completeOnboarding()
        XCTAssertEqual(app.toast, "Note saved", "Transition neither duplicates nor restarts notification state")
        try await Task.sleep(for: .milliseconds(2200))
        XCTAssertNil(app.toast, "Same AppStore timer clears a toast after either route")
        XCTAssertNil(app.errorMessage)
    }

    func testColdStartReadinessRecoversThroughRealAppStoreGETs() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer {
            session.invalidateAndCancel()
            CanvasFixtureURLProtocol.responder = nil
            CanvasFixtureURLProtocol.methods = []
        }
        let fixtureURL = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "day-readiness", withExtension: "json"))
        let cases = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as? [[String: Any]])
        let day = try JSONSerialization.data(withJSONObject: XCTUnwrap(cases.last?["payload"]))
        var failuresRemaining = 2
        CanvasFixtureURLProtocol.responder = { request in
            if failuresRemaining > 0 { throw URLError(.cannotConnectToHost) }
            let path = request.url?.path ?? ""
            if path == "/api/today" || path.hasPrefix("/api/day/") { return day }
            let payload: [String: Any]
            switch path {
            case "/api/presets": payload = ["presets": []]
            case "/api/brief": payload = ["date": "2026-09-16", "text": NSNull()]
            case "/api/exercises": payload = ["exercises": []]
            case "/api/plan": payload = ["rotation": ["Upper"], "upcoming": [], "core": [], "has_plan": true]
            case "/api/workout-stats":
                payload = ["today": ["date": "2026-09-16", "entries": 0, "exercises": []],
                    "week": ["week_start": "2026-09-14", "week_label": "Fixture week", "days_logged": 0, "total_sets": 0, "total_volume": 0, "streak_weeks": 0],
                    "coverage": ["muscle_groups": [:], "workout_types": [:], "untouched": []],
                    "prs": [], "plan": ["today": ["type": "Upper", "exercises": []], "next": []]]
            default:
                guard path.hasPrefix("/api/workouts/") else { throw URLError(.badURL) }
                payload = ["date": "2026-09-16", "day_label": "Today", "workouts": []]
            }
            return try JSONSerialization.data(withJSONObject: payload)
        }
        CanvasFixtureURLProtocol.methods = []
        let api = APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-only" }, session: session)
        let app = AppStore(api: api)
        let loader = AgentRootLoader()
        var routes: [AgentRootRoute] = []
        await loader.load(readiness: {
            await app.loadAll(reportErrors: false)
            XCTAssertNil(app.errorMessage, "Retry feedback must stay inline")
            let route = app.accountRoute
            routes.append(route)
            return route
        }, sleep: { _ in failuresRemaining -= 1 })
        XCTAssertEqual(routes, [.loading, .loading, .canvas])
        XCTAssertNil(app.errorMessage)
        // Exhaustion and explicit Retry use the same real AppStore path.
        app.reset()
        let retryLoader = AgentRootLoader()
        failuresRemaining = 10
        await retryLoader.load(readiness: {
            await app.loadAll(reportErrors: false)
            XCTAssertNil(app.errorMessage)
            return app.accountRoute
        }, sleep: { _ in })
        XCTAssertNotNil(retryLoader.errorMessage)
        failuresRemaining = 0
        await retryLoader.load(readiness: {
            await app.loadAll(reportErrors: false)
            XCTAssertNil(app.errorMessage)
            return app.accountRoute
        }, sleep: { _ in })
        XCTAssertEqual(retryLoader.route, .canvas)
        XCTAssertNil(retryLoader.errorMessage)
        XCTAssertEqual(loader.route, .canvas)
        XCTAssertNil(loader.errorMessage)
        XCTAssertTrue(CanvasFixtureURLProtocol.methods.allSatisfy { $0 == "GET" })
        let successfulResponder = CanvasFixtureURLProtocol.responder
        CanvasFixtureURLProtocol.responder = { request in
            if request.url?.path == "/api/brief" { throw URLError(.cannotConnectToHost) }
            return try XCTUnwrap(successfulResponder)(request)
        }
        app.presets = [Preset(name: "Stale fixture", emoji: "", calories: 1, protein: 0, carbs: 0, fat: 0, fiber: 0, meal: "Snack", sortOrder: 0)]
        await app.loadDay(reportErrors: false)
        XCTAssertTrue(app.presets.isEmpty, "Brief-only failure cannot discard the successful preset refresh")
        XCTAssertNil(app.errorMessage)
        // A visible load error clears on recovery, but a write error does not.
        CanvasFixtureURLProtocol.responder = { _ in throw URLError(.cannotConnectToHost) }
        await app.loadDay()
        XCTAssertNotNil(app.errorMessage)
        CanvasFixtureURLProtocol.responder = successfulResponder
        await app.loadDay()
        XCTAssertNil(app.errorMessage)
        CanvasFixtureURLProtocol.responder = { _ in throw URLError(.cannotConnectToHost) }
        await app.saveBrief("Fixture")
        let actionError = app.errorMessage
        XCTAssertNotNil(actionError)
        CanvasFixtureURLProtocol.responder = successfulResponder
        await app.loadAll()
        XCTAssertEqual(app.errorMessage, actionError)
    }

    func testCancelledReadinessDoesNotPublishOrRetry() async {
        let loader = AgentRootLoader()
        var calls = 0
        await loader.load(readiness: { calls += 1; return .loading }, sleep: { _ in throw CancellationError() })
        XCTAssertEqual(calls, 1)
        XCTAssertEqual(loader.route, .loading)
        XCTAssertNil(loader.errorMessage)
        XCTAssertFalse(loader.isLoading)
    }

    func testLoggerPreservesCustomAssignedTypeAndCanonicalChoices() throws {
        XCTAssertEqual(WorkoutLoggerType.value(for: "Upper"), "Upper")
        XCTAssertEqual(WorkoutLoggerType.choices(for: "Upper"), CanonicalWorkoutType.all + ["Upper"])
        XCTAssertEqual(WorkoutLoggerType.value(for: "push"), "Push")
        XCTAssertEqual(WorkoutLoggerType.choices(for: "Push"), CanonicalWorkoutType.all)
        for invalid in ["", "<script>", "Upper\n", String(repeating: "x", count: 81)] {
            XCTAssertFalse(WorkoutLoggerType.isValid(invalid))
            XCTAssertEqual(WorkoutLoggerType.value(for: invalid), "")
        }
        let body = LogWorkoutBody(exercise: "Fixture press", sets: [WorkoutSet(weight: 10, reps: 8)],
            workoutType: WorkoutLoggerType.value(for: "Upper"), date: nil)
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(body)) as? [String: Any])
        XCTAssertEqual(json["workout_type"] as? String, "Upper")
    }

    func testBriefUsesAdoptedCurrentDateAndPreservesHistoricalSelection() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer {
            session.invalidateAndCancel()
            CanvasFixtureURLProtocol.responder = nil
            CanvasFixtureURLProtocol.urls = []
            CanvasFixtureURLProtocol.methods = []
        }
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "day-readiness", withExtension: "json"))
        let cases = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [[String: Any]])
        var payload = try XCTUnwrap(cases.last?["payload"] as? [String: Any])
        payload["day_timing"] = ["time_zone": "Asia/Tokyo", "rollover_hour": 0, "effective_date": "2026-09-17"]
        payload["date"] = "2026-09-17"
        let currentData = try JSONSerialization.data(withJSONObject: payload)
        payload["date"] = "2026-09-01"
        let historyData = try JSONSerialization.data(withJSONObject: payload)
        CanvasFixtureURLProtocol.responder = { request in
            switch request.url?.path {
            case "/api/today": return currentData
            case "/api/day/2026-09-01": return historyData
            case "/api/presets": return Data(#"{"presets":[]}"#.utf8)
            case "/api/brief":
                let components = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }
                let date = components?.queryItems?.first(where: { $0.name == "date" })?.value ?? "missing"
                return try JSONSerialization.data(withJSONObject: ["text": "Fixture brief", "date": date])
            default: throw URLError(.badURL)
            }
        }
        CanvasFixtureURLProtocol.urls = []
        let api = APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-only" }, session: session)
        let app = AppStore(api: api)
        await app.loadDay()
        XCTAssertNil(app.errorMessage)
        XCTAssertEqual(app.dateString, "2026-09-17")
        XCTAssertEqual(app.brief?.date, "2026-09-17")
        let requests = CanvasFixtureURLProtocol.urls
        XCTAssertLessThan(try XCTUnwrap(requests.firstIndex(where: { $0.path == "/api/today" })),
                          try XCTUnwrap(requests.firstIndex(where: { $0.path == "/api/brief" })))
        app.selectedDate = try XCTUnwrap(app.dayPolicy.date(from: "2026-09-01"))
        await app.loadDay()
        XCTAssertEqual(app.dateString, "2026-09-01")
        XCTAssertEqual(app.brief?.date, "2026-09-01")
        XCTAssertTrue(CanvasFixtureURLProtocol.methods.allSatisfy { $0 == "GET" })
    }

    func testLegacyServerFallsBackWithoutInventingOnboarding() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); CanvasFixtureURLProtocol.responder = nil }
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "day-readiness", withExtension: "json"))
        let cases = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [[String: Any]])
        let newPayload = try XCTUnwrap(cases.last?["payload"] as? [String: Any])
        var oldPayload = newPayload
        for key in ["has_targets", "day_timing", "canvas_protocol"] { oldPayload.removeValue(forKey: key) }
        var payload = oldPayload
        CanvasFixtureURLProtocol.responder = { request in
            if request.url?.path == "/api/today" { return try JSONSerialization.data(withJSONObject: payload) }
            throw URLError(.badURL)
        }
        let app = AppStore(api: APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-only" }, session: session))
        XCTAssertEqual(app.accountRoute, .loading)
        await app.loadDay(reportErrors: false)
        XCTAssertEqual(app.accountRoute, .compatibility)
        XCTAssertNil(app.accountHasTargets, "Never infer account setup from legacy fallback calories")
        app.reset()
        payload = newPayload
        payload["canvas_protocol"] = "unknown.version"
        await app.loadDay(reportErrors: false)
        XCTAssertEqual(app.accountRoute, .compatibility)
        app.reset()
        payload = newPayload
        app.plan = WorkoutPlanPayload(rotation: [], lastWorkout: nil, upcoming: [], core: [], hasPlan: true)
        await app.loadDay(reportErrors: false)
        XCTAssertEqual(app.accountRoute, .canvas)
    }

    func testWarmLoadsOverlapAndRuntimePolicyChangeRefetchesWorkoutDay() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer {
            CanvasFixtureURLProtocol.holdToday = false
            CanvasFixtureURLProtocol.pendingToday?.finishLoading()
            CanvasFixtureURLProtocol.pendingToday = nil
            CanvasFixtureURLProtocol.observedRequest = nil
            CanvasFixtureURLProtocol.responder = nil
            session.invalidateAndCancel()
        }
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "day-readiness", withExtension: "json"))
        let cases = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [[String: Any]])
        var payload = try XCTUnwrap(cases.last?["payload"] as? [String: Any])
        let policy = LoggingDayPolicy.bootstrap
        let today = policy.formatter.string(from: policy.currentDay())
        payload["date"] = today
        payload["day_timing"] = ["time_zone": policy.timeZone, "rollover_hour": policy.rolloverHour, "effective_date": today]
        CanvasFixtureURLProtocol.responder = { request in
            switch request.url?.path {
            case "/api/today": return try JSONSerialization.data(withJSONObject: payload)
            case "/api/presets": return Data(#"{"presets":[]}"#.utf8)
            case "/api/brief":
                let key = URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "date" })?.value ?? ""
                return try JSONSerialization.data(withJSONObject: ["date": key, "text": NSNull()])
            case "/api/exercises": return Data(#"{"exercises":[]}"#.utf8)
            case "/api/plan": return Data(#"{"rotation":[],"upcoming":[],"core":[],"has_plan":true}"#.utf8)
            case "/api/workout-stats":
                return Data(#"{"today":{"date":"2026-09-17","entries":0,"exercises":[]},"week":{"week_start":"2026-09-14","week_label":"Fixture","days_logged":0,"total_sets":0,"total_volume":0,"streak_weeks":0},"coverage":{"muscle_groups":{},"workout_types":{},"untouched":[]},"prs":[],"plan":{"today":{"type":"Push","exercises":[]},"next":[]}}"#.utf8)
            default:
                guard request.url?.path.hasPrefix("/api/workouts/") == true else { throw URLError(.badURL) }
                return Data(#"{"date":"2026-09-17","day_label":"Today","workouts":[]}"#.utf8)
            }
        }
        let app = AppStore(api: APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-only" }, session: session))
        await app.loadAll(reportErrors: false)
        CanvasFixtureURLProtocol.urls = []
        let overlap = expectation(description: "Workouts start while the warm day response is held")
        overlap.assertForOverFulfill = false
        CanvasFixtureURLProtocol.holdToday = true
        CanvasFixtureURLProtocol.observedRequest = { request in
            if request.url?.path.hasPrefix("/api/workouts/") == true { overlap.fulfill() }
        }
        let load = Task { await app.loadAll(reportErrors: false) }
        await fulfillment(of: [overlap], timeout: 2)
        // A runtime policy update corrects any parallel old-date workout read.
        payload["date"] = "2026-09-18"
        payload["day_timing"] = ["time_zone": "Asia/Tokyo", "rollover_hour": 0, "effective_date": "2026-09-18"]
        CanvasFixtureURLProtocol.holdToday = false
        CanvasFixtureURLProtocol.pendingToday?.finishLoading()
        CanvasFixtureURLProtocol.pendingToday = nil
        await load.value
        XCTAssertEqual(app.dateString, "2026-09-18")
        XCTAssertEqual(app.brief?.date, "2026-09-18")
        XCTAssertEqual(CanvasFixtureURLProtocol.urls.filter { $0.path.hasPrefix("/api/workouts/") }.last?.path, "/api/workouts/2026-09-18")
        XCTAssertNil(app.errorMessage)
    }

    func testDayPolicyCacheInvalidationAndDST() throws {
        let policy = LoggingDayPolicy.bootstrap
        let original = policy.formatter
        for _ in 0..<100 {
            XCTAssertTrue(original === policy.formatter)
            _ = policy.currentDay()
        }
        var nextDate = policy
        nextDate.effectiveDate = "2026-09-17"
        XCTAssertTrue(original === nextDate.formatter, "Date-only updates do not invalidate resources")
        var changed = nextDate
        changed.timeZone = "Asia/Tokyo"
        XCTAssertFalse(original === changed.formatter)
        XCTAssertEqual(changed.calendar.timeZone.identifier, "Asia/Tokyo")
        let tokyo = changed.formatter
        changed.rolloverHour = 0
        XCTAssertFalse(tokyo === changed.formatter, "Every policy-key change invalidates atomically")
        let iso = ISO8601DateFormatter()
        let instant = try XCTUnwrap(iso.date(from: "2026-03-08T08:00:00Z"))
        XCTAssertEqual(policy.formatter.string(from: policy.currentDay(now: instant)), "2026-03-08")
        let fall = try XCTUnwrap(iso.date(from: "2026-11-01T08:59:00Z"))
        XCTAssertEqual(policy.formatter.string(from: policy.currentDay(now: fall)), "2026-10-31")
        XCTAssertEqual(changed.formatter.string(from: changed.currentDay(now: fall)), "2026-11-01")
    }

    func testHistoricalDayCannotReopenAccountOnboarding() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); CanvasFixtureURLProtocol.responder = nil }
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "day-readiness", withExtension: "json"))
        let cases = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [[String: Any]])
        var current = try XCTUnwrap(cases.last?["payload"] as? [String: Any])
        let policy = LoggingDayPolicy.bootstrap
        let today = policy.formatter.string(from: policy.currentDay())
        current["date"] = today
        current["day_timing"] = ["time_zone": policy.timeZone, "rollover_hour": policy.rolloverHour, "effective_date": today]
        var history = current
        history["date"] = "2026-01-01"
        history["has_targets"] = false
        CanvasFixtureURLProtocol.responder = { request in
            switch request.url?.path {
            case "/api/today": return try JSONSerialization.data(withJSONObject: current)
            case "/api/day/2026-01-01": return try JSONSerialization.data(withJSONObject: history)
            case "/api/presets": return Data(#"{"presets":[]}"#.utf8)
            case "/api/brief": return Data(#"{"text":null,"date":"2026-01-01"}"#.utf8)
            default: throw URLError(.badURL)
            }
        }
        let app = AppStore(api: APIClient(baseURL: URL(string: "https://fixture.invalid"), tokenProvider: { "fixture-only" }, session: session))
        XCTAssertEqual(app.accountRoute, .loading, "Unknown is not onboarding")
        app.plan = WorkoutPlanPayload(rotation: [], lastWorkout: nil, upcoming: [], core: [], hasPlan: true)
        await app.loadDay()
        XCTAssertEqual(app.accountRoute, .canvas)
        app.selectedDate = try XCTUnwrap(app.dayPolicy.date(from: "2026-01-01"))
        await app.loadDay()
        XCTAssertEqual(app.day?.hasTargets, false)
        XCTAssertEqual(app.accountRoute, .canvas, "Standard Coach uses account readiness, not historical targets")
        app.reset()
        XCTAssertEqual(app.accountRoute, .loading, "Readiness must not cross accounts")
    }

    func testServerDayPolicyChangesTimezoneWithoutMovingHistoricalDay() throws {
        let formatter = ISO8601DateFormatter()
        let policy = LoggingDayPolicy(timeZone: "Asia/Tokyo", rolloverHour: 0, effectiveDate: "2026-09-17")
        XCTAssertTrue(policy.isValid)
        let now = try XCTUnwrap(formatter.date(from: "2026-09-16T15:00:00Z"))
        XCTAssertEqual(policy.formatter.string(from: policy.currentDay(now: now)), "2026-09-17")
        let historical = try XCTUnwrap(policy.date(from: "2026-09-01"))
        XCTAssertEqual(policy.formatter.string(from: historical), "2026-09-01")
        XCTAssertFalse(LoggingDayPolicy(timeZone: "Unknown/Zone", rolloverHour: 4, effectiveDate: "2026-09-17").isValid)
        XCTAssertFalse(LoggingDayPolicy(timeZone: "Asia/Tokyo", rolloverHour: 24, effectiveDate: "2026-09-17").isValid)
    }

    func testForegroundDateUsesFourAMEasternBoundary() throws {
        let formatter = ISO8601DateFormatter()
        let store = AppStore()
        // Includes spring-forward and fall-back; subtracting four elapsed hours
        // would pick the wrong day at the spring-forward 4am boundary.
        let cases = [("2026-09-17T07:59:00Z", "2026-09-16"),
                     ("2026-09-17T08:00:00Z", "2026-09-17"),
                     ("2026-03-08T07:59:00Z", "2026-03-07"),
                     ("2026-03-08T08:00:00Z", "2026-03-08"),
                     ("2026-11-01T08:59:00Z", "2026-10-31"),
                     ("2026-11-01T09:00:00Z", "2026-11-01")]
        for (instant, expected) in cases {
            store.selectCurrentDay(now: try XCTUnwrap(formatter.date(from: instant)))
            XCTAssertEqual(store.dateString, expected)
        }
        store.selectedDate = try XCTUnwrap(formatter.date(from: "2026-09-01T16:00:00Z"))
        XCTAssertEqual(store.dateString, "2026-09-01", "Historical navigation stays explicit until foreground/current-day intent")
    }

    func testSwapRequestPreservesComposerDraft() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CanvasFixtureURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); CanvasFixtureURLProtocol.response = nil }
        let api = APIClient(baseURL: URL(string: "https://fixture.invalid"),
            tokenProvider: { "fixture-only" }, session: session)
        let store = AgentSurfaceStore(api: api, authenticated: { true })
        store.draft = "Breakfast was two eggs, still editing"
        var response = try snapshot()
        response.sessionId = store.sessionId
        CanvasFixtureURLProtocol.response = try JSONEncoder().encode(response)
        await store.requestSwap("Cable Flyes")
        XCTAssertNil(store.errorMessage)
        XCTAssertEqual(store.draft, "Breakfast was two eggs, still editing")
        // A failed preview must also leave the unrelated text intact.
        CanvasFixtureURLProtocol.response = nil
        await store.requestSwap("Fixture press")
        XCTAssertNotNil(store.errorMessage)
        XCTAssertEqual(store.draft, "Breakfast was two eggs, still editing")
    }

    func testLiveAndHTTPEnvelopeApplyToSameReducer() throws {
        let snapshot = try snapshot()
        let store = AgentSurfaceStore(authenticated: { true })
        // A different authenticated session must not publish another user's view.
        XCTAssertThrowsError(try store.receive(snapshot))
        var state = AgentSurfaceState(sessionId: snapshot.sessionId)
        XCTAssertTrue(try state.apply(snapshot))
        var live = snapshot
        live.revision += 1
        let encoded = try JSONEncoder().encode(live)
        let object = try JSONSerialization.jsonObject(with: encoded)
        let wire = try JSONSerialization.data(withJSONObject: ["type": "agent.canvas", "canvas": object])
        guard case .canvas(let decoded) = try LiveCoachWireCodec.decode(wire) else { return XCTFail("Missing canvas event") }
        XCTAssertTrue(try state.apply(decoded))
        XCTAssertEqual(state.envelope?.revision, live.revision)
        XCTAssertFalse(try state.apply(snapshot), "Late HTTP response must not overwrite newer voice output")
    }

    func testLiveRequestOptsIntoSameUUIDAndKeepsBearerOffURL() throws {
        let id = UUID().uuidString.lowercased()
        let url = try XCTUnwrap(URL(string: "https://fixture.invalid"))
        let request = try LiveCoachWebSocketTransport.makeRequest(baseURL: url, token: "fixture-only-not-a-credential", canvasSessionId: id)
        XCTAssertEqual(request.url?.path, "/api/agent-canvas/\(id)/live")
        XCTAssertNil(request.url?.query)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fixture-only-not-a-credential")
        XCTAssertThrowsError(try LiveCoachWebSocketTransport.makeRequest(baseURL: url, token: nil, canvasSessionId: id))
    }

    func testPinnedRendererBuildsClosedNativeChildrenOnly() throws {
        let snapshot = try snapshot()
        let surface = try XCTUnwrap(snapshot.surfaces.first)
        let model = try AgentSurfaceRenderer.makeModel(surface)
        XCTAssertEqual(model.componentTree?.children.count, 1)
        XCTAssertEqual(model.componentTree?.children.first?.baseComponentId, "macros")
    }
}
