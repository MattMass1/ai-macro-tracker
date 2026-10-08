import XCTest
import SwiftUI
@testable import MacroTracker

@MainActor
final class WorkoutPlanRefreshTests: XCTestCase {
    func testMountedSameTypePlanRefreshReplacesRowsThenShowsEmptyPlan() async throws {
        let store = AppStore()
        store.isLoadingWorkouts = false
        let date = store.dateString
        let key = WorkoutSessionCompletions.key(date: date, type: "Pull")
        let saved = UserDefaults.standard.object(forKey: key)
        defer { UserDefaults.standard.set(saved, forKey: key) }
        WorkoutSessionCompletions.save(["Lat Pulldown"], date: date, type: "Pull")
        func plan(_ names: [String]) -> WorkoutPlanPayload {
            .init(rotation: [], upcoming: [.init(type: "Pull", exercises: names.map { .init(name: $0) })],
                  core: [], hasPlan: true)
        }
        store.plan = plan(["Lat Pulldown", "Barbell Row", "Face Pull"])
        let initial = try XCTUnwrap(store.plan?.upcoming.first)
        let state = WorkoutSessionCardState(session: initial, date: date)
        let host = UIHostingController(rootView: RefreshHarness(store: store, state: state))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 393, height: 852)
        window.rootViewController = host
        window.makeKeyAndVisible()
        defer { window.isHidden = true }
        await settle(host)
        XCTAssertEqual(state.exercises, ["Lat Pulldown", "Barbell Row", "Face Pull"])
        // This is the same @Published property replaced by loadWorkoutData after Confirm.
        store.plan = plan(["Lat Pulldown", "Leg Press"])
        await settle(host)
        XCTAssertEqual(state.exercises, ["Lat Pulldown", "Leg Press"], "Mounted card must adopt the saved same-type swap and shorter list")
        XCTAssertEqual(state.completed, ["Lat Pulldown"])
        XCTAssertEqual(WorkoutSessionCompletions.saved(date: date, type: "Pull"), ["Lat Pulldown"])
        // An unchanged payload must preserve a local SWAP choice.
        state.exercises[1] = "Face Pull"
        store.plan = plan(["Lat Pulldown", "Leg Press"])
        await settle(host)
        XCTAssertEqual(state.exercises, ["Lat Pulldown", "Face Pull"])
        store.plan = plan([])
        await settle(host)
        XCTAssertTrue(state.exercises.isEmpty, "Empty confirmed plan must render the existing empty state and Add more")
        XCTAssertTrue(store.workouts.isEmpty, "Plan refresh is not completed training")
        let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = "Confirmed empty today plan"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func settle(_ host: UIViewController) async {
        host.view.setNeedsLayout()
        host.view.layoutIfNeeded()
        try? await Task.sleep(for: .milliseconds(200))
    }

    private struct RefreshHarness: View {
        @ObservedObject var store: AppStore
        let state: WorkoutSessionCardState
        var body: some View {
            if let session = store.plan?.upcoming.first {
                TodaysSessionCard(session: session, date: store.dateString,
                                  onLog: { _ in }, onAddMore: {}, state: state)
                    .id(session.type)
            }
        }
    }
}

