import SwiftUI
import A2UISwiftCore
import A2UISwiftUI

/// The only production file importing A2UI. Network payloads use our closed
/// Codable contract; the package only sees app-constructed v0.9 message trees.
@MainActor
struct AgentSurfaceRenderer: View {
    let surface: AgentSurface
    @ObservedObject var canvas: AgentSurfaceStore
    @State private var model: SurfaceViewModel?
    @State private var rejected = false

    var body: some View {
        Group {
            if let model, !rejected {
                A2UISurfaceView(viewModel: model, catalog: NativeCatalog(surface: surface, canvas: canvas), scrolls: false)
            } else if rejected {
                AgentCanvasFallback(canvas: canvas, message: "This task could not be displayed.")
            } else {
                ProgressView().accessibilityLabel("Preparing task")
            }
        }
        .task(id: canvas.envelope?.revision) {
            do {
                model = try Self.makeModel(surface)
                rejected = false
            } catch {
                rejected = true
                canvas.fail(AgentCanvasError.invalidPayload)
            }
        }
    }

    static func makeModel(_ surface: AgentSurface) throws -> SurfaceViewModel {
        // Revalidate even app-constructed surfaces before crossing the adapter.
        let probe = AgentCanvasEnvelope(protocolVersion: AgentCanvasEnvelope.version,
            catalog: AgentCanvasEnvelope.catalogId, sessionId: "00000000-0000-4000-8000-000000000001",
            instanceId: "00000000-0000-4000-8000-000000000002",
            revision: 0, serverTime: 0, surfaces: [surface], approval: surface.lifecycle == .approval
                ? AgentApproval(id: "validation", title: "", detail: "", status: .pending) : nil)
        try probe.validate()
        let catalog = Catalog(id: AgentCanvasEnvelope.catalogId,
            componentNames: Set(AgentComponentKind.allCases.map(\.rawValue)).union(["Column"]))
        let vm = SurfaceViewModel(catalog: catalog)
        let children = surface.components.map { AnyCodable.string($0.id) }
        let root = RawComponent(id: "root", component: "Column", properties: ["children": .array(children)])
        // No executable expressions, network URLs, arbitrary action dictionaries,
        // nutrition values, or user-controlled styles are passed to the renderer.
        let components = [root] + surface.components.map { RawComponent(id: $0.id, component: $0.component.rawValue) }
        try vm.processMessage(.createSurface(CreateSurfacePayload(surfaceId: surface.surfaceId, catalogId: AgentCanvasEnvelope.catalogId)))
        try vm.processMessage(.updateComponents(UpdateComponentsPayload(surfaceId: surface.surfaceId, components: components)))
        guard vm.componentTree?.children.count == surface.components.count else { throw AgentCanvasError.invalidPayload }
        return vm
    }

    private struct NativeCatalog: CustomComponentCatalog {
        let surface: AgentSurface
        let canvas: AgentSurfaceStore
        @ViewBuilder
        func build(typeName: String, node: ComponentNode, surface model: SurfaceModel) -> some View {
            if let item = surface.components.first(where: { $0.id == node.baseComponentId && $0.component.rawValue == typeName }) {
                AgentNativeComponent(item: item, canvas: canvas).padding(.vertical, 5)
            } else {
                AgentCanvasFallback(canvas: canvas, message: "A task component was rejected.")
            }
        }
    }
}

