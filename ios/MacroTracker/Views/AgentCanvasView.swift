import SwiftUI
import Charts
import UIKit

@MainActor
struct AgentCanvasView: View {
    @EnvironmentObject private var app: AppStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var canvas: AgentSurfaceStore
    @State private var legacyDestination = 2
    @State private var confirmNewSession = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    AgentDailyStatus(canvas: canvas)
                    if let notice = canvas.recoveryNotice {
                        Text(notice).font(.subheadline).foregroundStyle(Theme.sectionInk)
                    }
                    if let error = canvas.errorMessage {
                        AgentCanvasFallback(canvas: canvas, message: error)
                        Button("Start a fresh agent session") { confirmNewSession = true }.font(.subheadline)
                    }
                    if canvas.state.visibleSurfaces().isEmpty {
                        quietView
                    } else {
                        ForEach(canvas.state.visibleSurfaces()) { surface in
                            VStack(alignment: .trailing, spacing: 0) {
                                if surface.lifecycle != .approval {
                                    Button("Dismiss task", systemImage: "xmark") {
                                        Task { await canvas.perform(.init(action: .dismiss, surfaceId: surface.surfaceId)) }
                                    }.labelStyle(.iconOnly).frame(width: 44, height: 44)
                                        .disabled(canvas.busy)
                                }
                                AgentSurfaceRenderer(surface: surface, canvas: canvas)
                            }
                        }
                    }
                }.padding(16)
            }
            .scrollDismissesKeyboard(.interactively)
            .refreshable { await app.loadAll(); await canvas.refresh() }
            .background(Theme.canvas)
            .safeAreaInset(edge: .bottom, spacing: 0) {
                AgentComposer(canvas: canvas, voice: canvas.voice)
            }
            .navigationTitle("MMacros")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button("Meals", systemImage: "fork.knife") { canvas.showsMeals = true }
                        Button("Workouts", systemImage: "dumbbell") { canvas.showsWorkouts = true }
                        Button("Progress", systemImage: "chart.line.uptrend.xyaxis") { canvas.showsProgress = true }
                        Button("Standard coach", systemImage: "bubble.left.and.bubble.right") { canvas.showsLegacyCoach = true }
                        Button("Quiet view", systemImage: "rectangle") { Task { await canvas.perform(.init(action: .quiet)) } }
                    } label: { Image(systemName: "ellipsis.circle").frame(width: 44, height: 44) }
                        .accessibilityLabel("Standard screens and canvas controls")
                }
            }
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.2), value: canvas.envelope?.surfaces.map(\.surfaceId))
        }
        .task {
            canvas.attach(app)
            await canvas.refresh()
        }
        .onDisappear { Task { await canvas.voice.end() } }
        .onChange(of: canvas.showsLegacyCoach) { _, shown in
            if shown { Task { await canvas.voice.end() } }
        }
        .sheet(item: $canvas.logger) { selection in
            WorkoutLoggerView(initialType: selection.type, initialExercise: selection.exercise) { row in
                Task { await canvas.acknowledgeWorkout(row.id) }
            }.modifier(AgentToastModifier()).environmentObject(app)
        }
        .sheet(isPresented: $canvas.showsWorkouts, onDismiss: { Task { await canvas.perform(.init(action: .refresh)); await app.loadWorkoutData() } }) {
            NavigationStack { WorkoutsView() }
                .modifier(AgentToastModifier())
                .safeAreaInset(edge: .top) { closeButton { canvas.showsWorkouts = false } }
                .environmentObject(app)
        }
        .sheet(isPresented: $canvas.showsMeals, onDismiss: { canvas.completeLegacyNavigation() }) {
            NavigationStack { TodayView(selectedTab: $legacyDestination) }
                .modifier(AgentToastModifier())
                .safeAreaInset(edge: .top) { closeButton { canvas.showsMeals = false } }
                .environmentObject(app)
        }
        .sheet(isPresented: $canvas.showsProgress) {
            NavigationStack { ProgressDashboardView() }
                .modifier(AgentToastModifier())
                .safeAreaInset(edge: .top) { closeButton { canvas.showsProgress = false } }
                .environmentObject(app)
        }
        .sheet(isPresented: $canvas.showsLegacyCoach) {
            NavigationStack { ChatLogView() }
                .modifier(AgentToastModifier())
                .safeAreaInset(edge: .top) { closeButton { canvas.showsLegacyCoach = false } }
                .environmentObject(app)
        }
        .onChange(of: legacyDestination) { _, destination in
            legacyDestination = canvas.handleLegacyDestination(destination)
        }
        .confirmationDialog("Start a fresh session?", isPresented: $confirmNewSession, titleVisibility: .visible) {
            Button("Start fresh") { Task { await canvas.startFreshSession() } }
        } message: {
            Text("Pending previews will not be applied. Saved meals and workouts are unchanged. Check them before repeating a request with an uncertain result.")
        }
    }

    private func closeButton(_ action: @escaping () -> Void) -> some View {
        HStack { Spacer(); Button("Done", action: action).padding(12) }
            .background(.ultraThinMaterial)
    }

    private var quietView: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("What would help right now?").font(.title2.weight(.semibold)).foregroundStyle(Theme.ink)
            Text("Ask about your day, log a meal, or start your workout.")
                .font(.subheadline).foregroundStyle(Theme.sectionInk)
            ViewThatFits(in: .horizontal) {
                HStack { quickActions }
                VStack(alignment: .leading) { quickActions }
            }
        }.padding(.vertical, 24)
    }
    @ViewBuilder private var quickActions: some View {
        Button("Macros", systemImage: "chart.bar") { Task { await canvas.perform(.init(action: .showMacros)) } }
        Button("Workout", systemImage: "dumbbell") { Task { await canvas.perform(.init(action: .startWorkout)) } }
        Button("Week", systemImage: "chart.line.uptrend.xyaxis") { Task { await canvas.perform(.init(action: .showProgress)) } }
    }
}

