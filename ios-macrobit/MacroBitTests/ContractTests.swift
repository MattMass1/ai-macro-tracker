import XCTest
@testable import MacroBit

final class FixtureProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (Int, Data))?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            guard let handler = Self.handler else { throw URLError(.badServerResponse) }
            let (status, data) = try handler(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}

@MainActor
final class ContractTests: XCTestCase {
    let dayJSON = Data(#"{"date":"2026-11-16","day_label":"Monday","day_timing":{"time_zone":"America/New_York","rollover_hour":4,"effective_date":"2026-11-16"},"has_targets":false,"macros_complete":false,"macro_note":"Protein is unknown","totals":{"calories":100,"protein":0,"carbs":0,"fat":0,"fiber":0},"targets":{"calories":2000,"protein":150,"carbs":200,"fat":60,"fiber":25},"remaining":{"calories":1900,"protein":150,"carbs":200,"fat":60,"fiber":25},"meals":[{"id":"fixture-meal","name":"Calorie-only","meal":"Snack","calories":100,"protein":null,"carbs":null,"fat":null,"fiber":null}]}"#.utf8)

    func client(onUnauthorized: @escaping () -> Void = {}) -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [FixtureProtocol.self]
        return APIClient(baseURL: URL(string: "https://fixture.invalid")!, tokenProvider: { "not-a-credential" }, session: URLSession(configuration: config), onUnauthorized: onUnauthorized)
    }

