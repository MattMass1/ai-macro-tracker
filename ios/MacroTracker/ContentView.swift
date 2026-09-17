import SwiftUI
import UIKit
import Charts

enum AgentRootRoute: Equatable {
    case loading, onboarding, canvas, compatibility

    static func isReady(hasTargets: Bool?, calories: Double?, hasPlan: Bool?) -> Bool {
        hasTargets == true && (calories ?? 0) > 0 && hasPlan == true
    }

    static func initial(hasTargets: Bool?, calories: Double?, hasPlan: Bool?) -> Self {
        // Absence is unknown, never evidence that an account needs onboarding.
        // A local freshClaim bit cannot override the server's readiness facts.
        if hasTargets == false || hasPlan == false { return .onboarding }
        if isReady(hasTargets: hasTargets, calories: calories, hasPlan: hasPlan) { return .canvas }
        return .loading
    }
}

/// Shared by initial load, explicit retry and scene activation. Only GETs are
/// retried; three attempts plus 5/15s backoff allow a 30-60s Render cold start
/// (each ordinary API read also has its existing 45s network timeout).
@MainActor
final class AgentRootLoader: ObservableObject {
    @Published private(set) var route = AgentRootRoute.loading
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?
    private var generation = UUID()

    func load(readiness: () async -> AgentRootRoute,
              retryDelays: [Double] = [5, 15],
              sleep: (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) }) async {
        let current = UUID()
        generation = current
        isLoading = true
        errorMessage = nil
        defer { if generation == current { isLoading = false } }
        for attempt in 0...retryDelays.count {
            guard !Task.isCancelled, generation == current else { return }
            let result = await readiness()
            guard !Task.isCancelled, generation == current else { return }
            // Once real onboarding starts, do not unmount a picker or pending
            // metrics form when readiness changes midway through that flow.
            if route != .onboarding { route = result }
            if route != .loading { return }
            if attempt < retryDelays.count {
                do { try await sleep(retryDelays[attempt]) } catch { return }
            }
        }
        errorMessage = "Your account could not be loaded. Check your connection and retry."
    }

    func completeOnboarding() {
        generation = UUID()
        isLoading = false
        errorMessage = nil
        route = .canvas
    }
}

@MainActor
struct ContentView: View {
    @EnvironmentObject private var store: AppStore
    @EnvironmentObject private var auth: AuthService
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var loader = AgentRootLoader()
    @State private var reloadID = UUID()
    // Preserve preview call sites; tabs are no longer primary navigation.
    init(initialTab: Int = 1) {}
    var body: some View {
        AgentCanvasView(canvas: store.canvas)
        .safeAreaInset(edge: .top) {
            if let error = loader.errorMessage {
                HStack { Text(error).font(.caption); Button("Retry") { reloadID = UUID() } }.padding(8)
            } else if store.accountRoute == .compatibility {
                Text("This server needs an update. Standard tools remain available from the menu.")
                    .font(.caption).padding(8)
            }
        }
        .modifier(AgentToastModifier())
        .alert("Could not complete that", isPresented: Binding(get: { loader.route != .loading && store.errorMessage != nil }, set: { if !$0 { store.errorMessage = nil } })) {
            Button("OK", role: .cancel) { store.errorMessage = nil }
        } message: { Text(store.errorMessage ?? "Please try again.") }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                // Cancel/re-evaluate the root GET task even after a failed load.
                reloadID = UUID()
                Task { await store.canvas.refresh() }
            } else if phase == .background {
                Task { await store.canvas.voice.end() }
            }
        }
        .task(id: reloadID) {
            store.selectCurrentDay()
            // Readiness updates the permanent strip; it never gates the Canvas.
            await loader.load(readiness: {
                await store.loadAll(reportErrors: loader.route != .loading)
                return store.accountRoute
            })
        }
    }
}

struct ProgressDashboardView: View {
    @EnvironmentObject private var store: AppStore
    @State private var selectedWindow = 7

