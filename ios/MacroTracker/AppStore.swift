import Foundation

@MainActor
final class AppStore: ObservableObject {
    lazy var canvas = AgentSurfaceStore(api: api)
    @Published var selectedDate = AppStore.effectiveCurrentDay()
    @Published private(set) var dayPolicy = LoggingDayPolicy.bootstrap
    @Published var day: DayPayload?
    @Published private(set) var accountHasTargets: Bool?
    private var accountTargetCalories: Double?
    private var hasAccountResponse = false
    private var accountCanvasProtocol: String?
    private var hasAdoptedDayPolicy = false
    var accountRoute: AgentRootRoute {
        guard hasAccountResponse else { return .loading }
        guard accountCanvasProtocol == "mmacros.canvas.v1" else { return .compatibility }
        return AgentRootRoute.initial(hasTargets: accountHasTargets,
            calories: accountTargetCalories, hasPlan: plan?.hasPlan)
    }
    @Published var presets: [Preset] = []
    @Published var workouts: [WorkoutEntry] = []
    @Published var workoutHistoryEntries: [WorkoutHistoryEntry] = []
    @Published var trendsPayload: TrendsPayload?
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
    private enum ErrorSource { case action, day, workouts, presets, brief }
    private var errorSource = ErrorSource.action

    init(api: APIClient = .shared) { self.api = api }

    var dateString: String { dateFormatter.string(from: selectedDate) }
    var isToday: Bool { dayCalendar.isDate(selectedDate, inSameDayAs: dayPolicy.currentDay()) }

    static func effectiveCurrentDay(now: Date = Date()) -> Date {
        LoggingDayPolicy.bootstrap.currentDay(now: now)
    }

    func selectCurrentDay(now: Date = Date()) {
        selectedDate = dayPolicy.currentDay(now: now)
    }

    func loadAll(reportErrors: Bool = true) async {
        let session = sessionGeneration
        let requestedPolicy = dayPolicy
        let requestedKey = dateString
        if hasAdoptedDayPolicy {
            // Warm reads overlap. If authoritative policy changes during this
            // batch, reconcile date-keyed workouts once, without replaying writes.
            async let dayLoad: Void = loadDay(reportErrors: reportErrors)
            async let workoutLoad: Void = loadWorkoutData(reportErrors: reportErrors)
            _ = await (dayLoad, workoutLoad)
            guard !Task.isCancelled, session == sessionGeneration else { return }
            if dateString != requestedKey || dayPolicy.timeZone != requestedPolicy.timeZone
                || dayPolicy.rolloverHour != requestedPolicy.rolloverHour {
                await loadWorkoutData(reportErrors: reportErrors)
            }
        } else {
            // Only the initial policy adoption is serialized, not optional data.
            async let presetLoad: Void = loadPresets(embedded: nil, reportErrors: reportErrors)
            await loadDay(reportErrors: reportErrors, includeExtras: false)
            guard !Task.isCancelled, session == sessionGeneration else { return }
            async let briefLoad: Void = loadBrief(date: dateString, reportErrors: reportErrors)
            async let workoutLoad: Void = loadWorkoutData(reportErrors: reportErrors)
            _ = await (presetLoad, briefLoad, workoutLoad)
        }
    }

    func loadDay(reportErrors: Bool = true, includeExtras: Bool = true) async {
        let session = sessionGeneration
        let requestedDate = selectedDate
        var completionDate = requestedDate
        let requestedDateString = dateFormatter.string(from: requestedDate)
        let requestedIsToday = dayCalendar.isDate(requestedDate, inSameDayAs: dayPolicy.currentDay())
        isLoadingDay = true
        defer {
            if session == sessionGeneration, selectedDate == completionDate {
                isLoadingDay = false
            }
        }
        do {
            let loadedDay = try await (requestedIsToday ? api.today() : api.day(requestedDateString))
            guard !Task.isCancelled, session == sessionGeneration, selectedDate == requestedDate else { return }
            if let policy = loadedDay.dayTiming {
                guard policy.isValid else { throw AgentCanvasError.invalidPayload }
                // Keep the requested calendar DATE for history, not its old
                // timezone's absolute midnight. Current reads use server date.
                let date = requestedIsToday ? policy.date(from: policy.effectiveDate) : policy.date(from: requestedDateString)
                guard let date else { throw AgentCanvasError.invalidPayload }
                dayPolicy = policy
                hasAdoptedDayPolicy = true
                selectedDate = date
                completionDate = date
            }
            // Readiness belongs to the day response, not the optional brief.
            day = loadedDay
            if requestedIsToday {
                hasAccountResponse = true
                accountCanvasProtocol = loadedDay.canvasProtocol
                accountHasTargets = loadedDay.hasTargets
                accountTargetCalories = loadedDay.targets.calories
            }
            clearLoadError(source: .day)
            let briefDateString = dateString
            if brief?.date != briefDateString { brief = nil }
            // Optional loads fail independently: a brief outage must not discard
            // presets or raise an alert over root retry/onboarding recovery.
            if includeExtras {
                async let presetLoad: Void = loadPresets(embedded: loadedDay.presets, reportErrors: reportErrors)
                async let briefLoad: Void = loadBrief(date: briefDateString, reportErrors: reportErrors)
                _ = await (presetLoad, briefLoad)
            }
        } catch {
            guard !Task.isCancelled, session == sessionGeneration, selectedDate == completionDate else { return }
            if reportErrors { present(error, session: session, source: .day) }
        }
    }

