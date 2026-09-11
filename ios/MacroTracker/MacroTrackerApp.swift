import SwiftUI

@main
struct MacroTrackerApp: App {
    @StateObject private var auth = AuthService()
    @StateObject private var store = AppStore()

    var body: some Scene {
        WindowGroup {
            Group {
                if auth.isAuthenticated {
                    ContentView()
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
