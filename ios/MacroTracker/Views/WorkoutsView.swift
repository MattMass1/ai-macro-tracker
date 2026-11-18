import SwiftUI

struct WorkoutsView: View {
    @EnvironmentObject private var store: AppStore
    let openCoach: (String?) -> Void
    var isActive = true
    @State private var sessionSelection: WorkoutSessionSelection?
    @State private var librarySelection: WorkoutLoggerSelection?
    @State private var libraryDate: String?
    @State private var showLibrary = false
    @State private var historyScope = 0
    @AppStorage(CompanionPreferences.characterKey) private var characterRaw = CompanionCharacter.male.rawValue
    @AppStorage(CompanionPreferences.visibleKey) private var showsCompanion = true

    var body: some View {
        ZStack {
            StudioBackground(alignment: .bottom)
            VStack(spacing: 0) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    HStack {
                        Text("Workouts").font(Theme.pixelFont(StudioLayout.titleSize, relativeTo: .title)).foregroundStyle(Theme.ink)
                        Spacer()
                        Button("Plan with Coach") { openCoach("Help me with my workout plan.") }
                            .font(.caption.weight(.bold)).foregroundStyle(Theme.accent)
                    }
                    DayPicker(date: store.selectedDate, canGoForward: !store.isToday) { delta in
                        Task { await store.moveDay(by: delta) }
                    }
                    if store.isToday, let plan = store.plan, plan.hasPlan == true, let session = plan.upcoming.first {
                        PlannedWorkoutCard(session: session) {
                            sessionSelection = .planned(session, date: store.dateString)
                        }
                    }
                    HStack {
                        Button("New workout", systemImage: "plus") {
                            sessionSelection = .manual(date: store.dateString)
                        }
                        .buttonStyle(PixelPrimaryButtonStyle())
                        Menu {
                            Button("Choose an exercise", systemImage: "list.bullet") { libraryDate = store.dateString; showLibrary = true }
                            Button("Log rest day", systemImage: "moon") {
                                sessionSelection = .manual(date: store.dateString, exercise: .init(type: "Rest", exercise: "Rest Day"))
                            }
                        } label: {
                            Image(systemName: "ellipsis").frame(width: 46, height: 46)
                        }
                        .buttonStyle(PixelSecondaryButtonStyle())
                        .accessibilityLabel("Workout options")
                    }
                    if store.isToday, let tomorrow = store.plan?.tomorrow { TomorrowWorkoutCard(day: tomorrow) }
                    if store.isLoadingWorkouts && store.stats == nil { workoutSkeleton }
                    if let stats = store.stats { WorkoutDashboard(stats: stats) }
                    if let plan = store.plan, (plan.hasPlan != true || !store.isToday) { WorkoutPlanCard(plan: plan) }
                    Picker("History range", selection: $historyScope) {
                        Text("Selected day").tag(0)
                        Text("All").tag(1)
                    }
                    .pickerStyle(.segmented)
                    if historyScope == 0 {
                        WorkoutHistory(workouts: store.workouts) { id in Task { await store.deleteWorkout(id) } }
                    } else {
                        AggregateWorkoutHistory(workouts: store.workoutHistoryEntries) { id in Task { await store.deleteWorkout(id) } }
                    }
                }
                .padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 24)
            }
            .refreshable {
                async let selectedDay: Void = store.loadWorkoutData()
                async let fullHistory: Void = store.workoutHistory()
                _ = await (selectedDay, fullHistory)
            }
            if showsCompanion {
                CompanionStage(chat: store.chat, character: CompanionCharacter(rawValue: characterRaw) ?? .male, size: 144)
                    .frame(maxWidth: .infinity)
            }
            }
        }
        .task(id: isActive) {
            if isActive { await store.workoutHistory() }
        }
        .navigationBarHidden(true)
        .sheet(item: $sessionSelection) { selection in
            WorkoutSessionView(selection: selection, onAskCoach: { text in
                sessionSelection = nil
                openCoach(text)
            })
        }
        .sheet(isPresented: $showLibrary, onDismiss: {
            if let selection = librarySelection {
                librarySelection = nil
                sessionSelection = .manual(date: libraryDate ?? store.dateString, exercise: selection)
            }
            libraryDate = nil
        }) {
            ExerciseLibraryView { selection in
                librarySelection = selection
                showLibrary = false
            }
        }
    }

    private var workoutSkeleton: some View {
        RoundedRectangle(cornerRadius: Theme.cardRadius).fill(Theme.surface).frame(height: 130)
            .redacted(reason: .placeholder).shimmering()
    }
}

