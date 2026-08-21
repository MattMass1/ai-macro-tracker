import XCTest
@testable import MacroTracker

final class ModelDecodingTests: XCTestCase {
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

    func testChatReplyDecodesMetricsWidget() throws {
        let json = #"{"reply":"Let's get your numbers.","has_plan":false,"has_targets":false,"widget":{"type":"metrics_form","fields":[{"key":"height_cm","label":"Height","unit":"cm","placeholder":"180"},{"key":"weight_kg","label":"Weight","unit":"kg"},{"key":"goal_weight_kg","label":"Goal weight","unit":"kg","placeholder":"75"}]}}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let reply = try decoder.decode(ChatReply.self, from: json)
        XCTAssertEqual(reply.reply, "Let's get your numbers.")
        XCTAssertEqual(reply.widget?.type, "metrics_form")
        XCTAssertEqual(reply.widget?.fields.count, 3)
        XCTAssertEqual(reply.widget?.fields.first?.key, "height_cm")
        XCTAssertEqual(reply.widget?.fields.first?.unit, "cm")
        XCTAssertEqual(reply.widget?.fields.last?.placeholder, "75")
    }

    func testChatReplyWithoutWidgetStillDecodes() throws {
        let json = #"{"reply":"Tell me what you ate.","has_plan":true}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let reply = try decoder.decode(ChatReply.self, from: json)
        XCTAssertEqual(reply.reply, "Tell me what you ate.")
        XCTAssertNil(reply.widget)
        XCTAssertEqual(reply.hasPlan, true)
    }

    func testChatRequestEncodesMetricsSnakeCase() throws {
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        let body = ChatRequest(
            message: "My metrics: Height 180 cm",
            date: nil,
            metrics: ChatMetrics(heightCm: 180, weightKg: 80, goalWeightKg: 75)
        )
        let json = try JSONSerialization.jsonObject(with: encoder.encode(body)) as! [String: Any]
        XCTAssertEqual(json["message"] as? String, "My metrics: Height 180 cm")
        XCTAssertNil(json["date"])
        let metrics = json["metrics"] as! [String: Any]
        XCTAssertEqual((metrics["height_cm"] as? NSNumber)?.doubleValue, 180)
        XCTAssertEqual((metrics["weight_kg"] as? NSNumber)?.doubleValue, 80)
        XCTAssertEqual((metrics["goal_weight_kg"] as? NSNumber)?.doubleValue, 75)
        XCTAssertNil(metrics["age"])
        XCTAssertNil(metrics["activity_level"])
    }

    func testChatRequestEncodesOptionalAgeAndActivityLevel() throws {
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        let body = ChatRequest(
            message: "My metrics: Height 180 cm, Age 32, Activity level moderate",
            date: nil,
            metrics: ChatMetrics(heightCm: 180, weightKg: 80, goalWeightKg: 75, age: 32, activityLevel: "moderate")
        )
        let metrics = try JSONSerialization.jsonObject(with: encoder.encode(body)) as! [String: Any]
        let payload = metrics["metrics"] as! [String: Any]
        XCTAssertEqual((payload["age"] as? NSNumber)?.intValue, 32)
        XCTAssertEqual(payload["activity_level"] as? String, "moderate")
    }

    func testChatReplyDecodesBareStringWidgetFields() throws {
        let json = #"{"reply":"Add your measurements here.","widget":{"type":"metrics_form","fields":["height_cm","weight_kg","goal_weight_kg","age","activity_level"]}}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let reply = try decoder.decode(ChatReply.self, from: json)
        XCTAssertEqual(reply.widget?.type, "metrics_form")
        XCTAssertEqual(reply.widget?.fields.map(\.key), ["height_cm", "weight_kg", "goal_weight_kg", "age", "activity_level"])
        XCTAssertEqual(reply.widget?.fields.map(\.kind), [.number, .number, .number, .number, .string])
        XCTAssertEqual(reply.widget?.fields.first?.label, "Height")
        XCTAssertEqual(reply.widget?.fields.first?.unit, "ft / in")
        XCTAssertEqual(reply.widget?.fields.last?.label, "Activity level")
        XCTAssertEqual(reply.widget?.fields.last?.isNumeric, Optional(false))
    }