/// Present in each sheet as well as the shell: a root overlay sits behind a
/// presented sheet and would hide confirmations from the legacy write paths.
@MainActor
struct AgentToastModifier: ViewModifier {
    @EnvironmentObject private var app: AppStore
    func body(content: Content) -> some View {
        content.overlay(alignment: .top) {
            if let toast = app.toast {
                Label(toast, systemImage: "checkmark.circle.fill")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.ink)
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .background(Theme.surface, in: Capsule())
                    .shadow(color: Theme.ink.opacity(0.12), radius: 16, y: 6)
                    .padding(8)
                    .accessibilityAddTraits(.updatesFrequently)
                    .allowsHitTesting(false)
            }
        }
    }
}

@MainActor
struct AgentDailyStatus: View {
    @EnvironmentObject private var app: AppStore
    @ObservedObject var canvas: AgentSurfaceStore
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button { Task { await app.moveDay(by: -1) } } label: { Image(systemName: "chevron.left").frame(width: 44, height: 44) }
                    .accessibilityLabel("Previous day")
                Text(app.day?.dayLabel ?? app.dateString).font(.caption.weight(.bold)).frame(maxWidth: .infinity)
                Button { Task { await app.moveDay(by: 1) } } label: { Image(systemName: "chevron.right").frame(width: 44, height: 44) }
                    .disabled(app.isToday).accessibilityLabel("Next day")
            }
            if let day = app.day {
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .firstTextBaseline) { calorieText(day); Spacer(); workoutText }
                    VStack(alignment: .leading, spacing: 8) { calorieText(day); workoutText }
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 105), alignment: .leading)], alignment: .leading, spacing: 8) {
                    smallMacro("Protein", day.totals.protein, Theme.protein)
                    smallMacro("Carbs", day.totals.carbs, Theme.carbs)
                    smallMacro("Fat", day.totals.fat, Theme.fat)
                    smallMacro("Fiber", day.totals.fiber, Theme.fiber)
                }
            } else if app.isLoadingDay { ProgressView("Loading daily status") }
            else { Text("Daily status is unavailable. Pull to refresh or open your standard screens.").font(.subheadline) }
        }.foregroundStyle(Theme.ink).appCard()
    }
    private func calorieText(_ day: DayPayload) -> some View {
        (Text(day.totals.calories.formatted(.number.precision(.fractionLength(0)))).font(.title2.weight(.heavy))
         + Text(" / \(day.targets.calories.formatted(.number.precision(.fractionLength(0)))) kcal").font(.caption))
            .accessibilityLabel("\(day.totals.calories.formatted()) of \(day.targets.calories.formatted()) calories")
    }
    private var workoutText: some View {
        Label(workoutSummary, systemImage: "dumbbell")
            .font(.caption.weight(.semibold)).foregroundStyle(Theme.accent)
    }
    private var workoutSummary: String {
        guard app.isToday else { return "\(app.workouts.count) workout entries" }
        if let workout = canvas.envelope?.workout, workout.date == app.day?.date {
            return "\(workout.type) · \(workout.done ? "complete" : "active")"
        }
        if let type = app.plan?.upcoming.first?.type { return "\(type) · planned" }
        return "Workout unavailable"
    }
    private func smallMacro(_ name: String, _ value: Double, _ color: Color) -> some View {
        Text("\(name) \(value.formatted(.number.precision(.fractionLength(0))))g")
            .font(.caption).foregroundStyle(color).accessibilityLabel("\(name), \(value.formatted()) grams")
    }
}

