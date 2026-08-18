import SwiftUI

struct WorkoutsView: View {
    @EnvironmentObject private var store: AppStore
    @State private var showLogger = false
    var body: some View {
        ZStack(alignment: .bottomTrailing) { Theme.canvas.ignoresSafeArea()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 22) {
                    DayPicker(date: store.selectedDate, canGoForward: !store.isToday) { delta in Task { await store.moveDay(by: delta) } }
                    if store.isLoadingWorkouts && store.stats == nil { workoutSkeleton }
                    if let stats = store.stats { WorkoutDashboard(stats: stats) }
                    if let plan = store.plan { WorkoutPlanCard(plan: plan) }
                    WorkoutHistory(workouts: store.workouts) { id in Task { await store.deleteWorkout(id) } }
                }.padding(.horizontal, 16).padding(.bottom, 96)
            }.refreshable { await store.loadWorkoutData() }
            Button { showLogger = true } label: { Label("Log workout", systemImage: "plus").font(.subheadline.weight(.bold)).padding(.horizontal, 18).frame(height: 52).background(Theme.accent, in: Capsule()).foregroundStyle(.white).shadow(color: .black.opacity(0.18), radius: 16, y: 7) }.padding(18)
        }.navigationTitle("Workouts").sheet(isPresented: $showLogger) { WorkoutLoggerView() }
    }
    private var workoutSkeleton: some View { VStack(spacing: 10) { HStack { RoundedRectangle(cornerRadius: 20).frame(height: 130); RoundedRectangle(cornerRadius: 20).frame(height: 130) }; RoundedRectangle(cornerRadius: 20).frame(height: 160) }.foregroundStyle(Theme.surface).redacted(reason: .placeholder).shimmering() }
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
                if !stats.coverage.untouched.isEmpty { Text("Not hit: \(stats.coverage.untouched.joined(separator: ", "))").font(.caption).foregroundStyle(Theme.calories) }
            }.appCard()
            if !stats.prs.isEmpty {
                VStack(alignment: .leading, spacing: 10) { SectionLabel(text: "Personal records"); ScrollView(.horizontal, showsIndicators: false) { HStack { ForEach(stats.prs.prefix(6)) { pr in VStack(alignment: .leading, spacing: 3) { Image(systemName: "trophy.fill").foregroundStyle(Theme.calories); Text(pr.exercise).font(.caption.weight(.semibold)).lineLimit(1); Text("\(pr.maxWeight.formatted()) lb").font(.caption2.monospacedDigit()).foregroundStyle(.secondary) }.frame(width: 118, alignment: .leading).padding(12).background(Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 15)) } } } }.appCard()
            }
        }
    }
    private func coverageColor(_ value: String) -> Color { value == "Push" ? Theme.calories : value == "Pull" ? Theme.carbs : value == "Abs" ? Theme.fat : Theme.protein }
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

private struct WorkoutLoggerView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var exercise = ""; @State private var type = "Push"; @State private var sets = [WorkoutSet(weight: 0, reps: 0)]; @State private var last: LastWorkoutPayload?; @State private var isSaving = false
    private let types = ["Push", "Pull", "Legs", "Abs", "Cardio", "Full Body", "Rest"]
    private var suggestions: [KnownExercise] { store.exercises.filter { $0.workoutType.contains(type) } }
    var body: some View {
        NavigationStack {
            ScrollView { VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 9) { SectionLabel(text: "Training type"); ScrollView(.horizontal, showsIndicators: false) { HStack { ForEach(types, id: \.self) { item in Button(item) { type = item; exercise = item == "Rest" ? "Rest Day" : ""; sets = [WorkoutSet(weight: 0, reps: 0)] }.buttonStyle(.borderedProminent).tint(type == item ? Theme.accent : Color.secondary.opacity(0.2)).foregroundStyle(type == item ? .white : .primary) } } } }
                if type != "Rest" {
                    VStack(alignment: .leading, spacing: 9) {
                        HStack { SectionLabel(text: "Exercise"); Spacer(); if let last, !last.sets.isEmpty { Button("Fill last time") { sets = Array(last.sets.prefix(4)) }.font(.caption.weight(.bold)) } }
                        TextField("Barbell bench press", text: $exercise).textInputAutocapitalization(.words).padding(14).background(Theme.surface, in: RoundedRectangle(cornerRadius: 14)).onChange(of: exercise) { _, value in lookupLastWorkout(value) }.onDisappear { lastWorkoutTask?.cancel() }
                        if !suggestions.isEmpty { ScrollView(.horizontal, showsIndicators: false) { HStack { ForEach(suggestions) { item in Button(item.name) { exercise = item.name }.font(.caption).buttonStyle(.bordered) } } } }
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        HStack { SectionLabel(text: "Sets"); Spacer(); Button("Add set", systemImage: "plus") { if sets.count < 4 { sets.append(WorkoutSet(weight: 0, reps: 0)) } }.font(.caption.weight(.bold)).disabled(sets.count == 4) }
                        HStack { Text("SET").frame(width: 30); Text("WEIGHT").frame(maxWidth: .infinity); Text("REPS").frame(maxWidth: .infinity); Color.clear.frame(width: 28) }.font(.caption2.weight(.bold)).foregroundStyle(.secondary)
                        ForEach(sets.indices, id: \.self) { index in HStack { Text("\(index + 1)").font(.caption.monospacedDigit()).frame(width: 30); TextField("lb", value: $sets[index].weight, format: .number).keyboardType(.decimalPad).multilineTextAlignment(.center).padding(12).background(Theme.surface, in: RoundedRectangle(cornerRadius: 12)); TextField("reps", value: $sets[index].reps, format: .number).keyboardType(.numberPad).multilineTextAlignment(.center).padding(12).background(Theme.surface, in: RoundedRectangle(cornerRadius: 12)); Button(role: .destructive) { if sets.count > 1 { sets.remove(at: index) } } label: { Image(systemName: "minus.circle") }.frame(width: 28).disabled(sets.count == 1) } }
                    }
                }
                Button { submit() } label: { if isSaving { ProgressView().tint(.white) } else { Text(type == "Rest" ? "Log rest day" : "Log exercise").fontWeight(.bold) } }.frame(maxWidth: .infinity).frame(height: 52).background(Theme.accent, in: RoundedRectangle(cornerRadius: 16)).foregroundStyle(.white).disabled(isSaving || (type != "Rest" && (exercise.trimmingCharacters(in: .whitespaces).isEmpty || !sets.contains { $0.weight > 0 || $0.reps > 0 }))).opacity(isSaving ? 0.6 : 1)
            }.padding(16) }.background(Theme.canvas).navigationTitle("Log workout").navigationBarTitleDisplayMode(.inline).toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }.presentationDetents([.large])
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
    private func submit() { Task { isSaving = true; let valid = type == "Rest" ? [] : sets.filter { $0.weight > 0 || $0.reps > 0 }; if await store.logWorkout(exercise: type == "Rest" ? "Rest Day" : exercise, sets: valid, type: type) { dismiss() }; isSaving = false } }
}
