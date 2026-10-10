import SwiftUI
import UIKit
import Charts

/// The classic four-tab app: Workouts, Coach (default), Today, Progress.
struct ContentView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var selectedTab = 1  // Coach home

    init(initialTab: Int = 1) {
        _selectedTab = State(initialValue: initialTab)
    }

    var body: some View {
        ZStack(alignment: .top) {
            TabView(selection: $selectedTab) {
                NavigationStack { WorkoutsView() }
                    .tabItem { Label("Workouts", systemImage: "dumbbell.fill") }
                    .tag(0)
                NavigationStack { CoachChatView() }
                    .tabItem { Label("Coach", systemImage: "bubble.left.and.bubble.right.fill") }
                    .tag(1)
                NavigationStack { TodayView(onAskCoach: { selectedTab = 1 }) }
                    .tabItem { Label("Today", systemImage: "chart.pie.fill") }
                    .tag(2)
                NavigationStack { ProgressDashboardView() }
                    .tabItem { Label("Progress", systemImage: "chart.line.uptrend.xyaxis") }
                    .tag(3)
            }

            if let toast = store.toast {
                Label(toast, systemImage: "checkmark.circle.fill")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.ink)
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .background(Theme.surface, in: Capsule())
                    .shadow(color: Theme.ink.opacity(0.12), radius: 16, y: 6)
                    .padding(.top, 8)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.spring(response: 0.35, dampingFraction: 0.78), value: store.toast)
        .onChange(of: selectedTab) { _, _ in dismissKeyboard() }
        .task { await store.loadAll() }
        .onChange(of: scenePhase) { _, phase in
            // Reload when the app returns to the foreground so the day rolls
            // over even if it was open across midnight (or slept for hours).
            if phase == .active {
                store.selectCurrentDay()
                Task { await store.loadAll() }
            }
        }
        .alert("Couldn't complete that", isPresented: Binding(get: { store.errorMessage != nil }, set: { if !$0 { store.errorMessage = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(store.errorMessage ?? "Unknown error")
        }
    }

    private func dismissKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
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

    // Stored metric, shown imperial (US). Progress below is unit-agnostic.
    private var currentLb: Double { weight.currentKg * 2.2046226218 }
    private var goalLb: Double { weight.goalKg * 2.2046226218 }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { SectionLabel(text: "Weight"); Spacer(); Image(systemName: "scalemass.fill").foregroundStyle(Theme.accent) }
            Text("Current \(currentLb.formatted(.number.precision(.fractionLength(0)))) lb → Goal \(goalLb.formatted(.number.precision(.fractionLength(0)))) lb")
                .font(.headline).foregroundStyle(Theme.ink)
            Text("\(abs(currentLb - goalLb).formatted(.number.precision(.fractionLength(0)))) lb to goal")
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
                        Text(week.macrosComplete == false
                             ? "Avg \(Int(week.avgCalories).formatted()) kcal · protein incomplete · \(Int(week.daysLogged).formatted()) days logged"
                             : "Avg \(Int(week.avgCalories).formatted()) kcal · \(Int(week.avgProtein).formatted())g protein · \(Int(week.daysLogged).formatted()) days logged")
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
