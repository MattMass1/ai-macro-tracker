import SwiftUI

struct WorkoutsView: View {
    @EnvironmentObject private var store: AppStore
    @State private var loggerSelection: WorkoutLoggerSelection?
    @State private var librarySelection: WorkoutLoggerSelection?
    @State private var showLibrary = false
    var body: some View {
        ZStack { Theme.canvas.ignoresSafeArea()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    DayPicker(date: store.selectedDate, canGoForward: !store.isToday) { delta in Task { await store.moveDay(by: delta) } }
                    if store.isToday, let plan = store.plan, plan.hasPlan == true, let session = plan.upcoming.first {
                        TodaysSessionCard(
                            session: session,
                            date: store.dateString,
                            onLog: { loggerSelection = $0 },
                            onAddMore: { showLibrary = true }
                        )
                        .id(session.type)
                    }
                    if store.isLoadingWorkouts && store.stats == nil { workoutSkeleton }
                    if let stats = store.stats { WorkoutDashboard(stats: stats) }
                    if let plan = store.plan, (plan.hasPlan != true || !store.isToday) {
                        WorkoutPlanCard(plan: plan)
                    }
                    WorkoutHistory(workouts: store.workouts) { id in Task { await store.deleteWorkout(id) } }
                }.padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 96)
            }.refreshable { await store.loadWorkoutData() }
        }
        .navigationBarHidden(true)
        .sheet(item: $loggerSelection) { selection in
            WorkoutLoggerView(initialType: selection.type, initialExercise: selection.exercise)
        }
        .sheet(isPresented: $showLibrary, onDismiss: {
            if let selection = librarySelection {
                librarySelection = nil
                loggerSelection = selection
            }
        }) {
            ExerciseLibraryView { selection in
                librarySelection = selection
                showLibrary = false
            }
        }
    }
    private var workoutSkeleton: some View { VStack(spacing: 10) { HStack { RoundedRectangle(cornerRadius: 20).frame(height: 130); RoundedRectangle(cornerRadius: 20).frame(height: 130) }; RoundedRectangle(cornerRadius: 20).frame(height: 160) }.foregroundStyle(Theme.surface).redacted(reason: .placeholder).shimmering() }
}

private struct WorkoutLoggerSelection: Identifiable {
    let type: String
    let exercise: String
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

private enum CanonicalWorkoutType {
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

private struct TodaysSessionCard: View {
    let session: WorkoutPlanPayload.PlannedDay
    let date: String
    let onLog: (WorkoutLoggerSelection) -> Void
    let onAddMore: () -> Void
    @State private var exercises: [String]
    @State private var completed: Set<String>
    @State private var library: [LibraryExercise] = []