/// A summary, not a second checklist or set-entry interface.
struct PlannedWorkoutCard: View {
    let session: WorkoutPlanPayload.PlannedDay
    let onStart: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionLabel(text: "Today's workout")
            Text(session.type).font(.title2.bold()).foregroundStyle(Theme.ink)
            if session.exercises.isEmpty {
                Text("No exercises assigned yet. Add an exercise when you open the workout.")
                    .font(.subheadline).foregroundStyle(Theme.muted)
            } else {
                Text(session.exercises.map(\.name).joined(separator: " · "))
                    .font(.subheadline).foregroundStyle(Theme.muted)
            }
            Button("Open workout", action: onStart).buttonStyle(PixelPrimaryButtonStyle())
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .appCard()
    }
}

struct TomorrowWorkoutCard: View {
    let day: WorkoutPlanPayload.ScheduledDay

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                SectionLabel(text: "Tomorrow")
                Spacer()
                Text(day.date).font(.caption).foregroundStyle(Theme.muted)
            }
            Text(day.type).font(.title3.weight(.bold)).foregroundStyle(Theme.ink)
            if day.exercises.isEmpty {
                Text("No exercises scheduled.")
                    .font(.subheadline).foregroundStyle(Theme.muted)
            } else {
                Text(day.exercises.map(\.name).joined(separator: " · "))
                    .font(.subheadline).foregroundStyle(Theme.muted)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .appCard()
    }
}

struct WorkoutLoggerSelection: Identifiable {
    let type: String
    let exercise: String
    var muscleGroup: String? = nil
    var equipment: String? = nil
    var id: String { "\(type)|\(exercise)" }
}

/// Local checkmarks for today's planned session. Device-scoped (not per-user),
/// so sign-out must wipe them via `removeAll()` or the next claim inherits them.
enum WorkoutSessionCompletions {
    static let keyPrefix = "workout-session-completed."

    static func key(date: String, type: String) -> String {
        "\(keyPrefix)\(date).\(type)"
    }

    static func saved(date: String, type: String) -> Set<String> {
        Set(UserDefaults.standard.stringArray(forKey: key(date: date, type: type)) ?? [])
    }

    static func save(_ completed: Set<String>, date: String, type: String) {
        UserDefaults.standard.set(Array(completed), forKey: key(date: date, type: type))
    }

    static func removeAll() {
        let defaults = UserDefaults.standard
        for key in defaults.dictionaryRepresentation().keys where key.hasPrefix(keyPrefix) {
            defaults.removeObject(forKey: key)
        }
    }

    static func pruneOld() {
        let defaults = UserDefaults.standard
        let calendar = Calendar(identifier: .gregorian)
        guard let cutoff = calendar.date(byAdding: .day, value: -30, to: calendar.startOfDay(for: Date())) else { return }
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"

        for key in defaults.dictionaryRepresentation().keys where key.hasPrefix(keyPrefix) {
            let suffix = key.dropFirst(keyPrefix.count)
            guard suffix.count > 10, suffix[suffix.index(suffix.startIndex, offsetBy: 10)] == "." else { continue }
            let dateText = String(suffix.prefix(10))
            if let storedDate = formatter.date(from: dateText), storedDate < cutoff {
                defaults.removeObject(forKey: key)
            }
        }
    }
}

