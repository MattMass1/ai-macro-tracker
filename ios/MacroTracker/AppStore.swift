import Foundation

@MainActor
final class AppStore: ObservableObject {
    @Published var selectedDate = Date()
    @Published var day: DayPayload?
    @Published var presets: [Preset] = []
    @Published var workouts: [WorkoutEntry] = []
    @Published var exercises: [KnownExercise] = []
    @Published var plan: WorkoutPlanPayload?
    @Published var stats: WorkoutStatsPayload?
    @Published var brief: BriefPayload?
    @Published var isLoadingDay = true
    @Published var isLoadingWorkouts = false
    @Published var errorMessage: String?
    @Published var toast: String?
    private let api: APIClient
    /// Bumped by reset() so requests still in flight from a previous session
    /// can never publish data, errors, or toasts into the next user's session.
    /// The same-day guards alone don't cover this: reset() lands on the same
    /// calendar day, so only the generation check distinguishes sessions.
    private var sessionGeneration = 0

    init(api: APIClient = .shared) { self.api = api }

    var dateString: String { Self.dateFormatter.string(from: selectedDate) }
    var isToday: Bool { Calendar.current.isDateInToday(selectedDate) }

    func loadAll() async {
        async let dayTask: Void = loadDay()
        async let workoutTask: Void = loadWorkoutData()
        _ = await (dayTask, workoutTask)
    }

    func loadDay() async {
        let session = sessionGeneration
        let requestedDate = selectedDate
        let requestedDateString = Self.dateFormatter.string(from: requestedDate)
        let requestedIsToday = Calendar.current.isDateInToday(requestedDate)
        isLoadingDay = true
        defer {
            if session == sessionGeneration, Calendar.current.isDate(selectedDate, inSameDayAs: requestedDate) {
                isLoadingDay = false
            }
        }
        do {
            async let dayResult = requestedIsToday ? api.today() : api.day(requestedDateString)
            async let presetResult = api.presets()
            async let briefResult = api.brief(requestedDateString)
            let (loadedDay, loadedPresets, loadedBrief) = try await (dayResult, presetResult, briefResult)
            guard !Task.isCancelled, session == sessionGeneration, Calendar.current.isDate(selectedDate, inSameDayAs: requestedDate) else { return }
            day = loadedDay; presets = loadedDay.presets ?? loadedPresets.presets; brief = loadedBrief
        } catch {
            guard !Task.isCancelled, session == sessionGeneration, Calendar.current.isDate(selectedDate, inSameDayAs: requestedDate) else { return }
            present(error, session: session)
        }
    }

    func loadWorkoutData() async {
        let session = sessionGeneration
        let requestedDate = selectedDate
        let requestedDateString = Self.dateFormatter.string(from: requestedDate)
        isLoadingWorkouts = true
        defer {
            if session == sessionGeneration, Calendar.current.isDate(selectedDate, inSameDayAs: requestedDate) {
                isLoadingWorkouts = false
            }
        }
        do {
            async let workoutResult = api.workouts(requestedDateString)
            async let exerciseResult = api.exercises()
            async let planResult = api.plan()
            async let statsResult = api.workoutStats()
            let results = try await (workoutResult, exerciseResult, planResult, statsResult)
            guard !Task.isCancelled, session == sessionGeneration, Calendar.current.isDate(selectedDate, inSameDayAs: requestedDate) else { return }
            workouts = results.0.workouts; exercises = results.1.exercises; plan = results.2; stats = results.3
        } catch {
            guard !Task.isCancelled, session == sessionGeneration, Calendar.current.isDate(selectedDate, inSameDayAs: requestedDate) else { return }
            present(error, session: session)
        }
    }

    func moveDay(by value: Int) async {
        guard let newDate = Calendar.current.date(byAdding: .day, value: value, to: selectedDate), newDate <= Date() else { return }
        selectedDate = newDate
        await loadAll()
    }

    func logPreset(_ preset: Preset) async {
        let session = sessionGeneration
        do {
            let updated = try await api.logPreset(LogPresetBody(presetName: preset.name, servings: 1, meal: preset.meal, day: isToday ? nil : dateString))
            guard session == sessionGeneration else { return }
            day = updated
            showToast("Logged \(preset.name)")
        } catch { present(error, session: session) }
    }

