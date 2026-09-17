import XCTest
@testable import MacroTracker

final class AgentCanvasContractTests: XCTestCase {
    private func fixture() throws -> Data {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "snapshot", withExtension: "json"))
        return try Data(contentsOf: url)
    }

    func testNativeSetupRowsAndClosedInputs() throws {
        var value = try XCTUnwrap(JSONSerialization.jsonObject(with: fixture()) as? [String: Any])
        value["approval"] = NSNull()
        value["surfaces"] = [["surfaceId": "task", "lifecycle": "task", "components": [
            ["id": "setup", "component": "SetupChecklist", "rows": [["label": "Name", "detail": "Needed"]]],
            ["id": "metrics", "component": "ProfileMetrics"],
            ["id": "targets", "component": "TargetStatus"],
            ["id": "plan", "component": "WorkoutPlanPreview", "rows": [["label": "Sample", "detail": "Not saved"]]]
        ]]]
        let data = try JSONSerialization.data(withJSONObject: value)
        let decoded = try AgentCanvasEnvelope.decode(data)
        XCTAssertEqual(decoded.surfaces[0].components.map(\.component), [.setupChecklist, .profileMetrics, .targetStatus, .workoutPlanPreview])
        XCTAssertEqual(decoded.surfaces[0].components[0].rows[0].detail, "Needed")
        let intent = AgentIntent(action: .submitMetrics, numbers: ["height_cm": 180, "weight_kg": 80, "goal_weight_kg": 75])
        try AgentActionGateway.validate(intent, envelope: decoded, authenticated: true)
        XCTAssertThrowsError(try AgentActionGateway.validate(intent, envelope: decoded, authenticated: false))
        XCTAssertThrowsError(try AgentIntent(action: .submitMetrics, numbers: ["user_id": 123]).validateShape())
        XCTAssertThrowsError(try AgentIntent(action: .refresh, numbers: ["calories": 2000]).validateShape())
        let roundtrip = try JSONDecoder().decode(AgentIntent.self, from: JSONEncoder().encode(intent))
        XCTAssertEqual(roundtrip, intent)
        value["surfaces"] = [["surfaceId": "task", "lifecycle": "task", "components": [["id": "setup", "component": "OnboardingScript"]]]]
        XCTAssertThrowsError(try AgentCanvasEnvelope.decode(JSONSerialization.data(withJSONObject: value)))
    }

    func testAuthoritativeRecreationClearsApprovalAndRejectsOldInstance() throws {
        let old = try AgentCanvasEnvelope.decode(fixture())
        var state = AgentSurfaceState(sessionId: old.sessionId)
        _ = try state.apply(old)
        var fresh = old
        fresh.instanceId = UUID().uuidString.lowercased()
        fresh.revision = 0
        fresh.surfaces = []; fresh.approval = nil; fresh.receipt = nil; fresh.workout = nil
        XCTAssertThrowsError(try state.apply(fresh), "Unsolicited events cannot reset a session")
        XCTAssertTrue(try state.apply(fresh, authoritativeRefresh: true))
        XCTAssertNil(state.envelope?.approval)
        XCTAssertTrue(state.transientDeadlines.isEmpty)
        XCTAssertEqual(state.restRemaining(), 0)
        XCTAssertThrowsError(try state.apply(old), "Late old approvals must never reappear")
        XCTAssertThrowsError(try AgentActionGateway.validate(.init(action: .confirm, reference: old.approval?.id),
            envelope: state.envelope, authenticated: true))
        XCTAssertFalse(try state.apply(fresh))
    }

    func testCustomWorkoutLabelsShareServerFixture() throws {
        struct Labels: Decodable { let accepted: [String]; let rejected: [String] }
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "workout-labels", withExtension: "json"))
        let labels = try JSONDecoder().decode(Labels.self, from: Data(contentsOf: url))
        var snapshot = try AgentCanvasEnvelope.decode(fixture())
        for label in labels.accepted + labels.rejected {
            snapshot.workout = AgentWorkout(date: "2026-09-16", type: label, done: false,
                exercises: [], activeExerciseId: nil, loggedSets: [:], restEndsAt: nil)
            let data = try JSONEncoder().encode(snapshot)
            if labels.accepted.contains(label) {
                XCTAssertEqual(try AgentCanvasEnvelope.decode(data).workout?.type, label)
            } else {
                XCTAssertThrowsError(try AgentCanvasEnvelope.decode(data), label)
            }
        }
    }

    func testCanonicalServerFixtureDecodesClosedCatalog() throws {
        let snapshot = try AgentCanvasEnvelope.decode(fixture())
        XCTAssertEqual(snapshot.surfaces.first?.components.first?.component, .macroProgress)
        XCTAssertEqual(snapshot.approval?.status, .pending)
        XCTAssertEqual(snapshot.surfaces.count, 2)
    }

    func testUnknownComponentsActionsAndExtraExecutablePropertiesAreRejected() throws {
        let text = String(decoding: try fixture(), as: UTF8.self)
        for (old, new) in [("MacroProgress", "WebView"), ("\"confirm\", \"reference\"", "\"raw_post\", \"reference\""),
                           ("\"component\": \"MacroProgress\"", "\"component\": \"MacroProgress\", \"url\": \"https://fixture.invalid\"")] {
            XCTAssertThrowsError(try AgentCanvasEnvelope.decode(Data(text.replacingOccurrences(of: old, with: new).utf8)))
        }
    }

    func testReplacementDismissalExpiryAndStaleSessionProtection() throws {
        var snapshot = try AgentCanvasEnvelope.decode(fixture())
        var state = AgentSurfaceState(sessionId: snapshot.sessionId)
        XCTAssertTrue(try state.apply(snapshot))
        XCTAssertFalse(try state.apply(snapshot))
        snapshot.revision += 1
        snapshot.surfaces[0].components = [AgentComponent(id: "weekly", component: .weeklyTrend)]
        XCTAssertTrue(try state.apply(snapshot))
        XCTAssertEqual(state.envelope?.surfaces.count, 2)
        XCTAssertThrowsError(try state.dismiss("approval"))
        try state.dismiss("task")
        XCTAssertEqual(state.visibleSurfaces().map(\.surfaceId), ["approval"])
        snapshot.revision += 1
        snapshot.serverTime = 0
        snapshot.surfaces.append(AgentSurface(surfaceId: "receipt", lifecycle: .transient,
            components: [AgentComponent(id: "receipt", component: .mealReceipt)], expiresAt: 10))
        XCTAssertTrue(try state.apply(snapshot, at: 0))
        XCTAssertFalse(state.visibleSurfaces(at: 11).contains { $0.surfaceId == "receipt" })
        snapshot.sessionId = UUID().uuidString.lowercased()
        XCTAssertThrowsError(try state.apply(snapshot))
    }

    func testReceiptClockSkewAndRefreshDoNotHideOrExtendAcknowledgement() throws {
        for serverTime in [700.0, 1000.0, 1300.0] {
            // Device uptime is independent of either clock being five minutes off.
            var snapshot = try AgentCanvasEnvelope.decode(fixture())
            snapshot.serverTime = serverTime
            snapshot.surfaces.append(AgentSurface(surfaceId: "receipt", lifecycle: .transient,
                components: [AgentComponent(id: "receipt", component: .mealReceipt)], expiresAt: serverTime + 30))
            snapshot.workout = AgentWorkout(date: "2026-09-16", type: "Upper", done: false,
                exercises: [], activeExerciseId: nil, loggedSets: [:], restEndsAt: serverTime + 90)
            var state = AgentSurfaceState(sessionId: snapshot.sessionId)
            XCTAssertTrue(try state.apply(snapshot, at: 50))
            XCTAssertTrue(state.visibleSurfaces(at: 51).contains { $0.surfaceId == "receipt" })
            XCTAssertEqual(state.restRemaining(at: 51), 89)
            snapshot.revision += 1
            snapshot.serverTime += 20
            XCTAssertTrue(try state.apply(snapshot, at: 70))
            XCTAssertTrue(state.visibleSurfaces(at: 79).contains { $0.surfaceId == "receipt" })
            XCTAssertFalse(state.visibleSurfaces(at: 80).contains { $0.surfaceId == "receipt" })
            XCTAssertEqual(state.restRemaining(at: 80), 60)
            XCTAssertEqual(state.restRemaining(at: 140), 0)
            XCTAssertFalse(try state.apply(snapshot, at: 150))
        }
    }

    func testNewReceiptHasDeliveryMarginAndStillExpires() throws {
        var snapshot = try AgentCanvasEnvelope.decode(fixture())
        snapshot.serverTime = 100
        snapshot.surfaces.append(AgentSurface(surfaceId: "receipt", lifecycle: .transient,
            components: [AgentComponent(id: "receipt", component: .mealReceipt)], expiresAt: 100.5))
        var state = AgentSurfaceState(sessionId: snapshot.sessionId)
        _ = try state.apply(snapshot, at: 50)
        XCTAssertTrue(state.visibleSurfaces(at: 59).contains { $0.surfaceId == "receipt" })
        XCTAssertFalse(state.visibleSurfaces(at: 60).contains { $0.surfaceId == "receipt" })
        snapshot.serverTime = .infinity
        XCTAssertThrowsError(try snapshot.validate())
    }

    func testActionGatewayRequiresAuthenticationAndOwnedApprovalReference() throws {
        let snapshot = try AgentCanvasEnvelope.decode(fixture())
        let draft = try XCTUnwrap(snapshot.approval)
        XCTAssertThrowsError(try AgentActionGateway.validate(.init(action: .confirm, reference: draft.id), envelope: snapshot, authenticated: false))
        XCTAssertThrowsError(try AgentActionGateway.validate(.init(action: .confirm, reference: "forged"), envelope: snapshot, authenticated: true))
        XCTAssertNoThrow(try AgentActionGateway.validate(.init(action: .confirm, reference: draft.id), envelope: snapshot, authenticated: true))
        XCTAssertThrowsError(try AgentActionGateway.validate(.init(action: .openSetLogger, reference: "forged"), envelope: snapshot, authenticated: true))
    }

    func testUncertainTurnKeepsItsIdentityUntilAcknowledged() {
        var pending = AgentTurnIdentity()
        let original = pending.id(for: "Log my fixture meal")
        XCTAssertEqual(pending.id(for: "Log my fixture meal"), original)
        pending.acknowledge(original)
        XCTAssertNotEqual(pending.id(for: "Log my fixture meal"), original)
    }

    func testPinnedStatusCannotBeGeneratedOrDismissed() throws {
        let text = String(decoding: try fixture(), as: UTF8.self)
        XCTAssertThrowsError(try AgentCanvasEnvelope.decode(Data(text.replacingOccurrences(of: "MacroProgress", with: "DailyStatus").utf8)))
        XCTAssertThrowsError(try AgentCanvasEnvelope.decode(Data(text.replacingOccurrences(of: "\"lifecycle\": \"task\"", with: "\"lifecycle\": \"pinned\"").utf8)))
    }
}