final class ModelDecodingTests: XCTestCase {
    func testCalorieOnlyDayUsesCanonicalServerFixtureWithoutInventedMacros() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "calorie-only-day", withExtension: "json"))
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let day = try decoder.decode(DayPayload.self, from: Data(contentsOf: url))
        XCTAssertEqual(day.totals.calories, 200)
        let meal = try XCTUnwrap(day.meals.first)
        XCTAssertNil(meal.protein)
        XCTAssertNil(meal.carbs)
        XCTAssertNil(meal.fat)
        XCTAssertNil(meal.fiber)
        XCTAssertFalse(meal.nutrientsComplete)
        XCTAssertFalse(day.nutrientsComplete)
        XCTAssertEqual(day.macrosComplete, false)
    }

    func testDayPayloadDecodesSnakeCase() throws {
        let json = #"{"date":"2026-08-18","day_label":"Today","totals":{"calories":1200,"protein":91,"carbs":110,"fat":42,"fiber":18},"targets":{"calories":2100,"protein":175,"carbs":210,"fat":70,"fiber":30},"remaining":{"calories":900,"protein":84,"carbs":100,"fat":28,"fiber":12},"meals":[],"day_rollup":{"date":"2026-08-18","calories":1200,"protein":91,"carbs":110,"fat":42,"fiber":18}}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(DayPayload.self, from: json)
        XCTAssertEqual(payload.dayLabel, "Today")
        XCTAssertEqual(payload.remaining.protein, 84)
        XCTAssertEqual(payload.dayRollup?.date, "2026-08-18")
    }

    func testWorkoutSetsDecodeAsProductionShape() throws {
        let json = #"{"date":"2026-08-18","day_label":"Today","workouts":[{"id":"abc","exercise":"Bench Press","workout_type":["Push"],"muscle_group":["Chest"],"sets":[{"weight":185,"reps":6}]}]}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(WorkoutsPayload.self, from: json)
        XCTAssertEqual(payload.workouts.first?.sets.first, WorkoutSet(weight: 185, reps: 6))
    }

    func testBarcodeFoodPayloadDecodesSnakeCase() throws {
        let json = #"{"name":"Greek Yogurt","calories":97,"protein":9,"carbs":4,"fat":5,"fiber":0,"source":"Open Food Facts","serving_size":"150 g"}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(BarcodeFoodPayload.self, from: json)
        XCTAssertEqual(payload.name, "Greek Yogurt")
        XCTAssertEqual(payload.calories, 97)
        XCTAssertEqual(payload.protein, 9)
        XCTAssertEqual(payload.source, "Open Food Facts")
        XCTAssertEqual(payload.servingSize, "150 g")
        XCTAssertNil(payload.macrosPerServing)
    }

    func testBarcodeFoodPayloadDecodesWithoutServingSize() throws {
        let json = #"{"name":"Cola","calories":42,"protein":0,"carbs":10.6,"fat":0,"fiber":0,"source":"Open Food Facts"}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(BarcodeFoodPayload.self, from: json)
        XCTAssertEqual(payload.name, "Cola")
        XCTAssertNil(payload.servingSize)
        XCTAssertNil(payload.macrosPerServing)
        XCTAssertEqual(payload.carbs, 10.6)
    }

    func testBarcodeFoodPayloadDecodesMacrosPerServing() throws {
        let json = #"{"name":"Protein Bar","calories":364,"protein":36,"carbs":27,"fat":9,"fiber":4,"source":"Open Food Facts","serving_size":"1 bar (55 g)","macros_per_serving":{"calories":200,"protein":20,"carbs":15,"fat":5,"fiber":2}}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(BarcodeFoodPayload.self, from: json)
        XCTAssertEqual(payload.servingSize, "1 bar (55 g)")
        XCTAssertEqual(payload.calories, 364)
        XCTAssertEqual(payload.macrosPerServing, MacroTotals(calories: 200, protein: 20, carbs: 15, fat: 5, fiber: 2))
    }

    func testBarcodeFoodRequestEncodesCode() throws {
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        let json = try JSONSerialization.jsonObject(with: encoder.encode(BarcodeFoodRequest(code: "012345678905"))) as! [String: Any]
        XCTAssertEqual(json["code"] as? String, "012345678905")
    }

    func testWorkoutPlanWriteGroupsByTypeAndEncodesSnakeCase() throws {
        let plan = try XCTUnwrap(WorkoutPlanWrite.fromPickedExercises([
            LibraryExercise(
                name: "Bench Press",
                muscleGroup: ["Chest"],
                workoutType: "Push",
                equipment: "Barbell",
                difficulty: "Intermediate",
                swaps: ["Dumbbell Press"]
            ),
            LibraryExercise(
                name: "Back Squat",
                muscleGroup: ["Quads"],
                workoutType: "Legs",
                equipment: "Barbell",
                difficulty: "Intermediate",
                swaps: []
            ),
        ]))
        XCTAssertEqual(plan.version, 1)
        XCTAssertEqual(plan.rotation, ["Push", "Legs"])
        XCTAssertEqual(plan.daysPerWeek, 2)
        XCTAssertEqual(plan.days["Push"]?.exercises.first?.name, "Bench Press")
        XCTAssertEqual(plan.days["Legs"]?.exercises.first?.name, "Back Squat")
        XCTAssertEqual(plan.days["Push"]?.exercises.first?.sets, 3)
        XCTAssertEqual(plan.days["Push"]?.exercises.first?.reps, "8-12")
        XCTAssertEqual(plan.days["Push"]?.exercises.first?.restSec, 90)
        XCTAssertEqual(plan.days["Push"]?.exercises.first?.swaps, ["Dumbbell Press"])
        XCTAssertNil(plan.days["Legs"]?.exercises.first?.swaps)

        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        let json = try JSONSerialization.jsonObject(with: encoder.encode(plan)) as! [String: Any]
        XCTAssertEqual(json["version"] as? Int, 1)
        XCTAssertEqual(json["days_per_week"] as? Int, 2)
        XCTAssertEqual(json["rotation"] as? [String], ["Push", "Legs"])
        let push = try XCTUnwrap((json["days"] as? [String: Any])?["Push"] as? [String: Any])
        let exercise = try XCTUnwrap((push["exercises"] as? [[String: Any]])?.first)
        XCTAssertEqual(exercise["name"] as? String, "Bench Press")
        XCTAssertEqual(exercise["rest_sec"] as? Int, 90)
        XCTAssertEqual(exercise["sets"] as? Int, 3)
        XCTAssertEqual(exercise["reps"] as? String, "8-12")
    }

    func testWorkoutPlanWriteReturnsNilForEmptySelection() {
        XCTAssertNil(WorkoutPlanWrite.fromPickedExercises([]))
    }

    func testSavePlanPayloadDecodesWarnings() throws {
        let json = #"{"plan":{"version":1},"warnings":["Exercise is not in workout_library: Foo"]}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(SavePlanPayload.self, from: json)
        XCTAssertEqual(payload.warnings, ["Exercise is not in workout_library: Foo"])
    }
}