enum CanonicalWorkoutType {
    static let ordered = ["Push", "Pull", "Legs", "Abs", "Cardio", "Full Body"]
    static let all = ordered + ["Rest"]

    static func value(for rawValue: String, muscleGroups: [String] = []) -> String {
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if let match = all.first(where: { $0.caseInsensitiveCompare(trimmed) == .orderedSame }) {
            return match
        }
        for type in ordered where type != "Legs" {
            if muscleGroups.contains(where: { $0.caseInsensitiveCompare(type) == .orderedSame }) { return type }
        }
        if muscleGroups.contains(where: { muscleGroup in
            ["Quads", "Hams", "Legs"].contains { $0.caseInsensitiveCompare(muscleGroup) == .orderedSame }
        }) { return "Legs" }
        return "Push"
    }
}

struct ExerciseLibraryView: View {
    private struct SectionGroup: Identifiable {
        let type: String
        let exercises: [LibraryExercise]
        var id: String { type }
    }

    @Environment(\.dismiss) private var dismiss
    private let allowsMultipleSelection: Bool
    private let onSelect: ((WorkoutLoggerSelection) -> Void)?
    private let onConfirmMultiple: (([LibraryExercise]) -> Void)?
    @State private var exercises: [LibraryExercise] = []
    @State private var search = ""
    @State private var isLoading = true
    @State private var selectedIDs: Set<String> = []
    @State private var selectedOrder: [LibraryExercise] = []

    init(onSelect: @escaping (WorkoutLoggerSelection) -> Void) {
        self.allowsMultipleSelection = false
        self.onSelect = onSelect
        self.onConfirmMultiple = nil
    }

    init(allowsMultipleSelection: Bool, onConfirm: @escaping ([LibraryExercise]) -> Void) {
        self.allowsMultipleSelection = allowsMultipleSelection
        self.onSelect = nil
        self.onConfirmMultiple = onConfirm
    }

    private var filtered: [LibraryExercise] {
        guard !search.isEmpty else { return exercises }
        return exercises.filter {
            $0.name.localizedCaseInsensitiveContains(search)
                || $0.muscleGroup.contains { $0.localizedCaseInsensitiveContains(search) }
                || $0.workoutType.localizedCaseInsensitiveContains(search)
        }
    }

    private var groupedExercises: [SectionGroup] {
        let visible = allowsMultipleSelection ? filtered.filter { !selectedIDs.contains($0.id) } : filtered
        let grouped = Dictionary(grouping: visible) {
            CanonicalWorkoutType.value(for: $0.workoutType, muscleGroups: $0.muscleGroup)
        }
        return CanonicalWorkoutType.ordered.compactMap { type in
            guard let exercises = grouped[type], !exercises.isEmpty else { return nil }
            return SectionGroup(type: type, exercises: exercises)
        }
    }

