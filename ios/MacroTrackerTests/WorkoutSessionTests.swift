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

    func testPlannedManualAndRestRoutesCaptureTheirDate() {
        let date = "2026-11-14"
        let planned = WorkoutSessionSelection.planned(.init(type: "Upper A", exercises: [.init(name: "Bench Press")]), date: date)
        let manual = WorkoutSessionSelection.manual(date: date, exercise: .init(type: "Upper A", exercise: "Bench Press"))
        let rest = WorkoutSessionSelection.manual(date: date, exercise: .init(type: "Rest", exercise: "Rest Day"))
        XCTAssertEqual(planned.date, date)
        XCTAssertEqual(manual.date, date)
        XCTAssertEqual(rest.date, date)
        XCTAssertEqual(planned.session.type, manual.session.type)
        XCTAssertFalse(planned.allowsTypeEditing)
        XCTAssertTrue(manual.allowsTypeEditing)
        XCTAssertEqual(rest.session.type, "Rest")
        XCTAssertTrue(rest.session.exercises.isEmpty)
    }

    func testInvalidTrainingTypeCannotActivateViaAnyStartRoute() {
        var draft = WorkoutSessionDraft(type: "Upper @", exerciseNames: ["Bench Press"])
        XCTAssertFalse(draft.canStart, "Both exercise-row taps and the footer use this activation guard")
        draft.type = "Upper A"
        XCTAssertTrue(draft.canStart)
        draft.type = ""
        XCTAssertFalse(draft.canStart)
        draft.type = "Upper A"
        draft.exercises = []
        XCTAssertFalse(draft.canStart)
    }

    func testManualExercisesAreTrimmedAndNotDuplicated() {
        var draft = WorkoutSessionDraft(type: "Upper A", exerciseNames: [])
        XCTAssertFalse(draft.addExercise("  "))
        XCTAssertTrue(draft.addExercise("  Bench Press  "))
        XCTAssertFalse(draft.addExercise("bench press"))
        XCTAssertEqual(draft.exercises.map(\.name), ["Bench Press"])
        XCTAssertTrue(draft.loggable.isEmpty)
    }

    func testCannotSwapAnExerciseAlreadySavedDuringPartialFinish() {
        var draft = legs()
        draft.markLogged("Barbell Squat")
        draft.swap(exercise: "Barbell Squat", with: "Goblet Squat")
        XCTAssertEqual(draft.exercises[0].name, "Barbell Squat")
        XCTAssertTrue(draft.exercises[0].logged, "A saved exercise must never become an unsaved replacement on retry")
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
    private func render<V: View>(_ view: V, name: String, size: CGSize = CGSize(width: 390, height: 844), keyboard: Bool = false) throws {
        var frames: [String: CGRect] = [:]
        let host = UIHostingController(rootView: view.tint(Theme.accent).preferredColorScheme(.light)
            .onPreferenceChange(StudioLayoutFrames.self) { frames = $0 })
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(origin: .zero, size: size)
        window.rootViewController = host
        window.makeKeyAndVisible()
        defer { window.endEditing(true); window.isHidden = true; window.rootViewController = nil }
        host.view.setNeedsLayout(); host.view.layoutIfNeeded()
        let expectation = expectation(description: "settle")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { expectation.fulfill() }
        wait(for: [expectation], timeout: 5)
        if keyboard {
            let input = try XCTUnwrap(findInput(in: host.view))
            let shown = self.expectation(forNotification: UIResponder.keyboardDidShowNotification, object: nil)
            XCTAssertTrue(input.becomeFirstResponder())
            wait(for: [shown], timeout: 5)
            host.view.layoutIfNeeded()
            let keyboardTop = host.view.keyboardLayoutGuide.layoutFrame.minY
            let inputRect = input.convert(input.bounds, to: host.view)
            XCTAssertTrue(input.isFirstResponder)
            XCTAssertLessThan(keyboardTop, host.view.bounds.maxY - 100, "Software keyboard must actually be presented")
            XCTAssertLessThanOrEqual(inputRect.maxY, keyboardTop + 1, "Composer must remain above the keyboard")

        }
        if name.hasPrefix("root-") {
            let frameAttachment = XCTAttachment(string: String(describing: frames))
            frameAttachment.name = "\(name)-layout"
            frameAttachment.lifetime = .keepAlways
            add(frameAttachment)
            if !keyboard {
                let tabs = try XCTUnwrap(frames["tabs"])
                for tag in [0, 2, 3] {
                    let tab = try XCTUnwrap(frames["tab.\(tag)"])
                    XCTAssertGreaterThanOrEqual(tab.height, 44)
                }
                let row = try XCTUnwrap(frames["composer.row"])
                XCTAssertGreaterThanOrEqual(row.height, 44)
                XCTAssertLessThanOrEqual(row.maxY, tabs.minY, "The whole composer row must clear the tabs, not just its text field")
            } else {
                XCTAssertNil(frames["tabs"], "Tab chrome is hidden while composing")
            }
            if !keyboard {
                let input = try XCTUnwrap(findInput(in: host.view))
                let inputRect = input.convert(input.bounds, to: window)
                XCTAssertGreaterThanOrEqual(inputRect.height, 16, "The composer may not be collapsed by tab chrome")
                XCTAssertLessThanOrEqual(inputRect.maxY, window.bounds.maxY - 80, "The composer must finish above the tab row and home-indicator area")
            }
        }
        let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        XCTAssertGreaterThan(image.size.width, 0)
    }

    private func findInput(in view: UIView) -> UIView? {
        if view is UITextView || view is UITextField { return view }
        return view.subviews.lazy.compactMap { self.findInput(in: $0) }.first
    }

    func testActualKeyboardKeepsMultilineComposerReachable() throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StudioRenderProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let api = APIClient(baseURL: URL(string: "https://studio-chat-render.invalid"), tokenProvider: { "synthetic-keyboard-only" }, session: session)
        let store = AppStore(api: api)
        store.chat.input = "Chicken bowl\nHalf the rice\nExtra vegetables"
        try render(ContentView().environmentObject(store).environmentObject(AuthService(api: api)), name: "root-studio-keyboard", keyboard: true)
        XCTAssertFalse(store.isLoadingDay)
        XCTAssertFalse(store.isLoadingWorkouts)
        XCTAssertNil(store.errorMessage)
    }

    func testActualRootHasThreeTabsAndStudioHome() throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StudioRenderProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let api = APIClient(baseURL: URL(string: "https://studio-render.invalid"), tokenProvider: { "synthetic-render-only" }, session: session)
        let store = AppStore(api: api)
        try render(ContentView().environmentObject(store).environmentObject(AuthService(api: api)), name: "root-studio")
        XCTAssertFalse(store.isLoadingDay)
        XCTAssertFalse(store.isLoadingWorkouts)
        XCTAssertNil(store.errorMessage)
    }

    func testPopulatedConversationRemainsInTheStudio() throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StudioRenderProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let api = APIClient(baseURL: URL(string: "https://studio-chat-render.invalid"), tokenProvider: { "synthetic-render-only" }, session: session)
        let store = AppStore(api: api)
        try render(ContentView().environmentObject(store).environmentObject(AuthService(api: api)), name: "root-studio-chat")
        XCTAssertFalse(store.isLoadingDay)
        XCTAssertFalse(store.isLoadingWorkouts)
        XCTAssertNil(store.errorMessage)
    }

    private func sampleStore() -> AppStore {
        let store = AppStore(api: APIClient(baseURL: nil, tokenProvider: { nil }))
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

    func testStudioHomeRendersWithMaleCompanion() throws {
        try renderStudio(character: .male, name: "studio-male")
    }

    func testStudioHomeRendersWithFemaleCompanion() throws {
        try renderStudio(character: .female, name: "studio-female")
    }

    func testCompactStudioKeepsArtAndComposer() throws {
        try renderStudio(character: .female, name: "studio-compact", size: CGSize(width: 320, height: 568))
    }

    func testStudioSupportsLargeText() throws {
        let store = sampleStore()
        try render(NavigationStack { HomeView() }.environmentObject(store).environmentObject(AuthService())
            .environment(\.dynamicTypeSize, .accessibility1), name: "studio-large-text")
    }

    func testPixelFontIsRegisteredInTheBuiltApp() throws {
        let font = try XCTUnwrap(UIFont(name: "Jersey15-Regular", size: 24))
        XCTAssertEqual(font.familyName, "Jersey 15")
        XCTAssertNotNil(Bundle.main.url(forResource: "Jersey15-OFL", withExtension: "txt"))
    }

    private func renderStudio(character: CompanionCharacter, name: String, size: CGSize = CGSize(width: 390, height: 844)) throws {
        let defaults = UserDefaults.standard
        let oldCharacter = defaults.object(forKey: CompanionPreferences.characterKey)
        let oldVisible = defaults.object(forKey: CompanionPreferences.visibleKey)
        defaults.set(character.rawValue, forKey: CompanionPreferences.characterKey)
        defaults.set(true, forKey: CompanionPreferences.visibleKey)
        defer {
            defaults.set(oldCharacter, forKey: CompanionPreferences.characterKey)
            defaults.set(oldVisible, forKey: CompanionPreferences.visibleKey)
        }
        let store = sampleStore()
        try render(NavigationStack { HomeView() }.environmentObject(store).environmentObject(AuthService()), name: name, size: size)
    }

    func testSingleWorkoutsOverviewRenders() throws {
        let store = sampleStore()
        try render(NavigationStack { WorkoutsView(openCoach: { _ in }) }.environmentObject(store), name: "workouts")
    }

    func testManualWorkoutUsesTheSharedEditor() throws {
        let store = sampleStore()
        try render(WorkoutSessionView(selection: .manual(date: store.dateString), onAskCoach: { _ in }).environmentObject(store), name: "manual-workout")
    }

    func testRestDayUsesTheSharedEditor() throws {
        let store = sampleStore()
        let selection = WorkoutSessionSelection.manual(date: store.dateString, exercise: .init(type: "Rest", exercise: "Rest Day"))
        try render(WorkoutSessionView(selection: selection, onAskCoach: { _ in }).environmentObject(store), name: "rest-day")
    }

    func testSharedStudioConversationRenders() throws {
        let store = sampleStore()
        try render(NavigationStack { CoachChatView() }.environmentObject(store).environmentObject(AuthService()), name: "coach")
    }

    func testWorkoutSessionReviewRenders() throws {
        let store = sampleStore()
        let session = try XCTUnwrap(store.plan?.upcoming.first)
        try render(WorkoutSessionView(session: session, date: store.dateString, onAskCoach: { _ in }).environmentObject(store), name: "session-review")
    }
}

final class StudioLayoutTests: XCTestCase {
    func testReferencePhoneUses144PointCompanionNotOversizedStage() {
        let layout = StudioLayout(width: 390, height: 650)
        XCTAssertEqual(layout.companionSize, 144)
        XCTAssertEqual(layout.floorHeight, 176)
        XCTAssertEqual(StudioLayout.titleSize, 28)
        XCTAssertEqual(StudioLayout.panelTitleSize, 16)
    }

    func testCompactAndKeyboardLayoutsReserveRoomForConversation() {
        for size in [CGSize(width: 320, height: 350), CGSize(width: 390, height: 240)] {
            let layout = StudioLayout(width: size.width, height: size.height)
            XCTAssertGreaterThan(layout.companionSize, 0)
            XCTAssertLessThanOrEqual(layout.companionSize, 144)
            XCTAssertLessThan(layout.floorHeight, size.height * 0.35)
        }
    }
}
