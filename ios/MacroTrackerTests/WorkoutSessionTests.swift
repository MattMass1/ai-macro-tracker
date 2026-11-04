import XCTest
import SwiftUI
@testable import MacroTracker

final class WorkoutSessionDraftTests: XCTestCase {
    private func legs() -> WorkoutSessionDraft {
        WorkoutSessionDraft(type: "Legs", exerciseNames: ["Barbell Squat", "Leg Press", "Leg Curl"])
    }

    func testStartsWithThreeEmptySetsPerExercise() {
        let draft = legs()
        XCTAssertEqual(draft.exercises.map(\.name), ["Barbell Squat", "Leg Press", "Leg Curl"])
        XCTAssertEqual(draft.plannedSetCount, 9)
        XCTAssertEqual(draft.completedSetCount, 0)
        XCTAssertTrue(draft.loggable.isEmpty, "Nothing is logged until sets are done")
        XCTAssertNil(draft.pickUpHint)
    }

    func testSetCannotBeMarkedDoneWithoutLoadOrReps() {
        var draft = legs()
        let set = draft.exercises[0].sets[0].id
        XCTAssertFalse(draft.toggle(set: set, in: "Barbell Squat"))
        XCTAssertEqual(draft.completedSetCount, 0)
        draft.exercises[0].sets[0].reps = 8
        XCTAssertTrue(draft.toggle(set: set, in: "Barbell Squat"))
        XCTAssertEqual(draft.completedSetCount, 1)
        XCTAssertTrue(draft.toggle(set: set, in: "Barbell Squat"), "toggling back off always succeeds")
        XCTAssertEqual(draft.completedSetCount, 0)
    }

    func testLastSessionPrefillsUntouchedRowsOnly() {
        var draft = legs()
        draft.exercises[0].sets[1].weight = 185
        draft.applyLast(exercise: "Barbell Squat", sets: [.init(weight: 175, reps: 8), .init(weight: 175, reps: 8), .init(weight: 170, reps: 8), .init(weight: 165, reps: 8)], date: "2026-11-01")
        let sets = draft.exercises[0].sets
        XCTAssertEqual(sets.count, 4, "grows to match last time, up to the cap")
        XCTAssertEqual(sets[0].weight, 175); XCTAssertEqual(sets[0].reps, 8)
        XCTAssertEqual(sets[1].weight, 185); XCTAssertNil(sets[1].reps, "a row the user typed in is left alone")
        XCTAssertEqual(sets[3].weight, 165)
        XCTAssertEqual(draft.pickUpHint?.exercise, "Barbell Squat")
        XCTAssertEqual(draft.pickUpHint?.set, WorkoutSet(weight: 175, reps: 8))
        XCTAssertEqual(draft.exercises[0].lastDate, "2026-11-01")
    }

    func testLoggableOnlyIncludesDoneSetsAndSkipsSavedExercises() {
        var draft = legs()
        draft.applyLast(exercise: "Leg Press", sets: [.init(weight: 270, reps: 10)], date: nil)
        let press = draft.exercises[1].sets[0].id
        XCTAssertTrue(draft.toggle(set: press, in: "Leg Press"))
        draft.exercises[0].sets[0].weight = 175
        draft.exercises[0].sets[0].reps = 8
        XCTAssertEqual(draft.loggable.map(\.exercise), ["Leg Press"], "typed but not done sets are not logged")
        XCTAssertEqual(draft.loggable.first?.sets, [WorkoutSet(weight: 270, reps: 10)])
        draft.markLogged("Leg Press")
        XCTAssertTrue(draft.loggable.isEmpty, "a retry after a partial failure never logs an exercise twice")
    }

