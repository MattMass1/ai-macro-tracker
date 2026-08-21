import SwiftUI
import UIKit

struct ContentView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var selectedTab = 1  // Coach home
    @State private var showManualMeal = false
    @State private var showWorkoutLogger = false
    @State private var scanFoodTrigger = 0

    init(initialTab: Int = 1) {
        _selectedTab = State(initialValue: initialTab)
    }

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .top) {
                TabView(selection: $selectedTab) {
                    NavigationStack { WorkoutsView() }
                        .tag(0)
                    NavigationStack { ChatLogView(scanFoodTrigger: scanFoodTrigger) }
                        .tag(1)
                    NavigationStack { TodayView(selectedTab: $selectedTab) }
                        .tag(2)
                    NavigationStack { ProgressDashboardView() }
                        .tag(3)
                }
                .tabViewStyle(.page(indexDisplayMode: .never))

                if selectedTab != 1 {
                    AddBarView(
                        selectedTab: selectedTab,
                        onLogMeal: { showManualMeal = true },
                        onLogWorkout: { showWorkoutLogger = true },
                        onScanFood: {
                            selectedTab = 1
                            scanFoodTrigger += 1
                        },
                        onAskCoach: { selectedTab = 1 }
                    )
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                    .padding(.bottom, geometry.safeAreaInsets.bottom + 16)
                    .transition(.scale(scale: 0.9).combined(with: .opacity))
                    .zIndex(1)
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
        }
        .animation(.spring(response: 0.35, dampingFraction: 0.78), value: store.toast)
        .animation(.spring(response: 0.38, dampingFraction: 0.82), value: selectedTab)
        .onChange(of: selectedTab) { _, _ in dismissKeyboard() }
        .task { await store.loadAll() }
        .sheet(isPresented: $showManualMeal) { ManualFoodView() }
        .sheet(isPresented: $showWorkoutLogger) { WorkoutLoggerView() }
        .onChange(of: scenePhase) { _, phase in
            // Reload when the app returns to foreground so the day rolls over
            // even if it was open across midnight (or slept for hours).
            if phase == .active {
                let now = Date()
                if !Calendar.current.isDateInToday(store.selectedDate) {
                    store.selectedDate = now
                }
                Task { await store.loadAll() }
            }
        }
        .alert("Couldn’t complete that", isPresented: Binding(get: { store.errorMessage != nil }, set: { if !$0 { store.errorMessage = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(store.errorMessage ?? "Unknown error")
        }
    }

    private func dismissKeyboard() {
        UIApplication.shared.sendAction(
            #selector(UIResponder.resignFirstResponder),
            to: nil,
            from: nil,
            for: nil
        )
    }
}

private struct ProgressDashboardView: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        ZStack {
            Theme.canvas.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Progress")
                        .font(.system(size: 28, weight: .bold))
                        .foregroundStyle(Theme.ink)
                    VStack(alignment: .leading, spacing: 18) {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("COMING SOON").font(.caption2.weight(.bold)).tracking(1.4).foregroundStyle(Theme.accent)
                                Text("Your trends, at a glance").font(.headline).foregroundStyle(Theme.ink)
                            }
                            Spacer()
                            Image(systemName: "chart.line.uptrend.xyaxis").font(.title2).foregroundStyle(Theme.accent)
                        }
                        if let day = store.day {
                            CalorieSnapshot(date: store.selectedDate, consumed: day.totals.calories, target: day.targets.calories)
                        } else {
                            Label("Log meals to start building your progress view.", systemImage: "fork.knife")
                                .font(.subheadline).foregroundStyle(Theme.muted)
                        }
                    }.appCard(padding: 18)
                }.padding(.horizontal, 16).padding(.top, 16).padding(.bottom, 96)
            }
        }
        .navigationBarHidden(true)
    }
}

private struct CalorieSnapshot: View {
    let date: Date
    let consumed: Double
    let target: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("\(date.formatted(.dateTime.month(.abbreviated).day()).uppercased()) CALORIES")
                .font(.caption2.weight(.bold)).tracking(1.2).foregroundStyle(Theme.muted)
            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.divider)
                    Capsule().fill(Theme.accent).frame(width: geometry.size.width * fraction)
                }
            }.frame(height: 10)
            HStack {
                Text("\(Int(consumed).formatted()) consumed").font(.subheadline.weight(.bold)).foregroundStyle(Theme.ink)
                Spacer()
                Text("\(Int(target).formatted()) goal").font(.caption).foregroundStyle(Theme.muted)
            }
        }
    }

    private var fraction: CGFloat {
        guard target > 0 else { return 0 }
        return CGFloat(min(max(consumed / target, 0), 1))
    }
}