    var body: some View {
        NavigationStack {
            Group {
                if isLoading {
                    ProgressView()
                } else if exercises.isEmpty {
                    ContentUnavailableView("Exercise library unavailable", systemImage: "dumbbell")
                } else {
                    List {
                        if allowsMultipleSelection, !selectedOrder.isEmpty {
                            Section("Selected") {
                                ForEach(selectedOrder) { exercise in
                                    exerciseRow(exercise, type: CanonicalWorkoutType.value(
                                        for: exercise.workoutType,
                                        muscleGroups: exercise.muscleGroup
                                    ))
                                }
                            }
                        }
                        ForEach(groupedExercises) { group in
                            Section(group.type) {
                                ForEach(group.exercises) { exercise in
                                    exerciseRow(exercise, type: group.type)
                                }
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                    .searchable(text: $search, prompt: "Search exercises")
                }
            }
            .background(Theme.canvas)
            .navigationTitle(allowsMultipleSelection ? "Pick exercises" : "Add exercise")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                if allowsMultipleSelection {
                    ToolbarItem(placement: .confirmationAction) {
                        Button(selectedOrder.isEmpty ? "Use these" : "Use \(selectedOrder.count)") {
                            onConfirmMultiple?(selectedOrder)
                            dismiss()
                        }
                        .fontWeight(.semibold)
                        .disabled(selectedOrder.isEmpty)
                    }
                }
            }
        }
        .task {
            // Load the full library once (lazy — only when the picker opens).
            if exercises.isEmpty, let payload = try? await APIClient.shared.library() {
                exercises = payload.exercises
            }
            isLoading = false
        }
        .searchable(text: $search, prompt: "Search exercises")
        .onChange(of: search) { _, query in
            // Server-side search: fetch matches instead of filtering 1,542 rows locally.
            let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
            guard trimmed.count >= 2 else { return }
            Task {
                if let payload = try? await APIClient.shared.library(query: trimmed) {
                    exercises = payload.exercises
                }
            }
        }
    }

    private func exerciseRow(_ exercise: LibraryExercise, type: String) -> some View {
        Button { tap(exercise, type: type) } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(exercise.name).foregroundStyle(Theme.ink)
                    Text([exercise.muscleGroup.joined(separator: ", "), exercise.equipment]
                        .filter { !$0.isEmpty }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(Theme.muted)
                }
                Spacer(minLength: 0)
                if allowsMultipleSelection {
                    Image(systemName: selectedIDs.contains(exercise.id) ? "checkmark.circle.fill" : "circle")
                        .font(.title3)
                        .foregroundStyle(selectedIDs.contains(exercise.id) ? Theme.accent : Theme.muted)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func tap(_ exercise: LibraryExercise, type: String) {
        if allowsMultipleSelection {
            toggle(exercise)
            return
        }
        onSelect?(.init(
            type: type,
            exercise: exercise.name,
            muscleGroup: exercise.muscleGroup.first,
            equipment: exercise.equipment.isEmpty ? nil : exercise.equipment
        ))
    }

    private func toggle(_ exercise: LibraryExercise) {
        if selectedIDs.contains(exercise.id) {
            selectedIDs.remove(exercise.id)
            selectedOrder.removeAll { $0.id == exercise.id }
        } else {
            selectedIDs.insert(exercise.id)
            selectedOrder.append(exercise)
        }
    }
}

extension WorkoutPlanWrite {
    static func fromPickedExercises(_ exercises: [LibraryExercise]) -> WorkoutPlanWrite? {
        guard !exercises.isEmpty else { return nil }
        let grouped = Dictionary(grouping: exercises) { exercise -> String in
            let type = CanonicalWorkoutType.value(for: exercise.workoutType, muscleGroups: exercise.muscleGroup)
            return type == "Rest" ? "Full Body" : type
        }
        let rotation = CanonicalWorkoutType.ordered.filter { grouped[$0] != nil }
        guard !rotation.isEmpty else { return nil }
        var days: [String: Day] = [:]
        for type in rotation {
            let items = Array((grouped[type] ?? []).prefix(30))
            days[type] = Day(
                label: type,
                exercises: items.map { exercise in
                    Day.Exercise(
                        name: exercise.name,
                        sets: 3,
                        reps: "8-12",
                        restSec: 90,
                        swaps: exercise.swaps.isEmpty ? nil : exercise.swaps
                    )
                }
            )
        }
        return WorkoutPlanWrite(
            version: 1,
            rotation: rotation,
            daysPerWeek: min(7, max(1, rotation.count)),
            days: days
        )
    }
}

private struct WorkoutDashboard: View {
    let stats: WorkoutStatsPayload
    private let muscles = ["Push", "Pull", "Quads", "Hams", "Abs"]
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: "Dashboard")
            HStack(spacing: 10) {
                MetricBlock(label: "TODAY", value: "\(stats.today.entries)", detail: "entries", icon: "calendar")
                MetricBlock(label: "THIS WEEK", value: "\(stats.week.daysLogged)", detail: "of 7 days", icon: "chart.bar.fill")
            }
            VStack(alignment: .leading, spacing: 11) {
                HStack { SectionLabel(text: stats.coverage.muscleCoverageComplete == false ? "Classified coverage" : "Coverage"); Spacer(); Text("\(stats.week.totalSets) sets").font(.caption.monospacedDigit()).foregroundStyle(.secondary) }
                let maxValue = max(1, muscles.map { stats.coverage.muscleGroups[$0] ?? 0 }.max() ?? 1)
                ForEach(muscles, id: \.self) { muscle in
                    let count = stats.coverage.muscleGroups[muscle] ?? 0
                    HStack { Text(muscle).font(.caption).frame(width: 48, alignment: .leading); GeometryReader { geo in ZStack(alignment: .leading) { Capsule().fill(Color.secondary.opacity(0.1)); Capsule().fill(coverageColor(muscle)).frame(width: geo.size.width * CGFloat(count) / CGFloat(maxValue)) } }.frame(height: 7); Text("\(count)").font(.caption2.monospacedDigit()).frame(width: 20) }
                }
                if let note = stats.coverage.note { Text(note).font(.caption).foregroundStyle(Theme.sectionInk) }
                if stats.coverage.muscleCoverageComplete != false, !stats.coverage.untouched.isEmpty { Text("Not hit: \(stats.coverage.untouched.joined(separator: ", "))").font(.caption).foregroundStyle(Theme.carbs) }
            }.appCard()
            if !stats.prs.isEmpty {
                VStack(alignment: .leading, spacing: 10) { SectionLabel(text: "Personal records"); ScrollView(.horizontal, showsIndicators: false) { HStack { ForEach(stats.prs.prefix(6)) { pr in VStack(alignment: .leading, spacing: 3) { Image(systemName: "trophy.fill").foregroundStyle(Theme.accent); Text(pr.exercise).font(.caption.weight(.semibold)).lineLimit(1); Text("\(pr.maxWeight.formatted()) lb").font(.caption2.monospacedDigit()).foregroundStyle(Theme.muted) }.frame(width: 118, alignment: .leading).padding(12).background(Theme.accentTint, in: RoundedRectangle(cornerRadius: 15)) } } } }.appCard()
            }
        }
    }
    private func coverageColor(_ value: String) -> Color { value == "Push" ? Theme.accent : value == "Pull" ? Theme.carbs : value == "Abs" ? Theme.fiber : Theme.protein }
}

private struct MetricBlock: View {
    let label: String; let value: String; let detail: String; let icon: String
    var body: some View { VStack(alignment: .leading, spacing: 7) { HStack { SectionLabel(text: label); Spacer(); Image(systemName: icon).font(.caption).foregroundStyle(Theme.accent) }; HStack(alignment: .firstTextBaseline, spacing: 5) { Text(value).font(.system(size: 36, weight: .bold, design: .rounded)).monospacedDigit(); Text(detail).font(.caption).foregroundStyle(.secondary) } }.frame(maxWidth: .infinity, alignment: .leading).appCard() }
}

private struct WorkoutPlanCard: View {
    let plan: WorkoutPlanPayload
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: "Your program")
            if let next = plan.upcoming.first {
                Text("Next: \(next.type)").font(.headline).foregroundStyle(Theme.ink)
                Text(next.exercises.map(\.name).joined(separator: " · "))
                    .font(.subheadline).foregroundStyle(Theme.muted)
            } else {
                Text("No workout planned. Use New workout, or plan with Coach.")
                    .font(.subheadline).foregroundStyle(Theme.muted)
            }
            if !plan.core.isEmpty {
                SectionLabel(text: "Core, any day")
                Text(plan.core.map(\.name).joined(separator: " · ")).font(.caption).foregroundStyle(Theme.muted)
            }
        }.appCard()
    }
}

