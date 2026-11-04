import Foundation

/// One planned set the user fills in during a session. Nothing here is saved
/// until Finish workout confirms it.
struct SessionSet: Identifiable, Equatable {
    let id: UUID
    var weight: Double?
    var reps: Int?
    var done: Bool

    init(id: UUID = UUID(), weight: Double? = nil, reps: Int? = nil, done: Bool = false) {
        self.id = id
        self.weight = weight
        self.reps = reps
        self.done = done
    }

    /// A set can only be marked done with a load or a rep count.
    var hasValues: Bool { (weight ?? 0) > 0 || (reps ?? 0) > 0 }

    var asWorkoutSet: WorkoutSet { WorkoutSet(weight: weight ?? 0, reps: reps ?? 0) }
}

struct SessionExercise: Identifiable, Equatable {
    var id: String { name }
    var name: String
    var sets: [SessionSet]
    /// The previous time this exercise was logged, for prefills and the hint.
    var lastSets: [WorkoutSet] = []
    var lastDate: String?
    /// Set once Finish workout has saved this exercise, so a retry after a
    /// partial failure never logs it twice.
    var logged = false

    var doneCount: Int { sets.filter(\.done).count }

    var topLastSet: WorkoutSet? {
        lastSets.max { lhs, rhs in
            lhs.weight != rhs.weight ? lhs.weight < rhs.weight : lhs.reps < rhs.reps
        }
    }
}

/// The in-progress session: planned exercises, their set rows, and exactly
/// what Finish workout will send. Pure state so it can be tested without UI.
struct WorkoutSessionDraft: Equatable {
    static let defaultSetCount = 3
    static let maxSetCount = 6
    static let defaultRestSeconds = 90

    struct Entry: Equatable {
        let exercise: String
        let sets: [WorkoutSet]
    }

    var type: String
    var exercises: [SessionExercise]

    init(type: String, exerciseNames: [String]) {
        self.type = type
        exercises = exerciseNames.map { name in
            SessionExercise(name: name, sets: (0..<Self.defaultSetCount).map { _ in SessionSet() })
        }
    }

    var plannedSetCount: Int { exercises.reduce(0) { $0 + $1.sets.count } }
    var completedSetCount: Int { exercises.reduce(0) { $0 + $1.doneCount } }

    /// Done sets with a value, per exercise, skipping anything already saved.
    var loggable: [Entry] {
        exercises.compactMap { exercise in
            guard !exercise.logged else { return nil }
            let sets = exercise.sets.filter { $0.done && $0.hasValues }.map(\.asWorkoutSet)
            return sets.isEmpty ? nil : Entry(exercise: exercise.name, sets: sets)
        }
    }

    /// The first exercise with history: "Your last squat top set was 175 lb x 8."
    var pickUpHint: (exercise: String, set: WorkoutSet)? {
        for exercise in exercises {
            if let top = exercise.topLastSet { return (exercise.name, top) }
        }
        return nil
    }

    /// Previous-session prefill. Rows the user has not touched take the
    /// matching last set; typed or completed rows are left alone.
    mutating func applyLast(exercise name: String, sets: [WorkoutSet], date: String?) {
        guard let index = exercises.firstIndex(where: { $0.name == name }) else { return }
        exercises[index].lastSets = sets
        exercises[index].lastDate = date
        guard !sets.isEmpty else { return }
        let wanted = min(max(exercises[index].sets.count, sets.count), Self.maxSetCount)
        while exercises[index].sets.count < wanted { exercises[index].sets.append(SessionSet()) }
        for (offset, last) in sets.prefix(exercises[index].sets.count).enumerated() {
            let row = exercises[index].sets[offset]
            guard !row.done, !row.hasValues else { continue }
            exercises[index].sets[offset].weight = last.weight > 0 ? last.weight : nil
            exercises[index].sets[offset].reps = last.reps > 0 ? last.reps : nil
        }
    }

    /// Marks a set done (or not). Returns false when the set has no load or
    /// reps yet, in which case nothing changes.
    @discardableResult
    mutating func toggle(set setId: UUID, in exerciseName: String) -> Bool {
        guard let e = exercises.firstIndex(where: { $0.name == exerciseName }),
              let s = exercises[e].sets.firstIndex(where: { $0.id == setId }) else { return false }
        if exercises[e].sets[s].done {
            exercises[e].sets[s].done = false
            return true
        }
        guard exercises[e].sets[s].hasValues else { return false }
        exercises[e].sets[s].done = true
        return true
    }

    mutating func addSet(to exerciseName: String) {
        guard let e = exercises.firstIndex(where: { $0.name == exerciseName }),
              exercises[e].sets.count < Self.maxSetCount else { return }
        let previous = exercises[e].sets.last
        exercises[e].sets.append(SessionSet(weight: previous?.weight, reps: previous?.reps))
    }

    mutating func removeSet(_ setId: UUID, from exerciseName: String) {
        guard let e = exercises.firstIndex(where: { $0.name == exerciseName }),
              exercises[e].sets.count > 1 else { return }
        exercises[e].sets.removeAll { $0.id == setId }
    }

    /// Replaces an exercise for today only; its rows start over.
    mutating func swap(exercise oldName: String, with newName: String) {
        guard let e = exercises.firstIndex(where: { $0.name == oldName }),
              !exercises.contains(where: { $0.name == newName }) else { return }
        exercises[e] = SessionExercise(name: newName, sets: (0..<Self.defaultSetCount).map { _ in SessionSet() })
    }

    mutating func markLogged(_ exerciseName: String) {
        guard let e = exercises.firstIndex(where: { $0.name == exerciseName }) else { return }
        exercises[e].logged = true
    }
}

/// Rest between sets, anchored to a wall-clock end so lock screen and
/// backgrounding do not drift it.
struct RestTimer: Equatable {
    let endsAt: Date
    let totalSeconds: Int

    static func start(seconds: Int, now: Date = Date()) -> RestTimer {
        RestTimer(endsAt: now.addingTimeInterval(TimeInterval(seconds)), totalSeconds: seconds)
    }

    func remaining(at now: Date) -> Int {
        max(0, Int(endsAt.timeIntervalSince(now).rounded(.up)))
    }

    func isFinished(at now: Date) -> Bool { remaining(at: now) == 0 }

    static func label(seconds: Int) -> String {
        String(format: "%d:%02d", max(0, seconds) / 60, max(0, seconds) % 60)
    }
}