    func testAddRemoveAndSwapSets() {
        var draft = legs()
        draft.exercises[2].sets[2].weight = 80
        draft.addSet(to: "Leg Curl")
        XCTAssertEqual(draft.exercises[2].sets.count, 4)
        XCTAssertEqual(draft.exercises[2].sets[3].weight, 80, "a new set copies the previous row")
        for _ in 0..<10 { draft.addSet(to: "Leg Curl") }
        XCTAssertEqual(draft.exercises[2].sets.count, WorkoutSessionDraft.maxSetCount)
        let last = draft.exercises[2].sets.last!.id
        draft.removeSet(last, from: "Leg Curl")
        XCTAssertEqual(draft.exercises[2].sets.count, WorkoutSessionDraft.maxSetCount - 1)
        draft.swap(exercise: "Leg Curl", with: "Romanian Deadlift")
        XCTAssertEqual(draft.exercises[2].name, "Romanian Deadlift")
        XCTAssertEqual(draft.exercises[2].sets.count, WorkoutSessionDraft.defaultSetCount, "a swapped exercise starts over")
        draft.swap(exercise: "Romanian Deadlift", with: "Barbell Squat")
        XCTAssertEqual(draft.exercises[2].name, "Romanian Deadlift", "cannot duplicate an exercise already in the session")
    }

    func testRestTimerIsAnchoredToAWallClockEnd() {
        let start = Date(timeIntervalSince1970: 1_000)
        let timer = RestTimer.start(seconds: 90, now: start)
        XCTAssertEqual(timer.remaining(at: start), 90)
        XCTAssertEqual(timer.remaining(at: start.addingTimeInterval(30.2)), 60)
        XCTAssertEqual(timer.remaining(at: start.addingTimeInterval(500)), 0)
        XCTAssertTrue(timer.isFinished(at: start.addingTimeInterval(90)))
        XCTAssertEqual(RestTimer.label(seconds: 90), "1:30")
        XCTAssertEqual(RestTimer.label(seconds: 5), "0:05")
    }
}

/// Renders the new screens and saves PNGs so the layout can be looked at
/// without a signed-in device. Pure smoke: it only asserts the views render.
@MainActor
final class PixelScreensRenderTests: XCTestCase {
    private func render<V: View>(_ view: V, name: String) throws {
        let host = UIHostingController(rootView: view)
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 393, height: 852)
        window.rootViewController = host
        window.makeKeyAndVisible()
        defer { window.isHidden = true }
        host.view.setNeedsLayout(); host.view.layoutIfNeeded()
        let expectation = expectation(description: "settle")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { expectation.fulfill() }
        wait(for: [expectation], timeout: 5)
        let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let data = image.pngData() {
            try? data.write(to: URL(fileURLWithPath: "/tmp/mmacros-\(name).png"))
        }
        XCTAssertGreaterThan(image.size.width, 0)
    }

    private func sampleStore() -> AppStore {
        let store = AppStore()
        store.isLoadingDay = false
        store.isLoadingWorkouts = false
        store.plan = WorkoutPlanPayload(rotation: ["Push", "Pull", "Legs"], upcoming: [
            .init(type: "Legs", exercises: [.init(name: "Barbell Squat"), .init(name: "Leg Press"), .init(name: "Leg Curl")]),
        ], core: [], hasPlan: true)
        store.day = DayPayload(date: store.dateString, dayLabel: "Today",
                               totals: MacroTotals(calories: 1200, protein: 90, carbs: 110, fat: 40, fiber: 18),
                               targets: MacroTotals(calories: 2100, protein: 175, carbs: 210, fat: 70, fiber: 30),
                               remaining: MacroTotals(calories: 900, protein: 85, carbs: 100, fat: 30, fiber: 12),
                               meals: [FoodEntry(id: "m1", name: "Greek yogurt and berries", meal: "breakfast", calories: 320, protein: 24, carbs: 40, fat: 6, fiber: 4)])
        return store
    }

    func testHomeRenders() throws {
        let store = sampleStore()
        try render(NavigationStack { HomeView(openCoach: { _ in }) }.environmentObject(store), name: "home")
    }

    func testCoachTabRendersWithCompanionHeader() throws {
        let store = sampleStore()
        try render(NavigationStack { CoachChatView() }.environmentObject(store).environmentObject(AuthService()), name: "coach")
    }

    func testWorkoutSessionReviewRenders() throws {
        let store = sampleStore()
        let session = try XCTUnwrap(store.plan?.upcoming.first)
        try render(WorkoutSessionView(session: session, date: store.dateString, onAskCoach: { _ in }).environmentObject(store), name: "session-review")
    }
}