    init(
        session: WorkoutPlanPayload.PlannedDay,
        date: String,
        onLog: @escaping (WorkoutLoggerSelection) -> Void,
        onAddMore: @escaping () -> Void
    ) {
        self.session = session
        self.date = date
        self.onLog = onLog
        self.onAddMore = onAddMore
        let names = session.exercises.map(\.name)
        _exercises = State(initialValue: names)
        _completed = State(initialValue: WorkoutSessionCompletions.saved(date: date, type: session.type))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    SectionLabel(text: "Today's session")
                    Text(session.type).font(.title3.weight(.bold)).foregroundStyle(Theme.ink)
                }
                Spacer()
                Image(systemName: "figure.strengthtraining.traditional")
                    .foregroundStyle(Theme.accent)
                    .padding(10)
                    .background(Theme.accentTint, in: Circle())
            }
            if exercises.isEmpty {
                Text("No exercises assigned for this session.")
                    .font(.subheadline).foregroundStyle(Theme.muted)
            } else {
                ForEach(Array(exercises.enumerated()), id: \.offset) { index, name in
                    HStack(spacing: 10) {
                        Button { toggle(name) } label: {
                            Image(systemName: completed.contains(name) ? "checkmark.circle.fill" : "circle")
                                .font(.title3)
                                .foregroundStyle(completed.contains(name) ? Theme.accent : Theme.muted)
                        }
                        .buttonStyle(.plain)
                        Button { onLog(.init(type: session.type, exercise: name)) } label: {
                            Text(name)
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(completed.contains(name) ? Theme.muted : Theme.ink)
                                .strikethrough(completed.contains(name))
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        Menu {
                            let swaps = swaps(for: name)
                            if swaps.isEmpty {
                                Text("No swaps available")
                            } else {
                                ForEach(swaps, id: \.self) { swap in
                                    Button(swap) { swapExercise(at: index, from: name, to: swap) }
                                }
                            }
                        } label: {
                            Text("SWAP").font(.caption2.weight(.bold)).tracking(0.8).foregroundStyle(Theme.accent)
                        }
                    }
                    if index < exercises.count - 1 { Divider() }
                }
            }
            Button(action: onAddMore) {
                Label("Add more", systemImage: "plus")
                    .font(.subheadline.weight(.bold)).frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered).tint(Theme.accent)
        }
        .appCard()
        .task {
            WorkoutSessionCompletions.pruneOld()
            if let payload = try? await APIClient.shared.library() {
                library = payload.exercises
            }
        }
    }

    private func swaps(for name: String) -> [String] {
        library.first { $0.name.caseInsensitiveCompare(name) == .orderedSame }?.swaps ?? []
    }

    private func toggle(_ name: String) {
        if completed.contains(name) { completed.remove(name) } else { completed.insert(name) }
        saveCompletions()
    }

    private func swapExercise(at index: Int, from oldName: String, to newName: String) {
        exercises[index] = newName
        if completed.remove(oldName) != nil {
            completed.insert(newName)
        }
        saveCompletions()
    }

    private func saveCompletions() {
        WorkoutSessionCompletions.save(completed, date: date, type: session.type)
    }
}

private struct ExerciseLibraryView: View {
    private struct SectionGroup: Identifiable {
        let type: String
        let exercises: [LibraryExercise]
        var id: String { type }
    }

    @Environment(\.dismiss) private var dismiss
    let onSelect: (WorkoutLoggerSelection) -> Void
    @State private var exercises: [LibraryExercise] = []
    @State private var search = ""
    @State private var isLoading = true

    private var filtered: [LibraryExercise] {
        guard !search.isEmpty else { return exercises }
        return exercises.filter {
            $0.name.localizedCaseInsensitiveContains(search)
                || $0.muscleGroup.contains { $0.localizedCaseInsensitiveContains(search) }
                || $0.workoutType.localizedCaseInsensitiveContains(search)
        }
    }

