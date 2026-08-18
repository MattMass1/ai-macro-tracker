import XCTest
@testable import MacroTracker

final class ModelDecodingTests: XCTestCase {
    func testDayPayloadDecodesSnakeCase() throws {
        let json = #"{"date":"2026-08-18","day_label":"Today","totals":{"calories":1200,"protein":91,"carbs":110,"fat":42,"fiber":18},"targets":{"calories":2100,"protein":175,"carbs":210,"fat":70,"fiber":30},"remaining":{"calories":900,"protein":84,"carbs":100,"fat":28,"fiber":12},"meals":[]}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(DayPayload.self, from: json)
        XCTAssertEqual(payload.dayLabel, "Today")
        XCTAssertEqual(payload.remaining.protein, 84)
    }

    func testWorkoutSetsDecodeAsProductionShape() throws {
        let json = #"{"date":"2026-08-18","day_label":"Today","workouts":[{"id":"abc","exercise":"Bench Press","workout_type":["Push"],"muscle_group":["Chest"],"sets":[{"weight":185,"reps":6}]}]}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(WorkoutsPayload.self, from: json)
        XCTAssertEqual(payload.workouts.first?.sets.first, WorkoutSet(weight: 185, reps: 6))
    }
}

