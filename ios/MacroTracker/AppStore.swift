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
    @Published var isLoadingDay = false
    @Published var isLoadingWorkouts = false
    @Published var errorMessage: String?
    @Published var toast: String?
    private let api: APIClient

    init(api: APIClient = .shared) { self.api = api }

    var dateString: String { Self.dateFormatter.string(from: selectedDate) }
    var isToday: Bool { Calendar.current.isDateInToday(selectedDate) }

    func loadAll() async {
        async let dayTask: Void = loadDay()
        async let workoutTask: Void = loadWorkoutData()
        _ = await (dayTask, workoutTask)
    }

    func loadDay() async {
        isLoadingDay = true; defer { isLoadingDay = false }
        do {
            async let dayResult = isToday ? api.today() : api.day(dateString)
            async let presetResult = api.presets()
            async let briefResult = api.brief(dateString)
            let (loadedDay, loadedPresets, loadedBrief) = try await (dayResult, presetResult, briefResult)
            day = loadedDay; presets = loadedDay.presets ?? loadedPresets.presets; brief = loadedBrief
        } catch { present(error) }
    }

    func loadWorkoutData() async {
        isLoadingWorkouts = true; defer { isLoadingWorkouts = false }
        do {
            async let workoutResult = api.workouts(dateString)
            async let exerciseResult = api.exercises()
            async let planResult = api.plan()
            async let statsResult = api.workoutStats()
            let results = try await (workoutResult, exerciseResult, planResult, statsResult)
            workouts = results.0.workouts; exercises = results.1.exercises; plan = results.2; stats = results.3
        } catch { present(error) }
    }

    func moveDay(by value: Int) async {
        guard let newDate = Calendar.current.date(byAdding: .day, value: value, to: selectedDate), newDate <= Date() else { return }
        selectedDate = newDate
        await loadAll()
    }

    func logPreset(_ preset: Preset) async {
        do {
            day = try await api.logPreset(LogPresetBody(presetName: preset.name, servings: 1, meal: preset.meal, day: isToday ? nil : dateString))
            showToast("Logged \(preset.name)")
        } catch { present(error) }
    }

    func logMeal(_ body: LogMealBody) async -> Bool {
        do { day = try await api.logMeal(body); showToast("Meal logged"); return true }
        catch { present(error); return false }
    }

    func deleteMeal(_ id: String) async {
        do { day = try await api.deleteMeal(id); showToast("Entry deleted") }
        catch { present(error) }
    }

    func saveBrief(_ text: String) async {
        do { brief = try await api.saveBrief(text, date: dateString); showToast("Note saved") }
        catch { present(error) }
    }

    func logWorkout(exercise: String, sets: [WorkoutSet], type: String) async -> Bool {
        do {
            let logged = try await api.logWorkout(LogWorkoutBody(exercise: exercise, sets: sets, workoutType: type, date: isToday ? nil : dateString))
            workouts.insert(logged, at: 0)
            stats = try? await api.workoutStats()
            showToast("Workout logged"); return true
        } catch { present(error); return false }
    }

    func deleteWorkout(_ id: String) async {
        do { _ = try await api.deleteWorkout(id); workouts.removeAll { $0.id == id }; stats = try? await api.workoutStats(); showToast("Workout deleted") }
        catch { present(error) }
    }

    func lastWorkout(_ exercise: String) async -> LastWorkoutPayload? { try? await api.lastWorkout(exercise) }
    func chat(_ message: String) async throws -> ChatPayload { try await api.chat(message) }
    func analyze(image: String) async throws -> VisionPayload { try await api.analyze(image: image) }

    private func present(_ error: Error) { errorMessage = error.localizedDescription }
    private func showToast(_ value: String) {
        toast = value
        Task { try? await Task.sleep(for: .seconds(2)); if toast == value { toast = nil } }
    }
    private static let dateFormatter: DateFormatter = { let f = DateFormatter(); f.calendar = Calendar(identifier: .gregorian); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd"; return f }()
}