@MainActor
private struct AgentNativeComponent: View {
    let item: AgentComponent
    @ObservedObject var canvas: AgentSurfaceStore
    @EnvironmentObject private var app: AppStore
    @State private var showsSwap = false
    @State private var replacement = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let title = item.title { SectionLabel(text: title) }
            switch item.component {
            case .setupChecklist:
                Label("Setup progress", systemImage: "checklist").font(.headline)
                structuredRows
                if item.id != "setup-preview" {
                    Button("Refresh setup") { act(.init(action: .showSetup)) }
                }
            case .profileMetrics:
                Text(item.text ?? "Share your name with the coach and enter your measurements.").font(.subheadline)
                AgentSetupNumberForm(fields: [("height_cm", "Height (cm)"), ("weight_kg", "Weight (kg)"), ("goal_weight_kg", "Goal weight (kg)")], busy: canvas.busy || canvas.envelope?.approval != nil) { values in
                    act(.init(action: .submitMetrics, numbers: values))
                }
            case .targetStatus:
                structuredRows
                if let text = item.text { Text(text).font(.caption) }
                if item.id != "setup-preview" {
                    AgentSetupNumberForm(fields: [("calories", "Calories"), ("protein", "Protein (g)"), ("carbs", "Carbs (g)"), ("fat", "Fat (g)"), ("fiber", "Fiber (g)")], busy: canvas.busy || canvas.envelope?.approval != nil) { values in
                        act(.init(action: .submitTargets, numbers: values))
                    }
                }
            case .workoutPlanPreview:
                Label(item.id == "setup-preview" ? "Sample / proposed routine, not saved" : "Workout plan", systemImage: "dumbbell").font(.headline)
                structuredRows
                if let text = item.text { Text(text).font(.subheadline) }
                if item.id != "setup-preview" {
                    Button("Preview workout") { act(.init(action: .previewPlan)) }
                    if item.actions.contains(where: { $0.action == .startWorkout }) {
                        Button("Start workout") { act(.init(action: .startWorkout)) }
                    }
                }
            case .dailyStatus:
                Text("Daily status belongs to the permanent header.")
            case .macroProgress:
                AgentMacroProgress()
            case .mealReceipt:
                Label(canvas.envelope?.receipt?.label ?? "Meal saved", systemImage: "checkmark.circle.fill")
                    .font(.headline).foregroundStyle(Theme.accent)
                Text(item.text ?? "Your meal was verified by the server.").font(.subheadline)
                Button("View meals") { canvas.showsMeals = true }
            case .foodClarification:
                Label("One more detail", systemImage: "questionmark.bubble").font(.headline)
                Text(item.text ?? "Add the missing portion or food detail in the composer.")
            case .workoutOverview:
                workoutOverview
            case .activeExercise:
                if let exercise = canvas.envelope?.workout?.activeExercise {
                    SectionLabel(text: "Active exercise")
                    Text(exercise.name).font(.title2.bold())
                    Text("\(exercise.sets) sets · \(exercise.reps) reps").foregroundStyle(Theme.sectionInk)
                    Text("\(canvas.envelope?.workout?.loggedSets[exercise.id] ?? 0) sets verified this session")
                        .font(.caption).foregroundStyle(Theme.sectionInk)
                    HStack {
                        Button("Next exercise", systemImage: "arrow.right") { act(.init(action: .nextExercise)) }
                        Spacer()
                        Button("Swap", systemImage: "arrow.triangle.2.circlepath") {
                            showsSwap = true
                        }
                    }.font(.subheadline)
                } else { Text("Choose an exercise from your workout.") }
            case .setLogger:
                Text("Use your usual set rows, suggestions and Fill Last Time.").font(.subheadline)
                Button("Complete set", systemImage: "plus.circle.fill") {
                    act(.init(action: .openSetLogger, reference: item.reference))
                }.buttonStyle(.borderedProminent)
                    .accessibilityHint("Opens the established logger. Nothing is saved until you log the exercise.")
            case .restTimer:
                if canvas.envelope?.workout?.restEndsAt != nil {
                    TimelineView(.periodic(from: .now, by: 1)) { _ in
                        let remaining = canvas.state.restRemaining()
                        HStack {
                            Label("Rest", systemImage: "timer")
                            Spacer()
                            if remaining > 0 {
                                Text("\(Int(ceil(remaining))) seconds").monospacedDigit()
                            } else { Text("Ready for your next set").font(.subheadline) }
                        }.accessibilityElement(children: .combine)
                    }
                } else { Label("Rest timer starts after a verified log", systemImage: "timer").font(.caption) }
            case .weeklyTrend:
                AgentWeeklyTrend()
            case .confirmationCard:
                if let approval = canvas.envelope?.approval, approval.id == item.reference {
                    Label(approval.title, systemImage: "checkmark.shield").font(.headline)
                    Text(approval.detail).font(.subheadline)
                    if approval.status == .uncertain {
                        Text("The save could not be verified. Check your plan before making another change.")
                            .foregroundStyle(Theme.danger)
                        Button("View workout plan") { canvas.showsWorkouts = true }
                    }
                    HStack {
                        Button("Confirm") { act(.init(action: .confirm, reference: approval.id)) }
                            .buttonStyle(.borderedProminent)
                            .disabled(approval.status != .pending || canvas.errorMessage != nil)
                        Button("Cancel", role: .cancel) { act(.init(action: .cancel, reference: approval.id)) }
                            .buttonStyle(.bordered)
                    }
                } else { Text("This approval is no longer active.") }
            case .agentMessage:
                Text(item.text ?? "Ask for a meal, workout or progress view.").font(.subheadline)
            }
        }
        .foregroundStyle(Theme.ink)
        .tint(Theme.accent)
        .frame(maxWidth: .infinity, alignment: .leading)
        .appCard()
        .disabled(canvas.busy)
        .alert("Replace active exercise", isPresented: $showsSwap) {
            TextField("Replacement exercise", text: $replacement)
            Button("Preview replacement") {
                Task { await canvas.requestSwap(replacement) }
            }.disabled(replacement.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || replacement.count > 160)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Enter an exercise name. Your plan changes only after you confirm the preview. Your message draft stays untouched.")
        }
    }

    @ViewBuilder private var workoutOverview: some View {
        if let workout = canvas.envelope?.workout {
            Label(workout.type, systemImage: "dumbbell").font(.headline)
            Text(workout.done ? "Today's session is marked complete." : "Today's assigned session")
                .font(.subheadline).foregroundStyle(Theme.sectionInk)
            ForEach(workout.exercises) { exercise in
                Button { act(.init(action: .selectExercise, reference: exercise.id)) } label: {
                    HStack(alignment: .top) {
                        Image(systemName: workout.activeExerciseId == exercise.id ? "circle.inset.filled" : "circle")
                        Text(exercise.name).multilineTextAlignment(.leading)
                        Spacer(minLength: 4)
                        Text("\(exercise.sets) × \(exercise.reps)").font(.caption)
                    }.padding(.vertical, 6)
                }.disabled(workout.done)
            }
            Button("Full workout plan and swaps") { canvas.showsWorkouts = true }
            if !workout.done {
                Button("Finish workout", systemImage: "checkmark.circle") { act(.init(action: .completeWorkout)) }
                    .disabled(canvas.errorMessage != nil)
            }
        } else {
            Label("Workout", systemImage: "dumbbell").font(.headline)
            Text(item.text ?? "No assigned session is available. Your existing logger still works.")
            Button("Open workouts") { canvas.showsWorkouts = true }
        }
    }
    private func act(_ intent: AgentIntent) { Task { await canvas.perform(intent) } }
    private var structuredRows: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(item.rows.enumerated()), id: \.offset) { _, row in
                HStack(alignment: .top) {
                    Text(row.label).font(.subheadline.weight(.semibold))
                    Spacer()
                    Text(row.detail).font(.caption).foregroundStyle(Theme.sectionInk)
                }.accessibilityElement(children: .combine)
            }
        }
    }
}

/// Fixed app-owned inputs. Values are proposals sent to a closed server action;
/// the returned preview still requires native Confirm before persistence.
@MainActor
private struct AgentSetupNumberForm: View {
    let fields: [(String, String)]
    let busy: Bool
    let submit: ([String: Double]) -> Void
    @State private var values: [String: String] = [:]
    private var parsed: [String: Double] {
        values.compactMapValues { Double($0) }
    }
    var body: some View {
        VStack(spacing: 10) {
            ForEach(fields, id: \.0) { key, label in
                HStack {
                    Text(label).font(.subheadline)
                    TextField(label, text: Binding(get: { values[key] ?? "" }, set: { values[key] = $0 }))
                        .keyboardType(.decimalPad).multilineTextAlignment(.trailing)
                        .textFieldStyle(.roundedBorder)
                }
            }
            Button("Review before saving") { submit(parsed) }
                .buttonStyle(.borderedProminent)
                .disabled(busy || parsed.count != fields.count || !parsed.values.allSatisfy({ $0.isFinite && $0 >= 0 }))
        }
    }
}
