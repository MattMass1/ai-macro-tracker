import SwiftUI

/// Today's workout, from the plan to the next set (design package v5).
/// Review the session and previous loads first; then one exercise at a time
/// with load / reps / done rows, a rest timer and Finish workout. Nothing is
/// logged until Finish is confirmed, and each exercise is saved once.
struct WorkoutSessionView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    let session: WorkoutPlanPayload.PlannedDay
    let date: String
    let onAskCoach: (String) -> Void

    @State private var draft: WorkoutSessionDraft
    @State private var isActive = false
    @State private var exerciseIndex = 0
    @State private var restTimer: RestTimer?
    @State private var swapTarget: String?
    @State private var showFinishConfirm = false
    @State private var isSaving = false
    @State private var setHint: String?
    @FocusState private var inputFocused: Bool

    init(session: WorkoutPlanPayload.PlannedDay, date: String, onAskCoach: @escaping (String) -> Void) {
        self.session = session
        self.date = date
        self.onAskCoach = onAskCoach
        _draft = State(initialValue: WorkoutSessionDraft(type: session.type, exerciseNames: session.exercises.map(\.name)))
    }

    private var current: SessionExercise? { draft.exercises.indices.contains(exerciseIndex) ? draft.exercises[exerciseIndex] : nil }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        title
                        if isActive { activeExercise } else { review }
                    }
                    .padding(16)
                    .padding(.bottom, 24)
                }
                .scrollDismissesKeyboard(.interactively)
                footer
            }
            .background(Theme.canvas)
            .navigationTitle("Today's workout")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button(isActive ? "Close" : "Cancel") { dismiss() } }
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { inputFocused = false }.fontWeight(.semibold)
                }
            }
        }
        .presentationDetents([.large])
        .interactiveDismissDisabled(isActive && draft.completedSetCount > 0)
        .task { await loadHistory() }
        .sheet(item: Binding(get: { swapTarget.map(SwapRequest.init) }, set: { swapTarget = $0?.name })) { request in
            ExerciseLibraryView { selection in
                draft.swap(exercise: request.name, with: selection.exercise)
                swapTarget = nil
                Task { await loadHistory(for: selection.exercise) }
            }
        }
        .confirmationDialog(finishTitle, isPresented: $showFinishConfirm, titleVisibility: .visible) {
            Button("Log \(draft.loggable.count == 1 ? "1 exercise" : "\(draft.loggable.count) exercises")") { Task { await finish() } }
            Button("Keep going", role: .cancel) {}
        } message: {
            Text(finishMessage)
        }
    }

    private struct SwapRequest: Identifiable { let name: String; var id: String { name } }

    // MARK: - Review

    private var title: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(session.type).font(.system(size: 32, weight: .bold)).foregroundStyle(Theme.ink)
            Text("\(dateLabel) · \(draft.exercises.count) exercises · \(draft.plannedSetCount) planned sets")
                .font(.subheadline).foregroundStyle(Theme.muted)
        }
    }

    private var review: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let hint = draft.pickUpHint {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Pick up where you left off.").font(.subheadline.weight(.bold)).foregroundStyle(Theme.accent)
                    Text("Your last \(hint.exercise) top set was \(setLabel(hint.set)). Review the plan before you start.")
                        .font(.subheadline).foregroundStyle(Theme.sectionInk)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14)
                .background(Theme.accentTint, in: RoundedRectangle(cornerRadius: Theme.cardRadius))
            }
            HStack {
                Text("Your exercises").font(.headline).foregroundStyle(Theme.ink)
                Spacer()
                Text("\(draft.exercises.count) exercises").font(.caption).foregroundStyle(Theme.muted)
            }
            VStack(spacing: 0) {
                ForEach(Array(draft.exercises.enumerated()), id: \.element.id) { index, exercise in
                    Button {
                        exerciseIndex = index
                        isActive = true
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(exercise.name).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
                                Text("\(exercise.sets.count) sets").font(.caption).foregroundStyle(Theme.muted)
                                if let top = exercise.topLastSet {
                                    Text("Last: \(setLabel(top))").font(.caption).foregroundStyle(Theme.muted)
                                }
                            }
                            Spacer()
                            Image(systemName: "chevron.right").font(.caption.weight(.bold)).foregroundStyle(Theme.accent)
                        }
                        .padding(.vertical, 12)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    if index < draft.exercises.count - 1 { Divider().overlay(Theme.divider) }
                }
            }
            .padding(.horizontal, 14)
            .appCard(padding: 0)
        }
    }

    // MARK: - Active

    @ViewBuilder
    private var activeExercise: some View {
        if let exercise = current {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Text(exercise.name).font(.title2.weight(.bold)).foregroundStyle(Theme.ink)
                    Spacer()
                    Button("Swap") { swapTarget = exercise.name }
                        .font(.subheadline.weight(.bold)).foregroundStyle(Theme.accent)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("\(exercise.sets.count) working sets").font(.subheadline).foregroundStyle(Theme.sectionInk)
                    if let top = exercise.topLastSet {
                        Text("Last session: \(setLabel(top))").font(.subheadline).foregroundStyle(Theme.muted)
                    }
                }
                setTable(exercise)
                if let setHint {
                    Text(setHint).font(.caption).foregroundStyle(Theme.danger)
                }
                HStack {
                    Button("Add set", systemImage: "plus") { draft.addSet(to: exercise.name) }
                        .font(.caption.weight(.bold)).foregroundStyle(Theme.accent)
                        .disabled(exercise.sets.count >= WorkoutSessionDraft.maxSetCount)
                    Spacer()
                }
                restRow
                exercisePager
            }
            .onChange(of: exerciseIndex) { _, _ in setHint = nil }
        }
    }

    private func setTable(_ exercise: SessionExercise) -> some View {
        VStack(spacing: 0) {
            HStack {
                Text("Set").frame(width: 32, alignment: .leading)
                Text("Load · lb").frame(maxWidth: .infinity, alignment: .leading)
                Text("Reps").frame(maxWidth: .infinity, alignment: .leading)
                Text("Done").frame(width: 52)
            }
            .font(.caption.weight(.semibold)).foregroundStyle(Theme.muted)
            .padding(.vertical, 8)
            Divider().overlay(Theme.divider)
            ForEach(Array(exercise.sets.enumerated()), id: \.element.id) { index, set in
                HStack(spacing: 10) {
                    Text("\(index + 1)").font(.subheadline.monospacedDigit()).foregroundStyle(Theme.ink).frame(width: 32, alignment: .leading)
                    numberField("175", value: weightBinding(exercise.name, set.id), keyboard: .decimalPad, done: set.done)
                    numberField("8", value: repsBinding(exercise.name, set.id), keyboard: .numberPad, done: set.done)
                    Button {
                        inputFocused = false
                        if draft.toggle(set: set.id, in: exercise.name) {
                            setHint = nil
                            if draft.exercises[exerciseIndex].sets.first(where: { $0.id == set.id })?.done == true {
                                restTimer = RestTimer.start(seconds: WorkoutSessionDraft.defaultRestSeconds)
                            }
                        } else {
                            setHint = "Add a load or reps before marking the set done."
                        }
                    } label: {
                        Image(systemName: set.done ? "checkmark" : "circle")
                            .font(.subheadline.weight(.bold))
                            .foregroundStyle(set.done ? .white : Theme.accent)
                            .frame(width: 40, height: 40)
                            .background(set.done ? Theme.accent : Theme.paper, in: RoundedRectangle(cornerRadius: Theme.controlRadius))
                            .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).stroke(set.done ? Theme.accentDark : Theme.edgeStrong))
                    }
                    .frame(width: 52)
                    .accessibilityLabel(set.done ? "Set \(index + 1) done" : "Mark set \(index + 1) done")
                }
                .padding(.vertical, 8)
                .contextMenu {
                    Button("Remove set", systemImage: "minus.circle", role: .destructive) { draft.removeSet(set.id, from: exercise.name) }
                        .disabled(exercise.sets.count == 1)
                }
                if index < exercise.sets.count - 1 { Divider().overlay(Theme.divider) }
            }
        }
        .padding(.horizontal, 14)
        .appCard(padding: 0)
    }

    private func numberField<V>(_ placeholder: String, value: Binding<V?>, keyboard: UIKeyboardType, done: Bool) -> some View where V: Numeric & LosslessStringConvertible {
        TextField(placeholder, text: Binding(
            get: { value.wrappedValue.map { String($0) } ?? "" },
            set: { value.wrappedValue = V($0.replacingOccurrences(of: ",", with: ".")) }
        ))
        .keyboardType(keyboard)
        .focused($inputFocused)
        .font(.body.monospacedDigit())
        .foregroundStyle(Theme.ink)
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, minHeight: 42, alignment: .leading)
        .background(done ? Theme.accentTint : Theme.paper, in: RoundedRectangle(cornerRadius: Theme.controlRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).stroke(Theme.edgeStrong))
    }

    private func weightBinding(_ exercise: String, _ id: UUID) -> Binding<Double?> {
        Binding(
            get: { draft.exercises.first { $0.name == exercise }?.sets.first { $0.id == id }?.weight },
            set: { new in update(exercise, id) { $0.weight = new } }
        )
    }

    private func repsBinding(_ exercise: String, _ id: UUID) -> Binding<Int?> {
        Binding(
            get: { draft.exercises.first { $0.name == exercise }?.sets.first { $0.id == id }?.reps },
            set: { new in update(exercise, id) { $0.reps = new } }
        )
    }

    private func update(_ exercise: String, _ id: UUID, _ change: (inout SessionSet) -> Void) {
        guard let e = draft.exercises.firstIndex(where: { $0.name == exercise }),
              let s = draft.exercises[e].sets.firstIndex(where: { $0.id == id }) else { return }
        change(&draft.exercises[e].sets[s])
    }

    private var restRow: some View {
        HStack {
            if let restTimer {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    let remaining = restTimer.remaining(at: context.date)
                    HStack(spacing: 10) {
                        Text(remaining == 0 ? "Rest done" : "Rest \(RestTimer.label(seconds: remaining))")
                            .font(.subheadline.weight(.bold)).monospacedDigit()
                            .foregroundStyle(remaining == 0 ? Theme.accent : Theme.ink)
                        ProgressView(value: Double(restTimer.totalSeconds - remaining), total: Double(restTimer.totalSeconds))
                            .tint(Theme.accent)
                    }
                }
                Spacer()
                Button("Skip") { self.restTimer = nil }.buttonStyle(PixelSecondaryButtonStyle())
            } else {
                Text("Suggested rest: \(RestTimer.label(seconds: WorkoutSessionDraft.defaultRestSeconds))")
                    .font(.subheadline).foregroundStyle(Theme.muted)
                Spacer()
                Button("Start timer") { restTimer = RestTimer.start(seconds: WorkoutSessionDraft.defaultRestSeconds) }
                    .buttonStyle(PixelSecondaryButtonStyle())
            }
        }
        .padding(.vertical, 4)
    }

    private var exercisePager: some View {
        HStack {
            Button("Previous", systemImage: "chevron.left") { exerciseIndex = max(0, exerciseIndex - 1) }
                .disabled(exerciseIndex == 0)
            Spacer()
            Text("\(exerciseIndex + 1) of \(draft.exercises.count)").font(.caption.weight(.semibold)).foregroundStyle(Theme.muted)
            Spacer()
            Button("Next", systemImage: "chevron.right") { exerciseIndex = min(draft.exercises.count - 1, exerciseIndex + 1) }
                .labelStyle(.trailingIcon)
                .disabled(exerciseIndex >= draft.exercises.count - 1)
        }
        .font(.subheadline.weight(.bold)).foregroundStyle(Theme.accent)
        .padding(.top, 4)
    }

    // MARK: - Footer

    private var footer: some View {
        VStack(spacing: 10) {
            Text(isActive ? "\(draft.completedSetCount) of \(draft.plannedSetCount) planned sets done"
                          : "Review first. Nothing is logged until you confirm.")
                .font(.caption).foregroundStyle(Theme.muted)
            if isActive {
                Button {
                    inputFocused = false
                    showFinishConfirm = true
                } label: {
                    if isSaving { ProgressView().tint(.white) } else { Text("Finish workout") }
                }
                .buttonStyle(PixelPrimaryButtonStyle())
                .disabled(isSaving)
            } else {
                Button("Start workout") {
                    exerciseIndex = 0
                    isActive = true
                }
                .buttonStyle(PixelPrimaryButtonStyle())
                .disabled(draft.exercises.isEmpty)
            }
            Button {
                onAskCoach("Help with today's \(session.type) workout.")
            } label: {
                HStack(spacing: 8) {
                    Text("Ask Coach").font(.subheadline.weight(.bold)).foregroundStyle(Theme.sectionInk)
                    Text("Help with this workout").font(.subheadline).foregroundStyle(Theme.muted)
                    Spacer()
                    Image(systemName: "arrow.up").fontWeight(.bold).foregroundStyle(.white)
                        .frame(width: 36, height: 36)
                        .background(Theme.accent, in: RoundedRectangle(cornerRadius: Theme.controlRadius))
                }
                .padding(.leading, 12).padding(4)
                .background(Theme.paper, in: RoundedRectangle(cornerRadius: Theme.controlRadius))
                .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).stroke(Theme.edgeStrong, lineWidth: 2))
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
        .background(Theme.dock)
        .overlay(alignment: .top) { Rectangle().fill(Theme.accentEdge).frame(height: 3) }
    }

    private var finishTitle: String {
        draft.loggable.isEmpty ? "No sets to log yet" : "Finish and log this workout?"
    }

    private var finishMessage: String {
        guard !draft.loggable.isEmpty else { return "Mark at least one set done with a load or reps, then finish." }
        return draft.loggable.map { "\($0.exercise): \($0.sets.map(setLabel).joined(separator: ", "))" }.joined(separator: "\n")
    }

    // MARK: - Actions

    private func loadHistory() async {
        await withTaskGroup(of: (String, LastWorkoutPayload?).self) { group in
            for exercise in draft.exercises { group.addTask { await (exercise.name, store.lastWorkout(exercise.name)) } }
            for await (name, last) in group {
                guard let last else { continue }
                draft.applyLast(exercise: name, sets: last.sets, date: last.date)
            }
        }
    }

    private func loadHistory(for name: String) async {
        guard let last = await store.lastWorkout(name) else { return }
        draft.applyLast(exercise: name, sets: last.sets, date: last.date)
    }

    private func finish() async {
        let entries = draft.loggable
        guard !entries.isEmpty, !isSaving else { return }
        isSaving = true
        defer { isSaving = false }
        for entry in entries {
            guard await store.logWorkout(exercise: entry.exercise, sets: entry.sets, type: draft.type) else { return }
            draft.markLogged(entry.exercise)
        }
        var completed = WorkoutSessionCompletions.saved(date: date, type: session.type)
        for exercise in draft.exercises where exercise.logged { completed.insert(exercise.name) }
        WorkoutSessionCompletions.save(completed, date: date, type: session.type)
        dismiss()
    }

    private func setLabel(_ set: WorkoutSet) -> String {
        set.weight > 0 ? "\(set.weight.formatted()) lb × \(set.reps)" : "\(set.reps) reps"
    }

    private var dateLabel: String {
        guard let parsed = Self.dateFormatter.date(from: date) else { return date }
        return parsed.formatted(.dateTime.weekday(.wide).month(.wide).day())
    }

    private static let dateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()
}

private struct TrailingIconLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 4) { configuration.title; configuration.icon }
    }
}

private extension LabelStyle where Self == TrailingIconLabelStyle {
    static var trailingIcon: TrailingIconLabelStyle { TrailingIconLabelStyle() }
}