    func logMeal(_ body: LogMealBody) async -> Bool {
        let session = sessionGeneration
        do {
            let updated = try await api.logMeal(body)
            guard session == sessionGeneration else { return true }
            day = updated; showToast("Meal logged"); return true
        } catch { present(error, session: session); return false }
    }

    func deleteMeal(_ id: String) async {
        let session = sessionGeneration
        do {
            _ = try await api.deleteMeal(id)
            guard session == sessionGeneration else { return }
            showToast("Entry deleted")
            await loadDay()
        } catch { present(error, session: session) }
    }

    func saveBrief(_ text: String) async {
        let session = sessionGeneration
        let requestedDate = selectedDate
        let requestedDateString = Self.dateFormatter.string(from: requestedDate)
        do {
            let saved = try await api.saveBrief(text, date: requestedDateString)
            guard session == sessionGeneration, Calendar.current.isDate(selectedDate, inSameDayAs: requestedDate) else { return }
            brief = saved; showToast("Note saved")
        } catch {
            guard session == sessionGeneration, Calendar.current.isDate(selectedDate, inSameDayAs: requestedDate) else { return }
            present(error, session: session)
        }
    }

    func logWorkout(exercise: String, sets: [WorkoutSet], type: String) async -> Bool {
        let session = sessionGeneration
        do {
            let logged = try await api.logWorkout(LogWorkoutBody(exercise: exercise, sets: sets, workoutType: type, date: isToday ? nil : dateString))
            guard session == sessionGeneration else { return true }
            workouts.insert(logged, at: 0)
            showToast("Workout logged")
            if let updatedStats = try? await api.workoutStats(), session == sessionGeneration { stats = updatedStats }
            return true
        } catch { present(error, session: session); return false }
    }

    func deleteWorkout(_ id: String) async {
        let session = sessionGeneration
        do {
            _ = try await api.deleteWorkout(id)
            guard session == sessionGeneration else { return }
            workouts.removeAll { $0.id == id }
            showToast("Workout deleted")
            if let updatedStats = try? await api.workoutStats(), session == sessionGeneration { stats = updatedStats }
        } catch { present(error, session: session) }
    }

    func lastWorkout(_ exercise: String) async -> LastWorkoutPayload? { try? await api.lastWorkout(exercise) }
    func savePlan(_ plan: WorkoutPlanWrite, reportError: Bool = true) async -> Bool {
        let session = sessionGeneration
        do {
            _ = try await api.savePlan(plan)
            guard session == sessionGeneration else { return true }
            await loadWorkoutData()
            return true
        } catch {
            if reportError { present(error, session: session) }
            return false
        }
    }
    func chat(_ message: String, metrics: ChatMetrics? = nil) async throws -> ChatReply {
        try await api.chat(message, metrics: metrics)
    }
    func analyze(image: String) async throws -> VisionPayload { try await api.analyze(image: image) }
    func barcodeFood(code: String) async throws -> BarcodeFoodPayload { try await api.barcodeFood(code: code) }

    /// Called on sign-out so the next user never sees the previous user's data.
    func reset() {
        sessionGeneration += 1
        selectedDate = Date()
        day = nil; presets = []; workouts = []; exercises = []; plan = nil; stats = nil; brief = nil
        isLoadingDay = true; isLoadingWorkouts = false
        errorMessage = nil; toast = nil
        WorkoutSessionCompletions.removeAll()
    }

    private func present(_ error: Error, session: Int) {
        guard session == sessionGeneration else { return }
        errorMessage = error.localizedDescription
    }
    private func showToast(_ value: String) {
        let session = sessionGeneration
        toast = value
        Task {
            try? await Task.sleep(for: .seconds(2))
            guard session == sessionGeneration, toast == value else { return }
            toast = nil
        }
    }
    private static let dateFormatter: DateFormatter = { let f = DateFormatter(); f.calendar = Calendar(identifier: .gregorian); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd"; return f }()
}
