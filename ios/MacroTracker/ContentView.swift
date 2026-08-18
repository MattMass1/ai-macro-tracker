import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var store: AppStore
    @State private var selectedTab = 0
    var body: some View {
        ZStack(alignment: .top) {
            TabView(selection: $selectedTab) {
                NavigationStack { TodayView() }.tabItem { Label("Today", systemImage: "circle.grid.2x2.fill") }.tag(0)
                NavigationStack { ChatLogView() }.tabItem { Label("Log", systemImage: "plus.circle.fill") }.tag(1)
                NavigationStack { WorkoutsView() }.tabItem { Label("Workouts", systemImage: "figure.strengthtraining.traditional") }.tag(2)
            }
            if let toast = store.toast {
                Label(toast, systemImage: "checkmark.circle.fill").font(.subheadline.weight(.semibold)).padding(.horizontal, 16).padding(.vertical, 10).background(.ultraThinMaterial, in: Capsule()).shadow(color: .black.opacity(0.12), radius: 16, y: 6).padding(.top, 8).transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.spring(response: 0.35, dampingFraction: 0.78), value: store.toast)
        .task { await store.loadAll() }
        .alert("Couldn’t complete that", isPresented: Binding(get: { store.errorMessage != nil }, set: { if !$0 { store.errorMessage = nil } })) { Button("OK", role: .cancel) {} } message: { Text(store.errorMessage ?? "Unknown error") }
    }
}