private struct WorkoutHistory: View {
    let workouts: [WorkoutEntry]; let onDelete: (String) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: "Workout log")
            if workouts.isEmpty { EmptyState(icon: "dumbbell", title: "No training logged", message: "Log your first exercise. Previous sets will be offered the next time you choose it.") }
            ForEach(workouts) { workout in
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) { Text(workout.exercise).font(.subheadline.weight(.semibold)); Text(workout.sets.map { $0.weight > 0 ? "\($0.weight.formatted())×\($0.reps)" : "\($0.reps) reps" }.joined(separator: "  ·  ")).font(.caption.monospacedDigit()).foregroundStyle(.secondary); Text(workout.workoutType.joined(separator: " · ").uppercased()).font(.caption2.weight(.bold)).tracking(1).foregroundStyle(Theme.accent) }
                    Spacer(); Menu { Button("Delete workout", systemImage: "trash", role: .destructive) { onDelete(workout.id) } } label: { Image(systemName: "ellipsis") }
                }.appCard(padding: 14)
            }
        }
    }
}

private struct AggregateWorkoutHistory: View {
    let workouts: [WorkoutHistoryEntry]
    let onDelete: (String) -> Void

    private var groups: [(date: String, workouts: [WorkoutHistoryEntry])] {
        Dictionary(grouping: workouts, by: \.date)
            .map { (date: $0.key, workouts: $0.value) }
            .sorted { $0.date > $1.date }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionLabel(text: "History")
            if workouts.isEmpty {
                EmptyState(icon: "clock.arrow.circlepath", title: "No workout history", message: "Your logged training will appear here.")
            }
            ForEach(groups, id: \.date) { group in
                SectionLabel(text: dayLabel(group.date))
                VStack(spacing: 0) {
                    ForEach(Array(group.workouts.enumerated()), id: \.element.id) { index, workout in
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: "dumbbell.fill")
                                .foregroundStyle(Theme.accent)
                                .frame(width: 28, height: 28)
                                .background(Theme.accentTint, in: Circle())
                            VStack(alignment: .leading, spacing: 5) {
                                Text(workout.exercise).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
                                Text(setSummary(workout.sets)).font(.caption.monospacedDigit()).foregroundStyle(Theme.muted)
                                Text(workout.workoutType.joined(separator: " · ").uppercased())
                                    .font(.caption2.weight(.bold)).tracking(1).foregroundStyle(Theme.accent)
                            }
                            Spacer()
                            Menu {
                                Button("Delete workout", systemImage: "trash", role: .destructive) { onDelete(workout.id) }
                            } label: { Image(systemName: "ellipsis").foregroundStyle(Theme.muted) }
                        }
                        .padding(.vertical, 12)
                        if index < group.workouts.count - 1 { Divider() }
                    }
                }
                .appCard(padding: 14)
            }
        }
    }

    private func setSummary(_ sets: [WorkoutSet]) -> String {
        sets.map { $0.weight > 0 ? "\($0.weight.formatted())×\($0.reps)" : "\($0.reps) reps" }.joined(separator: "  ·  ")
    }

    private func dayLabel(_ date: String) -> String {
        guard let parsed = Self.dateFormatter.date(from: date) else { return date }
        return parsed.formatted(.dateTime.weekday(.wide).month(.abbreviated).day()).uppercased()
    }

    private static let dateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()
}

enum WorkoutLoggerType {
    static func isValid(_ label: String) -> Bool {
        // Same bounded display-label grammar as the server, not a split enum.
        label.range(of: #"\A[A-Za-z0-9][A-Za-z0-9 /&()+.'_\-]{0,79}\z"#, options: .regularExpression) != nil
    }
    static func value(for label: String) -> String {
        guard isValid(label) else { return "" }
        return CanonicalWorkoutType.all.first { $0.caseInsensitiveCompare(label) == .orderedSame } ?? label
    }
    static func choices(for label: String) -> [String] {
        let selected = value(for: label)
        return CanonicalWorkoutType.all + (selected.isEmpty || CanonicalWorkoutType.all.contains(selected) ? [] : [selected])
    }
}