    func testMetricsFieldInfersTypeWhenObjectOmitsType() throws {
        let json = #"{"key":"activity_level","label":"Activity level","placeholder":"moderate"}"#.data(using: .utf8)!
        let field = try JSONDecoder().decode(MetricsField.self, from: json)
        XCTAssertEqual(field.kind, .string)
        XCTAssertFalse(field.isNumeric)
        let age = try JSONDecoder().decode(MetricsField.self, from: #"{"key":"age","label":"Age"}"#.data(using: .utf8)!)
        XCTAssertEqual(age.kind, .number)
        XCTAssertTrue(age.isNumeric)
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
    }

    func testBarcodeFoodPayloadDecodesWithoutServingSize() throws {
        let json = #"{"name":"Cola","calories":42,"protein":0,"carbs":10.6,"fat":0,"fiber":0,"source":"Open Food Facts"}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(BarcodeFoodPayload.self, from: json)
        XCTAssertEqual(payload.name, "Cola")
        XCTAssertNil(payload.servingSize)
        XCTAssertEqual(payload.carbs, 10.6)
    }

    func testBarcodeFoodRequestEncodesCode() throws {
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        let json = try JSONSerialization.jsonObject(with: encoder.encode(BarcodeFoodRequest(code: "012345678905"))) as! [String: Any]
        XCTAssertEqual(json["code"] as? String, "012345678905")
    }

    func testMetricsFieldExplicitTypeWinsOverKeyInference() throws {
        let json = #"{"key":"height_cm","label":"Height","type":"string"}"#.data(using: .utf8)!
        let field = try JSONDecoder().decode(MetricsField.self, from: json)
        XCTAssertEqual(field.kind, .string)
        XCTAssertFalse(field.isNumeric)
    }

    func testUSHeightConvertsToBackendCentimeters() throws {
        let centimeters = try XCTUnwrap(
            MetricsFormCard.heightCentimeters(feet: "5", inches: "10")
        )
        XCTAssertEqual(centimeters, 177.8, accuracy: 0.0001)
        XCTAssertNil(MetricsFormCard.heightCentimeters(feet: "5", inches: "12"))
    }

    func testUSPoundsConvertToBackendKilograms() {
        XCTAssertEqual(
            MetricsFormCard.kilograms(fromPounds: 175),
            79.37866475,
            accuracy: 0.0000001
        )
    }

    func testChatReplyDecodesExerciseCardWidget() throws {
        let json = #"{"reply":"Two moves to watch.","widget":{"type":"exercise_card","exercise":{"name":"Bulgarian Split Squat","muscle_group":"Legs","equipment":"Dumbbell","sets":3,"reps":"10","video_url":"https://example.com/split.mp4","instructions":"Front heel down.","workout_type":"Legs"}}}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let reply = try decoder.decode(ChatReply.self, from: json)
        XCTAssertEqual(reply.widget?.type, "exercise_card")
        XCTAssertEqual(reply.widget?.exercise?.name, "Bulgarian Split Squat")
        XCTAssertEqual(reply.widget?.exercise?.muscleGroup, "Legs")
        XCTAssertEqual(reply.widget?.exercise?.equipment, "Dumbbell")
        XCTAssertEqual(reply.widget?.exercise?.sets, "3")
        XCTAssertEqual(reply.widget?.exercise?.reps, "10")
        XCTAssertEqual(reply.widget?.exercise?.videoUrl, "https://example.com/split.mp4")
        XCTAssertEqual(reply.widget?.exercise?.instructions, "Front heel down.")
        XCTAssertEqual(reply.widget?.exercise?.workoutType, "Legs")
        XCTAssertEqual(reply.widget?.fields.count, 0)
    }

    func testExerciseCardDecodesOptionalFieldsAndArrays() throws {
        let json = #"{"type":"exercise_card","exercise":{"name":"Leg Press (Machine)","muscle_group":["Legs","Quads"],"workout_type":["Legs"],"sets":null}}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let widget = try decoder.decode(ExerciseCardWidget.self, from: json)
        XCTAssertEqual(widget.exercise.name, "Leg Press (Machine)")
        XCTAssertEqual(widget.exercise.muscleGroup, "Legs")
        XCTAssertEqual(widget.exercise.workoutType, "Legs")
        XCTAssertNil(widget.exercise.equipment)
        XCTAssertNil(widget.exercise.sets)
        XCTAssertNil(widget.exercise.videoUrl)
        XCTAssertNil(widget.exercise.videoURL)
    }

    func testChatReplyIgnoresUnknownWidgetType() throws {
        let json = #"{"reply":"Logged it.","widget":{"type":"future_card","foo":1}}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let reply = try decoder.decode(ChatReply.self, from: json)
        XCTAssertEqual(reply.reply, "Logged it.")
        XCTAssertNil(reply.widget)
    }

    func testChatReplyStillDecodesWhenExerciseObjectIsMissing() throws {
        let json = #"{"reply":"Try this.","widget":{"type":"exercise_card"}}"#.data(using: .utf8)!
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let reply = try decoder.decode(ChatReply.self, from: json)
        XCTAssertEqual(reply.widget?.type, "exercise_card")
        XCTAssertEqual(reply.widget?.exercise?.displayName, "Exercise")
        XCTAssertNil(reply.widget?.exercise?.videoURL)
    }
}
