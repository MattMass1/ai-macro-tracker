import SwiftUI

@main
struct MacroTrackerApp: App {
    @Environment(\.scenePhase) private var scenePhase
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
                if auth.isUnlocked || !auth.hasPasscode && auth.isUnlocked { ContentView().environmentObject(store) }
                else { PasscodeView().environmentObject(auth) }
            }
            .tint(Theme.accent)
            .onChange(of: scenePhase) { _, phase in
                switch phase {
                case .background: auth.handleDidEnterBackground()
                case .active: auth.handleDidBecomeActive()
                default: break
                }
            }
        }
    }
}

struct PasscodeView: View {
    @EnvironmentObject private var auth: AuthService
    @State private var passcode = ""
    @FocusState private var focused: Bool

    var body: some View {
        ZStack {
            Theme.canvas.ignoresSafeArea()
            VStack(alignment: .leading, spacing: 28) {
                Spacer()
                Image(systemName: "leaf.fill").font(.system(size: 34, weight: .semibold)).foregroundStyle(Theme.accent)
                VStack(alignment: .leading, spacing: 8) {
                    Text(auth.hasPasscode ? "Welcome back." : "Protect your log.").font(.system(size: 36, weight: .bold, design: .rounded)).tracking(-1)
                    Text(auth.hasPasscode ? "Enter your passcode to see today’s numbers." : "Create a numeric passcode. It stays in your iPhone’s Keychain.").foregroundStyle(.secondary)
                }
                SecureField("Passcode", text: $passcode).keyboardType(.numberPad).textContentType(.password).focused($focused).padding(16).background(Theme.surface, in: RoundedRectangle(cornerRadius: 16))
                if let error = auth.errorMessage { Label(error, systemImage: "exclamationmark.circle.fill").font(.footnote).foregroundStyle(Theme.danger) }
                Button {
                    auth.hasPasscode ? auth.unlock(passcode) : auth.setPasscode(passcode)
                } label: { Text(auth.hasPasscode ? "Unlock" : "Create passcode").fontWeight(.bold).frame(maxWidth: .infinity).padding(.vertical, 15).background(Theme.accent, in: RoundedRectangle(cornerRadius: 16)).foregroundStyle(.white) }
                .disabled(passcode.count < 4).opacity(passcode.count < 4 ? 0.45 : 1)
                if auth.hasPasscode {
                    Button("Unlock with Face ID", systemImage: "faceid") { Task { await auth.unlockWithBiometrics() } }.frame(maxWidth: .infinity)
                }
                Spacer().frame(height: 36)
            }.padding(24)
        }.onAppear { focused = true; if auth.hasPasscode { Task { await auth.unlockWithBiometrics() } } }
    }
}