struct AgentMacroProgress: View {
    @EnvironmentObject private var app: AppStore
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionLabel(text: "Remaining · \(app.day?.dayLabel ?? app.dateString)")
            if let day = app.day {
                row("Calories", day.totals.calories, day.targets.calories, day.remaining.calories, "kcal", Theme.calories)
                row("Protein", day.totals.protein, day.targets.protein, day.remaining.protein, "g", Theme.protein)
                row("Carbs", day.totals.carbs, day.targets.carbs, day.remaining.carbs, "g", Theme.carbs)
                row("Fat", day.totals.fat, day.targets.fat, day.remaining.fat, "g", Theme.fat)
                row("Fiber", day.totals.fiber, day.targets.fiber, day.remaining.fiber, "g", Theme.fiber)
            } else { Text("Your macro data is unavailable. No totals have been estimated.") }
        }
    }
    private func row(_ name: String, _ value: Double, _ target: Double, _ remaining: Double, _ unit: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(name).font(.subheadline.weight(.semibold)); Spacer()
                Text("\(remaining.formatted(.number.precision(.fractionLength(0)))) \(unit) left").font(.subheadline)
            }
            if target > 0 { ProgressView(value: min(max(value, 0), target), total: target).tint(color) }
        }.accessibilityElement(children: .combine)
    }
}

struct AgentWeeklyTrend: View {
    @EnvironmentObject private var app: AppStore
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("This week", systemImage: "chart.line.uptrend.xyaxis").font(.headline)
            if let stats = app.stats {
                Text("\(stats.week.daysLogged) training days · \(stats.week.totalSets) sets").font(.subheadline)
            }
            if let trends = app.trendsPayload, !trends.days.isEmpty {
                Chart(trends.days) { day in
                    BarMark(x: .value("Day", day.dayLabel), y: .value("Calories", day.calories)).foregroundStyle(Theme.accent)
                        .accessibilityLabel(day.dayLabel).accessibilityValue("\(day.calories.formatted()) calories")
                }.frame(height: 160)
            } else { Text("No nutrition trend is available yet.").font(.subheadline) }
        }
    }
}

@MainActor
struct AgentCanvasFallback: View {
    @ObservedObject var canvas: AgentSurfaceStore
    let message: String
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Your standard screens are available", systemImage: "rectangle.on.rectangle").font(.headline)
            Text(message).font(.subheadline)
            ViewThatFits(in: .horizontal) {
                HStack { links }
                VStack(alignment: .leading) { links }
            }
        }.foregroundStyle(Theme.ink).appCard()
    }
    @ViewBuilder private var links: some View {
        Button("Meals") { canvas.showsMeals = true }
        Button("Workouts") { canvas.showsWorkouts = true }
        Button("Coach") { canvas.showsLegacyCoach = true }
    }
}

@MainActor
private struct AgentComposer: View {
    @ObservedObject var canvas: AgentSurfaceStore
    @ObservedObject var voice: LiveCoachController
    private var live: Bool { [.connecting, .listening, .speaking].contains(voice.state) }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if live {
                HStack {
                    Text(voice.activityLabel.isEmpty ? LiveCoachPresentation(state: voice.state).status : voice.activityLabel)
                        .font(.caption.weight(.semibold))
                    Spacer()
                    Button { Task { await voice.toggleMute() } } label: {
                        Image(systemName: voice.isMuted ? "mic.slash.fill" : "mic.fill").frame(width: 44, height: 44)
                    }.accessibilityLabel(voice.isMuted ? "Unmute microphone" : "Mute microphone")
                        .disabled(voice.state == .connecting)
                    Button { Task { await voice.end() } } label: { Image(systemName: "stop.fill").frame(width: 44, height: 44) }
                        .accessibilityLabel("Stop voice session")
                }
                if !voice.coachCaption.isEmpty { Text(voice.coachCaption).font(.caption).lineLimit(3) }
                Text("You can interrupt naturally. Typing uses this same session.").font(.caption2).foregroundStyle(Theme.sectionInk)
            } else if case .failed = voice.state {
                let presentation = LiveCoachPresentation(state: voice.state)
                Text(presentation.detail).font(.caption).foregroundStyle(Theme.danger)
                if presentation.showsSettingsAction {
                    Button("Open microphone settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    }.font(.caption)
                }
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Ask or log something…", text: $canvas.draft, axis: .vertical)
                    .lineLimit(1...5).padding(12).background(Theme.surface, in: RoundedRectangle(cornerRadius: 18))
                    .accessibilityLabel("Message your agent")
                Button { Task { await canvas.send() } } label: {
                    if canvas.busy { ProgressView().frame(width: 44, height: 44) }
                    else { Image(systemName: "arrow.up.circle.fill").font(.title).frame(width: 44, height: 44) }
                }.disabled(canvas.busy || canvas.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || canvas.draft.count > 1000)
                    .accessibilityLabel("Send message")
                if !live {
                    Button { Task { await voice.start() } } label: { Image(systemName: "waveform.circle").font(.title).frame(width: 44, height: 44) }
                        .accessibilityLabel("Start voice session")
                }
            }
            if canvas.draft.count > 1000 { Text("Please keep this message under 1,000 characters.").font(.caption).foregroundStyle(Theme.danger) }
        }.padding(.horizontal, 12).padding(.vertical, 8).background(.ultraThinMaterial)
    }
}