    private var groupedExercises: [SectionGroup] {
        let grouped = Dictionary(grouping: filtered) {
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
                        ForEach(groupedExercises) { group in
                            Section(group.type) {
                                ForEach(group.exercises) { exercise in
                                    Button { onSelect(.init(type: group.type, exercise: exercise.name)) } label: {
                                        VStack(alignment: .leading, spacing: 3) {
                                            Text(exercise.name).foregroundStyle(Theme.ink)
                                            Text([exercise.muscleGroup.joined(separator: ", "), exercise.equipment]
                                                .filter { !$0.isEmpty }.joined(separator: " · "))
                                                .font(.caption).foregroundStyle(Theme.muted)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                    .searchable(text: $search, prompt: "Search exercises")
                }
            }
            .background(Theme.canvas)
            .navigationTitle("Add exercise")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
        .task {
            if let payload = try? await APIClient.shared.library() {
                exercises = payload.exercises
            }
            isLoading = false
        }
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
                HStack { SectionLabel(text: "Coverage"); Spacer(); Text("\(stats.week.totalSets) sets").font(.caption.monospacedDigit()).foregroundStyle(.secondary) }
                let maxValue = max(1, muscles.map { stats.coverage.muscleGroups[$0] ?? 0 }.max() ?? 1)
                ForEach(muscles, id: \.self) { muscle in
                    let count = stats.coverage.muscleGroups[muscle] ?? 0
                    HStack { Text(muscle).font(.caption).frame(width: 48, alignment: .leading); GeometryReader { geo in ZStack(alignment: .leading) { Capsule().fill(Color.secondary.opacity(0.1)); Capsule().fill(coverageColor(muscle)).frame(width: geo.size.width * CGFloat(count) / CGFloat(maxValue)) } }.frame(height: 7); Text("\(count)").font(.caption2.monospacedDigit()).frame(width: 20) }
                }
                if !stats.coverage.untouched.isEmpty { Text("Not hit: \(stats.coverage.untouched.joined(separator: ", "))").font(.caption).foregroundStyle(Theme.carbs) }
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
    @State private var completed: Set<String> = []
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { SectionLabel(text: "Next in rotation"); Spacer(); if let next = plan.upcoming.first { Text(next.type).font(.caption.weight(.bold)).padding(.horizontal, 9).padding(.vertical, 5).background(Theme.accent.opacity(0.12), in: Capsule()).foregroundStyle(Theme.accent) } }
            if let next = plan.upcoming.first {
                ForEach(next.exercises) { exercise in Button { toggle(exercise.name) } label: { HStack { Image(systemName: completed.contains(exercise.name) ? "checkmark.circle.fill" : "circle").foregroundStyle(completed.contains(exercise.name) ? Theme.accent : .secondary); Text(exercise.name).strikethrough(completed.contains(exercise.name)).foregroundStyle(completed.contains(exercise.name) ? .secondary : .primary); Spacer() }.contentShape(Rectangle()) }.buttonStyle(.plain).padding(.vertical, 2) }
            } else { Text("No exercises tagged for the next rotation.").font(.subheadline).foregroundStyle(.secondary) }
            if !plan.core.isEmpty { Divider(); Text("CORE, ANY DAY").font(.caption2.weight(.bold)).tracking(1.2).foregroundStyle(.secondary); Text(plan.core.map(\.name).joined(separator: "  ·  ")).font(.caption).foregroundStyle(.secondary) }
        }.appCard()
    }
    private func toggle(_ name: String) { withAnimation(.spring(response: 0.25, dampingFraction: 0.7)) { if completed.contains(name) { completed.remove(name) } else { completed.insert(name) } } }
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

struct WorkoutLoggerView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var exercise: String; @State private var type: String; @State private var sets = [WorkoutSet(weight: 0, reps: 0)]; @State private var last: LastWorkoutPayload?; @State private var isSaving = false
    @FocusState private var inputFocused: Bool
    private let types = ["Push", "Pull", "Legs", "Abs", "Cardio", "Full Body", "Rest"]
    private var suggestions: [KnownExercise] { store.exercises.filter { $0.workoutType.contains(type) } }
    init(initialType: String = "Push", initialExercise: String = "") {
        _type = State(initialValue: CanonicalWorkoutType.value(for: initialType))
        _exercise = State(initialValue: initialExercise)
    }
    var body: some View {
        NavigationStack {
            ScrollView { VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 9) { SectionLabel(text: "Training type"); ScrollView(.horizontal, showsIndicators: false) { HStack { ForEach(types, id: \.self) { item in Button(item) { inputFocused = false; type = item; exercise = item == "Rest" ? "Rest Day" : ""; sets = [WorkoutSet(weight: 0, reps: 0)] }.buttonStyle(.borderedProminent).tint(type == item ? Theme.accent : Theme.input).foregroundStyle(type == item ? .white : Theme.sectionInk) } } } }
                if type != "Rest" {
                    VStack(alignment: .leading, spacing: 9) {
                        HStack { SectionLabel(text: "Exercise"); Spacer(); if let last, !last.sets.isEmpty { Button("Fill last time") { sets = Array(last.sets.prefix(4)) }.font(.caption.weight(.bold)) } }
                        TextField("Barbell bench press", text: $exercise).textInputAutocapitalization(.words).focused($inputFocused).padding(14).background(Theme.surface, in: RoundedRectangle(cornerRadius: 14)).onChange(of: exercise) { _, value in lookupLastWorkout(value) }.onDisappear { lastWorkoutTask?.cancel() }
                        if !suggestions.isEmpty { ScrollView(.horizontal, showsIndicators: false) { HStack { ForEach(suggestions) { item in Button(item.name) { inputFocused = false; exercise = item.name }.font(.caption).buttonStyle(.bordered) } } } }
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        HStack { SectionLabel(text: "Sets"); Spacer(); Button("Add set", systemImage: "plus") { if sets.count < 4 { sets.append(WorkoutSet(weight: 0, reps: 0)) } }.font(.caption.weight(.bold)).disabled(sets.count == 4) }
                        HStack { Text("SET").frame(width: 30); Text("WEIGHT").frame(maxWidth: .infinity); Text("REPS").frame(maxWidth: .infinity); Color.clear.frame(width: 28) }.font(.caption2.weight(.bold)).foregroundStyle(.secondary)
                        ForEach(sets.indices, id: \.self) { index in HStack { Text("\(index + 1)").font(.caption.monospacedDigit()).frame(width: 30); TextField("lb", value: $sets[index].weight, format: .number).keyboardType(.decimalPad).focused($inputFocused).multilineTextAlignment(.center).padding(12).background(Theme.surface, in: RoundedRectangle(cornerRadius: 12)); TextField("reps", value: $sets[index].reps, format: .number).keyboardType(.numberPad).focused($inputFocused).multilineTextAlignment(.center).padding(12).background(Theme.surface, in: RoundedRectangle(cornerRadius: 12)); Button(role: .destructive) { inputFocused = false; if sets.count > 1 { sets.remove(at: index) } } label: { Image(systemName: "minus.circle").frame(width: 44, height: 44) }.disabled(sets.count == 1) } }
                    }
                }
                Button { submit() } label: { if isSaving { ProgressView().tint(.white) } else { Text(type == "Rest" ? "Log rest day" : "Log exercise").fontWeight(.bold) } }.frame(maxWidth: .infinity).frame(height: 52).background(Theme.accent, in: RoundedRectangle(cornerRadius: 16)).foregroundStyle(.white).disabled(isSaving || (type != "Rest" && (exercise.trimmingCharacters(in: .whitespaces).isEmpty || !sets.contains { $0.weight > 0 || $0.reps > 0 }))).opacity(isSaving ? 0.6 : 1)
            }.padding(16) }
            .scrollDismissesKeyboard(.interactively)
            .background(Theme.canvas)
            .navigationTitle("Log workout")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { inputFocused = false }.fontWeight(.semibold)
                }
            }
        }
        .presentationDetents([.large])
        .task {
            if !exercise.isEmpty { lookupLastWorkout(exercise) }
        }
    }
    @State private var lastWorkoutTask: Task<Void, Never>?
    private func lookupLastWorkout(_ value: String) {
        lastWorkoutTask?.cancel()
        guard value.count > 2 else { last = nil; return }
        let query = value
        lastWorkoutTask = Task { @MainActor in
            let result = await store.lastWorkout(query)
            guard !Task.isCancelled, exercise == query else { return }
            last = result
        }
    }
    private func submit() { inputFocused = false; Task { isSaving = true; let valid = type == "Rest" ? [] : sets.filter { $0.weight > 0 || $0.reps > 0 }; if await store.logWorkout(exercise: type == "Rest" ? "Rest Day" : exercise, sets: valid, type: type) { dismiss() }; isSaving = false } }
}
