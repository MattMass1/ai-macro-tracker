import SwiftUI

@main
struct MacroTrackerApp: App {
    @StateObject private var auth = AuthService()
    @StateObject private var store = AppStore()

    init() {
        let appearance = UITabBarAppearance()
        appearance.configureWithOpaqueBackground()
        appearance.backgroundColor = UIColor(Theme.surface)
        appearance.shadowColor = UIColor.separator.withAlphaComponent(0.25)
        UITabBar.appearance().standardAppearance = appearance
        UITabBar.appearance().scrollEdgeAppearance = appearance
    }

    var body: some Scene {
        WindowGroup {
            Group {
                if auth.isAuthenticated {
                    ContentView(initialTab: auth.freshClaim ? 1 : 0)
                        .environmentObject(store)
                        .environmentObject(auth)
                } else {
                    InviteView().environmentObject(auth)
                }
            }
            .tint(Theme.accent)
            .preferredColorScheme(.light)
            .onChange(of: auth.isAuthenticated) { _, authenticated in
                // Tenancy: never show the previous user's data to the next one.
                if !authenticated { store.reset() }
            }
        }
    }
}
