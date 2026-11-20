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

    func testAppleSignInSendsTokenNonceAndNameWithoutBearer() async throws {
        FixtureProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/auth/apple")
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            let json = try self.body(request)
            XCTAssertEqual(Set(json.keys), ["identity_token", "nonce", "name"])
            XCTAssertEqual(json["nonce"] as? String, "raw-nonce")
            return (200, Data(#"{"token":"mmd_fixture","user_id":"u1","display_name":"Matt","is_new":true}"#.utf8))
        }
        let result = try await client().signInWithApple(AppleSignInBody(identityToken: "a.b.c", nonce: "raw-nonce", name: "Matt"))
        XCTAssertEqual(result, AppleSignInPayload(token: "mmd_fixture", userId: "u1", displayName: "Matt", isNew: true))
    }

    func testAppleSignInRefusalIsNotTreatedAsSignOut() async throws {
        var unauthorized = 0
        FixtureProtocol.handler = { _ in (401, Data(#"{"error":"Apple sign-in could not be verified."}"#.utf8)) }
        do { _ = try await client(onUnauthorized: { unauthorized += 1 }).signInWithApple(AppleSignInBody(identityToken: "a.b.c", nonce: "n", name: nil)); XCTFail("expected refusal") }
        catch let error as APIError { XCTAssertEqual(error.errorDescription, "Apple sign-in could not be verified.") }
        XCTAssertEqual(unauthorized, 0)
    }

    func testAppleNonceIsRandomAndHashedForApple() {
        let a = AppleNonce.make(), b = AppleNonce.make()
        XCTAssertEqual(a.count, 32); XCTAssertNotEqual(a, b)
        XCTAssertEqual(AppleNonce.sha256("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    }

    func testTurnSerializesCanonicalUUIDsWithoutChangingValuesOrMessage() async throws {
        let sessionID = try XCTUnwrap(UUID(uuidString: "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF"))
        let turnID = try XCTUnwrap(UUID(uuidString: "FEDCBAFE-DCBA-4FED-9CBA-FEDCBAFEDCBA"))
        let message = "Log ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF exactly AS IS"
        var calls = 0
        FixtureProtocol.handler = { request in
            calls += 1
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/agent-canvas/abcdefab-cdef-4abc-8def-abcdefabcdef/turn")
            let json = try self.body(request)
            XCTAssertEqual(Set(json.keys), ["turn_id", "message"])
            XCTAssertEqual(json["turn_id"] as? String, "fedcbafe-dcba-4fed-9cba-fedcbafedcba")
            XCTAssertEqual(json["message"] as? String, message)
            XCTAssertEqual(request.url?.pathComponents.dropLast().last.flatMap(UUID.init(uuidString:)), sessionID)
            XCTAssertEqual((json["turn_id"] as? String).flatMap(UUID.init(uuidString:)), turnID)
            return (200, Data(#"{"reply":"OK","quickReplies":[]}"#.utf8))
        }
        _ = try await client().turn(sessionID: sessionID, turnID: turnID, message: message)
        XCTAssertEqual(calls, 1)
        XCTAssertEqual(sessionID.uuidString, "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF")
        XCTAssertEqual(turnID.uuidString, "FEDCBAFE-DCBA-4FED-9CBA-FEDCBAFEDCBA")
    }

    func testStringRouteIdentifierPreservesCase() async throws {
        var calls = 0
        FixtureProtocol.handler = { request in
            calls += 1
            XCTAssertEqual(request.httpMethod, "DELETE")
            XCTAssertEqual(request.url?.path, "/api/meal/ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF")
            return (200, self.dayJSON)
        }
        _ = try await client().deleteMeal("ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF")
        XCTAssertEqual(calls, 1)
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

    func testMalformedSurfaceDoesNotEraseValidMetricsForm() throws {
        let data = Data(#"{"reply":"Legacy metrics","surfaces":[17,{"components":[{"component":"ProfileMetrics"}]}]}"#.utf8)
        let turn = try JSONDecoder().decode(CoachTurn.self, from: data)
        XCTAssertTrue(turn.wantsMetricsForm)
        XCTAssertEqual(turn.reply, "Legacy metrics")
    }

    func testCanonicalFourSurfaceFixtureDecodes() throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Verification/Fixtures/chat-surfaces.json"))
        let turn = try JSONDecoder().decode(CoachTurn.self, from: data)
        XCTAssertEqual(turn.surfaces.count, 4)
        XCTAssertEqual(turn.reply, "Synthetic offline fixtures. No backend writes occurred.")
        let embedded = try JSONDecoder().decode(CoachTurn.self, from: Data(OfflineFixtureProtocol.surfaceExamples.utf8))
        XCTAssertEqual(embedded.surfaces, turn.surfaces)
        XCTAssertEqual(embedded.quickReplies, turn.quickReplies)
        guard case .mealPreview(let preview) = turn.surfaces[0],
              case .mealReceipt(let receipt) = turn.surfaces[1],
              case .saveFailed(let failure) = turn.surfaces[2],
              case .dayTotals(let totals) = turn.surfaces[3] else { return XCTFail("All four discriminated kinds must decode") }
        XCTAssertEqual(preview.foods.first?.nutrition.calories, 100)
        XCTAssertNil(preview.foods.first?.nutrition.protein)
        XCTAssertNil(preview.totals.protein)
        XCTAssertEqual(preview.day_after?.totals.calories, 400)
        XCTAssertFalse(preview.macros_complete)
        XCTAssertEqual(receipt.entries.first?.id, "c1000000-0000-4000-8000-000000000001")
        XCTAssertEqual(receipt.totals, preview.totals)
        XCTAssertFalse(failure.retry_safe)
        XCTAssertEqual(totals.remaining.calories, 1600)
        XCTAssertNil(totals.remaining.protein)
    }

    func testUnknownMalformedAndPrimitiveCardsPreserveValidPeers() throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Verification/Fixtures/chat-surfaces.json"))
        let original = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let cards = try XCTUnwrap(original["surfaces"] as? [[String: Any]])
        var malformed = cards[0]
        malformed["foods"] = []
        let mixed: [String: Any] = ["reply": "Still readable", "surfaces": [17, NSNull(), ["kind": "future"], malformed, cards[1], cards[3]], "quickReplies": ["Cancel"]]
        let turn = try JSONDecoder().decode(CoachTurn.self, from: JSONSerialization.data(withJSONObject: mixed))
        XCTAssertEqual(turn.surfaces.count, 2)
        XCTAssertEqual(turn.quickReplies, ["Cancel"])
        XCTAssertEqual(turn.reply, "Still readable")
        guard case .mealReceipt = turn.surfaces[0], case .dayTotals = turn.surfaces[1] else { return XCTFail("Valid peers must keep their order") }
    }

    func testSurfaceFormattingPreservesFractionalNumbersAndNulls() {
        for value in [12.25, 400.25, 1599.75, -2.75, 0.000000001] {
            XCTAssertEqual(Double(surfaceNumber(value)), value)
        }
        XCTAssertEqual(surfaceNumber(nil), "Unknown")
        XCTAssertEqual(surfaceNutrientText(nil), "Unknown")
        XCTAssertEqual(surfaceNutrientText(12.25), "12.25g")
    }

    func testCodeOwnedPreviewNoticesAndRecipeDetailsDecode() throws {
        let notice = "Nutrition changed. Review the updated amounts and confirm again."
        let turn = try JSONDecoder().decode(CoachTurn.self, from: Data(OfflineFixtureProtocol.cardTurn(0, notice: notice, modified: true).utf8))
        guard let card = turn.surfaces.first, case .mealPreview(let preview) = card else { return XCTFail("Readable preview required") }
        XCTAssertEqual(preview.notice, notice)
        XCTAssertEqual(preview.foods.first?.modifications, "remove tomato")
        XCTAssertEqual(preview.foods.first?.recipe_note, "STANDARD recipe; nutrition unchanged for minor garnish changes")
    }

    func testInvalidNutrientOrMissingFieldFallsBackToOriginalText() throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Verification/Fixtures/chat-surfaces.json"))
        let original = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let card = try XCTUnwrap((original["surfaces"] as? [[String: Any]])?.first)
        for bad in [["calories": -1, "protein": NSNull(), "carbs": 20, "fat": 1, "fiber": 2], ["calories": 100, "carbs": 20, "fat": 1, "fiber": 2]] as [[String: Any]] {
            var malformed = card; malformed["totals"] = bad
            let turn = try JSONDecoder().decode(CoachTurn.self, from: JSONSerialization.data(withJSONObject: ["reply": "Original text", "surfaces": [malformed]]))
            XCTAssertTrue(turn.surfaces.isEmpty)
            XCTAssertEqual(turn.reply, "Original text")
        }
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

    func testSurfaceConfirmationSendsLogItOnceAndRefreshesToday() async {
        var sent = [String](); var reads = 0
        FixtureProtocol.handler = { request in
            if request.url!.path == "/api/today" { reads += 1; return (200, self.dayJSON) }
            let message = try self.body(request)["message"] as! String
            sent.append(message)
            return (200, Data(OfflineFixtureProtocol.cardTurn(message == "Log it" ? 1 : 0, labels: message == "Log it" ? [] : ["Log it", "Cancel"]).utf8))
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        await store.send("a banana")
        XCTAssertEqual(store.messages.last?.surfaces.count, 1)
        await store.sendQuickReply("Log it"); await store.sendQuickReply("Log it")
        XCTAssertEqual(sent, ["a banana", "Log it"])
        XCTAssertEqual(reads, 2)
        guard let card = store.messages.last?.surfaces.first, case .mealReceipt = card else { return XCTFail("Receipt must reach the message feed") }
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
        var paths = [String]()
        FixtureProtocol.handler = { request in
            if request.url!.path == "/api/today" { return (200, self.dayJSON) }
            paths.append(request.url!.path)
            bodies.append(try self.body(request))
            if bodies.count == 1 { throw URLError(.timedOut) }
            return (200, Data(#"{"reply":"Saved once","quickReplies":[]}"#.utf8))
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        await store.send("Log it")
        XCTAssertNotNil(store.retryTurn)
        let pendingID = store.retryTurn?.id
        await store.retryLastTurn()
        XCTAssertEqual(bodies.count, 2)
        XCTAssertEqual(paths, Array(repeating: "/api/agent-canvas/\(store.sessionID.uuidString.lowercased())/turn", count: 2))
        XCTAssertEqual(bodies[0]["turn_id"] as? String, pendingID?.uuidString.lowercased())
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

    /// Repeat and save-preset send only the entry id (plus an empty body): the server owns the numbers.
    func testRepeatAndSaveSendNoNumbers() async throws {
        var calls = [(String, String, [String: Any])]()
        FixtureProtocol.handler = { request in
            let path = request.url!.path
            if request.httpMethod == "POST" { calls.append((request.httpMethod!, path, try self.body(request))) }
            if path.hasSuffix("/save-preset") { return (200, Data(#"{"presets":[{"name":"Eggs","emoji":"🍽️","calories":140,"protein":12,"carbs":1,"fat":10,"fiber":0,"meal":"Breakfast","sort_order":1}]}"#.utf8)) }
            return (200, self.dayJSON)
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        let entry = FoodEntry(id: "c1000000-0000-4000-8000-000000000001", name: "Eggs", meal: "Breakfast", calories: 140, protein: 12, carbs: 1, fat: 10, fiber: 0)
        let repeated = await store.repeatMeal(entry)
        XCTAssertFalse(store.isSavedMeal(entry))
        let saved = await store.saveMealAsPreset(entry)
        XCTAssertTrue(repeated); XCTAssertTrue(saved)
        XCTAssertEqual(calls.map { $0.1 }, ["/api/meal/c1000000-0000-4000-8000-000000000001/repeat", "/api/meal/c1000000-0000-4000-8000-000000000001/save-preset"])
        for call in calls { XCTAssertTrue(call.2.isEmpty, "no numbers or fields sent: \(call.2)") }
        XCTAssertTrue(store.isSavedMeal(entry))
    }

    func testEditAndRemoveSavedMealSendOnlyNamesAndChanges() async throws {
        var bodies = [(String, [String: Any])]()
        FixtureProtocol.handler = { request in
            let path = request.url!.path
            bodies.append((path, try self.body(request)))
            let name = path.hasSuffix("/edit") ? "Morning shake" : "Bowl"
            return (200, Data(#"{"presets":[{"name":"\#(name)","emoji":"🧋","calories":150,"protein":30,"carbs":3,"fat":2.5,"fiber":0,"meal":"Breakfast","sort_order":1}]}"#.utf8))
        }
        let store = AppStore(api: client(), vault: MemoryVault())
        let edited = await store.editPreset(EditPresetBody(name: "Shake", newName: "Morning shake", emoji: "🧋", meal: nil))
        XCTAssertTrue(edited); XCTAssertEqual(store.presets.map(\.name), ["Morning shake"])
        let shake = Preset(name: "Morning shake", emoji: "🧋", calories: 150, protein: 30, carbs: 3, fat: 2.5, fiber: 0, meal: "Breakfast", sortOrder: 1, active: true)
        let removed = await store.removePreset(shake)
        XCTAssertTrue(removed); XCTAssertEqual(store.presets.map(\.name), ["Bowl"])
        XCTAssertEqual(bodies.map(\.0), ["/api/presets/edit", "/api/presets/remove"])
        XCTAssertEqual(Set(bodies[0].1.keys), ["name", "new_name", "emoji"])
        XCTAssertEqual(bodies[0].1["new_name"] as? String, "Morning shake")
        XCTAssertEqual(bodies[1].1 as NSDictionary, ["name": "Morning shake"] as NSDictionary)
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