    func body(_ request: URLRequest) throws -> [String: Any] {
        if let data = request.httpBody { return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any]) }
        let stream = try XCTUnwrap(request.httpBodyStream)
        stream.open(); defer { stream.close() }
        var data = Data(); var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; data.append(contentsOf: buffer.prefix(count)) }
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    /// The backend accepts only canonical lowercase UUIDs; Foundation's uuidString is uppercase.
    func testChatSendsLowercaseSessionAndTurnIDs() async throws {
        var path = "", turnID = ""
        FixtureProtocol.handler = { request in
            path = request.url!.path; turnID = try self.body(request)["turn_id"] as! String
            return (200, Data(#"{"reply":"Preview","approval":null,"quickReplies":[]}"#.utf8))
        }
        let session = UUID(), turn = UUID()
        _ = try await client().turn(sessionID: session, turnID: turn, message: "a banana")
        XCTAssertEqual(path, "/api/agent-canvas/\(session.uuidString.lowercased())/turn")
        XCTAssertEqual(turnID, turn.uuidString.lowercased())
        XCTAssertEqual(turnID, turnID.lowercased())
    }

    func testEveryAllowedRouteHeadersKeysAndTimeouts() async throws {
        var calls = [(String, String)]()
        FixtureProtocol.handler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer not-a-credential")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            XCTAssertEqual(request.value(forHTTPHeaderField: "X-Nutrient-Contract"), "nullable-v1")
            let path = request.url!.path
            calls.append((request.httpMethod!, path))
            XCTAssertEqual(request.timeoutInterval, path.contains("agent-canvas") ? 120 : 45)
            if path.contains("agent-canvas") {
                let json = try self.body(request)
                XCTAssertEqual(Set(json.keys), ["turn_id", "message"])
                return (200, Data(#"{"reply":"Preview","approval":null,"quickReplies":["Log it","Cancel"]}"#.utf8))
            }
            if path == "/api/log" {
                let json = try self.body(request)
                XCTAssertEqual(Set(json.keys), ["name","calories","protein","carbs","fat","fiber","macro_source","meal","date"])
                XCTAssertEqual(json["date"] as? String, "2026-11-16")
            }
            if path == "/api/log-preset" {
                XCTAssertEqual(Set(try self.body(request).keys), ["preset_name","servings","meal","date"])
            }
            if path == "/api/presets" { return (200, Data(#"{"presets":[]}"#.utf8)) }
            if path == "/api/trends" { XCTAssertEqual(request.url!.query, "days=7"); return (200, Data(#"{"days":[],"weekly":[],"weight":{"current_kg":0,"goal_kg":0}}"#.utf8)) }
            return (200, self.dayJSON)
        }
        let api = client()
        _ = try await api.today(); _ = try await api.day("2026-11-16"); _ = try await api.presets()
        _ = try await api.logMeal(LogMealBody(name: "Test", calories: 100, protein: 1, carbs: 2, fat: 3, fiber: 4, macroSource: "Nutrition label", meal: "Snack", day: "2026-11-16"))
        _ = try await api.logPreset(LogPresetBody(presetName: "Preset", servings: 1, meal: "Lunch", day: "2026-11-16"))
        _ = try await api.deleteMeal("fixture-meal"); _ = try await api.trends(days: 7)
        _ = try await api.turn(sessionID: UUID(), turnID: UUID(), message: "a banana")
        XCTAssertEqual(calls.count, 8)
        XCTAssertEqual(Set(calls.map { $0.1 }), ["/api/today","/api/day/2026-11-16","/api/presets","/api/log","/api/log-preset","/api/meal/fixture-meal","/api/trends", calls.last!.1])
    }

    func testNullsAndAuthoritativeEffectiveDate() async throws {
        FixtureProtocol.handler = { _ in (200, self.dayJSON) }
        let store = AppStore(api: client(), vault: MemoryVault())
        await store.refreshToday()
        XCTAssertEqual(store.effectiveDate, "2026-11-16")
        XCTAssertNil(store.day?.meals.first?.protein)
        XCTAssertEqual(nutrientText(store.day?.meals.first?.protein), "Unknown")
        XCTAssertFalse(try XCTUnwrap(store.day).nutrientsComplete)
        XCTAssertEqual(store.day?.macroNote, "Protein is unknown")
    }

    func test401SignsOutAndClearsConversation() async {
        let vault = MemoryVault(); vault.value = "not-a-credential"
        let store = AppStore(api: client(), vault: vault)
        store.isSignedIn = true
        FixtureProtocol.handler = { _ in (401, Data(#"{"error":"Revoked"}"#.utf8)) }
        await store.refreshToday()
        XCTAssertFalse(store.isSignedIn); XCTAssertNil(vault.value); XCTAssertNil(store.day)
        XCTAssertTrue(store.messages.isEmpty)
    }

    func testQuickRepliesSendExactLabelAndRefreshAfterReply() async {
        var messages = [String](); var todayReads = 0
        FixtureProtocol.handler = { request in
            if request.url!.path == "/api/today" { todayReads += 1; return (200, self.dayJSON) }
            messages.append(try self.body(request)["message"] as! String)
            return (200, Data(#"{"reply":"Preview","approval":null,"quickReplies":["Log it","Cancel"]}"#.utf8))
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        await store.send("a banana"); await store.sendQuickReply("Log it")
        XCTAssertEqual(messages, ["a banana", "Log it"]); XCTAssertEqual(todayReads, 2)
    }

    func testScanOnceDigitsOnlyAnd404AsChat() async {
        var sent = [String]()
        FixtureProtocol.handler = { request in
            sent.append(try self.body(request)["message"] as! String)
            return (404, Data(#"{"error":"I couldn't find that barcode."}"#.utf8))
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        let event = UUID()
        store.acceptScan("00016000275287", eventID: event)
        store.acceptScan("00016000275287", eventID: event)
        store.acceptScan("barcode: 12345678", eventID: UUID())
        await store.drainScans()
        XCTAssertEqual(sent, ["00016000275287"])
        XCTAssertEqual(store.messages.last?.text, "I couldn't find that barcode.")
        XCTAssertEqual(store.messages.last?.role, .coach)
        XCTAssertNil(store.errorMessage)
    }

    func testScanQueuedWhileSendingIsNotDropped() async {
        var sent = [String]()
        FixtureProtocol.handler = { request in
            if request.url!.path == "/api/today" { return (200, self.dayJSON) }
            sent.append(try self.body(request)["message"] as! String)
            return (200, Data(#"{"reply":"OK","quickReplies":[]}"#.utf8))
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        store.isSending = true
        store.acceptScan("00016000275287", eventID: UUID())
        await store.drainScans(); XCTAssertTrue(sent.isEmpty)
        store.isSending = false
        await store.send("how much protein today")
        XCTAssertEqual(sent, ["how much protein today", "00016000275287"])
    }

    func testTimeoutRetryReusesExactTurnAndMessage() async {
        var bodies = [[String: Any]]()
        FixtureProtocol.handler = { request in
            if request.url!.path == "/api/today" { return (200, self.dayJSON) }
            bodies.append(try self.body(request))
            if bodies.count == 1 { throw URLError(.timedOut) }
            return (200, Data(#"{"reply":"Saved once","quickReplies":[]}"#.utf8))
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        await store.send("Log it")
        XCTAssertNotNil(store.retryTurn)
        await store.retryLastTurn()
        XCTAssertEqual(bodies.count, 2)
        XCTAssertEqual(bodies[0]["turn_id"] as? String, bodies[1]["turn_id"] as? String)
        XCTAssertEqual(bodies[0]["message"] as? String, bodies[1]["message"] as? String)
        XCTAssertNil(store.retryTurn)
    }

    func test409DoesNotOfferRetryOrAutomaticallyResend() async {
        var calls = 0
        FixtureProtocol.handler = { _ in calls += 1; return (409, Data(#"{"error":"busy"}"#.utf8)) }
        let store = AppStore(api: client(), vault: MemoryVault())
        await store.send("Log it"); await store.retryLastTurn()
        XCTAssertEqual(calls, 1); XCTAssertNil(store.retryTurn)
        XCTAssertEqual(store.messages.last?.text, "Still working on your last message")
    }

    func testRejectOversizedAndTooManyQuickReplies() async throws {
        for payload in [Data(#"{"reply":"OK","quickReplies":["1","2","3","4","5"]}"#.utf8), Data((#"{"reply":""# + String(repeating: "x", count: 48_000) + #"","quickReplies":[]}"#).utf8)] {
            FixtureProtocol.handler = { _ in (200, payload) }
            do { _ = try await client().turn(sessionID: UUID(), turnID: UUID(), message: "test"); XCTFail("Must reject unreadable envelope") }
            catch { XCTAssertTrue(error is PayloadError) }
        }
    }

    func testUserRequestedResendGetsNewTurnID() async {
        var ids = [String]()
        FixtureProtocol.handler = { request in
            if request.url!.path == "/api/today" { return (200, self.dayJSON) }
            ids.append(try self.body(request)["turn_id"] as! String)
            if ids.count == 1 { throw URLError(.networkConnectionLost) }
            return (200, Data(#"{"reply":"OK","quickReplies":[]}"#.utf8))
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        await store.send("a banana"); await store.resendAsNewTurn()
        XCTAssertEqual(ids.count, 2); XCTAssertNotEqual(ids.first, ids.last)
    }

    func testSavePresetDeleteEachRefetchToday() async {
        var reads = 0
        FixtureProtocol.handler = { request in
            if request.url!.path == "/api/today" { reads += 1 }
            return (200, self.dayJSON)
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        let manual = await store.logMeal(LogMealBody(name: "Test", calories: 1, protein: 1, carbs: 1, fat: 1, fiber: 1, macroSource: "Label", meal: "Snack", day: nil))
        let preset = await store.logPreset(LogPresetBody(presetName: "Test", servings: 1, meal: "Snack", day: nil))
        let deletion = await store.deleteMeal("fixture-entry")
        XCTAssertTrue(manual); XCTAssertTrue(preset); XCTAssertTrue(deletion); XCTAssertEqual(reads, 3)
    }

    func testChatStartsEmptyAndSessionStaysStableAcrossTurns() async {
        var paths = [String](); var ids = [String]()
        FixtureProtocol.handler = { request in
            if request.url!.path == "/api/today" { return (200, self.dayJSON) }
            paths.append(request.url!.path); ids.append(try self.body(request)["turn_id"] as! String)
            return (200, Data(#"{"reply":"OK","quickReplies":[]}"#.utf8))
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        XCTAssertTrue(store.messages.isEmpty)
        await store.send("one"); await store.send("two")
        XCTAssertEqual(paths.first, paths.last); XCTAssertNotEqual(ids.first, ids.last)
        XCTAssertNotEqual(store.sessionID, AppStore(api: client(), vault: MemoryVault()).sessionID)
    }

    func test422And503RemainChatReplies() async {
        for status in [422, 503, 400, 500, 502] {
            FixtureProtocol.handler = { _ in (status, Data(#"{"error":"Nothing was saved."}"#.utf8)) }
            let store = AppStore(api: client(), vault: MemoryVault())
            await store.send("test")
            XCTAssertEqual(store.messages.last?.role, .coach)
            XCTAssertFalse(store.isSending)
            if status == 422 || status == 400 { XCTAssertEqual(store.messages.last?.text, "Nothing was saved.") }
            if status == 503 { XCTAssertTrue(store.messages.last?.text.contains("waking up") == true) }
        }
    }
}

final class MemoryVault: TokenVault {
    var value: String?
    func read() -> String? { value }
    func save(_ token: String) -> Bool { value = token; return true }
    func clear() { value = nil }
}