    func loadWorkoutData(reportErrors: Bool = true) async {
        let session = sessionGeneration
        let requestedDate = selectedDate
        let requestedDateString = dateFormatter.string(from: requestedDate)
        isLoadingWorkouts = true
        defer {
            if session == sessionGeneration, dayCalendar.isDate(selectedDate, inSameDayAs: requestedDate) {
                isLoadingWorkouts = false
            }
        }
        do {
            async let workoutResult = api.workouts(requestedDateString)
            async let exerciseResult = api.exercises()
            async let planResult = api.plan()
            async let statsResult = api.workoutStats()
            let results = try await (workoutResult, exerciseResult, planResult, statsResult)
            guard !Task.isCancelled, session == sessionGeneration, dayCalendar.isDate(selectedDate, inSameDayAs: requestedDate) else { return }
            workouts = results.0.workouts; exercises = results.1.exercises; plan = results.2; stats = results.3
            clearLoadError(source: .workouts)
        } catch {
            guard !Task.isCancelled, session == sessionGeneration, dayCalendar.isDate(selectedDate, inSameDayAs: requestedDate) else { return }
            if reportErrors { present(error, session: session, source: .workouts) }
        }
    }

    private func loadPresets(embedded: [Preset]?, reportErrors: Bool) async {
        let session = sessionGeneration
        do {
            let loaded: [Preset]
            if let embedded { loaded = embedded } else { loaded = try await api.presets().presets }
            guard !Task.isCancelled, session == sessionGeneration else { return }
            presets = loaded
            clearLoadError(source: .presets)
        } catch {
            if reportErrors { present(error, session: session, source: .presets) }
        }
    }

    private func loadBrief(date: String, reportErrors: Bool) async {
        let session = sessionGeneration
        do {
            let loaded = try await api.brief(date)
            guard !Task.isCancelled, session == sessionGeneration, dateString == date else { return }
            brief = loaded
            clearLoadError(source: .brief)
        } catch {
            guard !Task.isCancelled, session == sessionGeneration, dateString == date else { return }
            if reportErrors { present(error, session: session, source: .brief) }
        }
    }

    func workoutHistory(exercise: String? = nil, limit: Int = 200) async {
        let session = sessionGeneration
        do {
            let payload = try await api.workoutHistory(exercise: exercise, limit: limit)
            guard !Task.isCancelled, session == sessionGeneration else { return }
            workoutHistoryEntries = payload.workouts
        } catch { present(error, session: session) }
    }

    func trends(days: Int) async {
        let session = sessionGeneration
        do {
            let payload = try await api.trends(days: days)
            guard !Task.isCancelled, session == sessionGeneration else { return }
            trendsPayload = payload
        } catch { present(error, session: session) }
    }

    func moveDay(by value: Int) async {
        guard let newDate = dayCalendar.date(byAdding: .day, value: value, to: selectedDate),
              dayCalendar.startOfDay(for: newDate) <= dayPolicy.currentDay() else { return }
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
        let requestedDateString = dateFormatter.string(from: requestedDate)
        do {
            let saved = try await api.saveBrief(text, date: requestedDateString)
            guard session == sessionGeneration, dayCalendar.isDate(selectedDate, inSameDayAs: requestedDate) else { return }
            brief = saved; showToast("Note saved")
        } catch {
            guard session == sessionGeneration, dayCalendar.isDate(selectedDate, inSameDayAs: requestedDate) else { return }
            present(error, session: session)
        }
    }

    func logWorkout(exercise: String, sets: [WorkoutSet], type: String, onLogged: ((WorkoutEntry) -> Void)? = nil) async -> Bool {
        let session = sessionGeneration
        do {
            let logged = try await api.logWorkout(LogWorkoutBody(exercise: exercise, sets: sets, workoutType: type, date: isToday ? nil : dateString))
            guard session == sessionGeneration else { return true }
            workouts.insert(logged, at: 0)
            onLogged?(logged)
            showToast("Workout logged")
            if let updatedStats = try? await api.workoutStats(), session == sessionGeneration { stats = updatedStats }
            await workoutHistory()
            return true
        } catch { present(error, session: session); return false }
    }

    func deleteWorkout(_ id: String) async {
        let session = sessionGeneration
        do {
            _ = try await api.deleteWorkout(id)
            guard session == sessionGeneration else { return }
            workouts.removeAll { $0.id == id }
            workoutHistoryEntries.removeAll { $0.id == id }
            showToast("Workout deleted")
            if let updatedStats = try? await api.workoutStats(), session == sessionGeneration { stats = updatedStats }
            await workoutHistory()
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
    func chatHistory(limit: Int) async throws -> [ChatHistoryMessage] {
        try await api.chatHistory(limit: limit)
    }
    func barcodeFood(code: String) async throws -> BarcodeFoodPayload { try await api.barcodeFood(code: code) }

    /// Called on sign-out so the next user never sees the previous user's data.
    func reset() {
        canvas.reset()
        sessionGeneration += 1
        dayPolicy = .bootstrap
        accountHasTargets = nil; accountTargetCalories = nil
        hasAccountResponse = false; accountCanvasProtocol = nil; hasAdoptedDayPolicy = false
        selectCurrentDay()
        day = nil; presets = []; workouts = []; workoutHistoryEntries = []; trendsPayload = nil; exercises = []; plan = nil; stats = nil; brief = nil
        isLoadingDay = true; isLoadingWorkouts = false
        errorMessage = nil; toast = nil
        WorkoutSessionCompletions.removeAll()
    }

    private func clearLoadError(source: ErrorSource) {
        // GET recovery must never dismiss a failed write/action.
        if errorSource == source { errorMessage = nil }
    }
    private func present(_ error: Error, session: Int, source: ErrorSource = .action) {
        guard session == sessionGeneration else { return }
        if source != .action, errorMessage != nil, errorSource == .action { return }
        errorSource = source
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
    private var dayCalendar: Calendar { dayPolicy.calendar }
    private var dateFormatter: DateFormatter { dayPolicy.formatter }
}