    var body: some View {
        ZStack {
            Theme.canvas.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HStack {
                        Text("Progress").font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.ink)
                        Spacer()
                        Image(systemName: "chart.line.uptrend.xyaxis").font(.title2).foregroundStyle(Theme.accent)
                    }
                    if let trends = store.trendsPayload, !trends.days.isEmpty {
                        if trends.weight.currentKg > 0, trends.weight.goalKg > 0 { WeightTrendCard(weight: trends.weight) }
                        Picker("Trend window", selection: $selectedWindow) {
                            Text("7 days").tag(7)
                            Text("30 days").tag(30)
                        }.pickerStyle(.segmented)
                        CalorieTrendChart(days: trends.days)
                        WeeklyTrendList(weeks: trends.weekly)
                        AdherenceCard(
                            loggedDays: trends.days.filter { $0.calories > 0 || $0.protein > 0 || $0.carbs > 0 || $0.fat > 0 }.count,
                            window: selectedWindow
                        )
                    } else {
                        EmptyState(icon: "chart.bar", title: "No trends yet", message: "Log meals to see your trends.")
                    }
                }.padding(.horizontal, 16).padding(.top, 16).padding(.bottom, 96)
            }
        }
        .navigationBarHidden(true)
        .task(id: selectedWindow) { await store.trends(days: selectedWindow) }
        .refreshable { await store.trends(days: selectedWindow) }
    }
}

private struct WeightTrendCard: View {
    let weight: TrendWeight

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { SectionLabel(text: "Weight"); Spacer(); Image(systemName: "scalemass.fill").foregroundStyle(Theme.accent) }
            Text("Current \(weight.currentKg.formatted(.number.precision(.fractionLength(1)))) kg → Goal \(weight.goalKg.formatted(.number.precision(.fractionLength(1)))) kg")
                .font(.headline).foregroundStyle(Theme.ink)
            Text("\(abs(weight.currentKg - weight.goalKg).formatted(.number.precision(.fractionLength(1)))) kg to goal")
                .font(.caption).foregroundStyle(Theme.muted)
            GeometryReader { geometry in
                ZStack(alignment: .leading) { Capsule().fill(Theme.divider); Capsule().fill(Theme.accent).frame(width: geometry.size.width * progress) }
            }.frame(height: 10)
        }.appCard(padding: 18)
    }

    private var progress: CGFloat {
        guard weight.currentKg > 0 else { return 0 }
        return CGFloat(min(max(weight.goalKg / weight.currentKg, 0), 1))
    }
}

private struct CalorieTrendChart: View {
    let days: [TrendDay]
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionLabel(text: "Daily calories")
            Chart {
                ForEach(days) { day in
                    BarMark(x: .value("Day", day.dayLabel), y: .value("Calories", day.calories))
                        .foregroundStyle(Theme.accent.gradient).cornerRadius(4)
                }
                if let target = days.last(where: { $0.targetCalories > 0 })?.targetCalories {
                    RuleMark(y: .value("Target", target))
                        .foregroundStyle(Theme.carbs).lineStyle(StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
                        .annotation(position: .top, alignment: .trailing) { Text("TARGET").font(.caption2.weight(.bold)).foregroundStyle(Theme.carbs) }
                }
            }
            .chartXAxis { AxisMarks(values: .automatic(desiredCount: min(days.count, 7))) { AxisValueLabel() } }
            .chartYAxis { AxisMarks(position: .leading) }
            .frame(height: 220)
        }.appCard(padding: 18)
    }
}

private struct WeeklyTrendList: View {
    let weeks: [TrendWeek]
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionLabel(text: "Weekly averages")
            ForEach(Array(weeks.enumerated()), id: \.element.id) { index, week in
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "calendar").foregroundStyle(Theme.accent)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(week.weekStart).font(.caption.weight(.semibold)).foregroundStyle(Theme.muted)
                        Text("Avg \(Int(week.avgCalories).formatted()) kcal · \(Int(week.avgProtein).formatted())g protein · \(Int(week.daysLogged).formatted()) days logged")
                            .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
                    }
                }
                if index < weeks.count - 1 { Divider() }
            }
        }.appCard(padding: 18)
    }
}

private struct AdherenceCard: View {
    let loggedDays: Int
    let window: Int
    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: "checkmark.circle.fill").font(.title2).foregroundStyle(Theme.accent)
            VStack(alignment: .leading, spacing: 3) {
                SectionLabel(text: "Adherence")
                Text("\(loggedDays) of \(window) days logged").font(.headline).foregroundStyle(Theme.ink)
            }
            Spacer()
            Text("\(percent)%").font(.title2.weight(.bold)).monospacedDigit().foregroundStyle(Theme.accent)
        }.appCard(padding: 18)
    }
    private var percent: Int {
        guard window > 0 else { return 0 }
        return Int((Double(loggedDays) / Double(window) * 100).rounded())
    }
}
