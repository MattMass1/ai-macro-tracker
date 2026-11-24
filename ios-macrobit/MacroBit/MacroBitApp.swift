import AuthenticationServices
import SwiftUI
import UIKit

@main
struct MacroBitApp: App {
    @StateObject private var store = AppStore()
    var body: some Scene {
        WindowGroup {
            RootView().environmentObject(store).tint(Theme.accent).preferredColorScheme(.light)
                .task {
                    #if DEBUG
                    if ProcessInfo.processInfo.arguments.contains("-fixture-ui") {
                        store.installOfflineFixtures()
                        return
                    }
                    #endif
                    await store.restore()
                }
        }
    }
}

struct RootView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.scenePhase) private var scenePhase
    var body: some View {
        Group {
            if store.isSignedIn { ContentView() } else { TokenView() }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active && store.isSignedIn { Task { await store.refreshToday(); store.rollChatIfNewDay(); await store.loadSuggestions() } }
        }
    }
}

struct TokenView: View {
    @EnvironmentObject private var store: AppStore
    @State private var nonce = ""
    var body: some View {
        ZStack {
            StudioBackground(alignment: .bottom)
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Text("Macro-Bit").font(Theme.pixelFont(36, relativeTo: .largeTitle))
                    CompanionView(character: .male, state: .rest).frame(height: 180).frame(maxWidth: .infinity)
                    VStack(alignment: .leading, spacing: 16) {
                        Text("Your studio, a fresh start").font(.title2.bold())
                        Text("Sign in to start logging. Your old app and its log stay separate.").foregroundStyle(Theme.muted)
                        SignInWithAppleButton(.signIn) { request in
                            let raw = AppleNonce.make(); nonce = raw
                            request.requestedScopes = [.fullName, .email]
                            request.nonce = AppleNonce.sha256(raw)
                        } onCompletion: { result in
                            appleFinished(result)
                        }
                        .signInWithAppleButtonStyle(.black).frame(height: 50).disabled(store.isValidating)
                        .accessibilityIdentifier("sign-in-with-apple")
                        if store.isValidating {
                            ProgressView("Connecting. The server can take about a minute to wake up.")
                        }
                        if let error = store.errorMessage { Text(error).foregroundStyle(Theme.danger).accessibilityIdentifier("token-error") }
                    }.appCard()
                }.padding(20)
            }
        }.foregroundStyle(Theme.ink)
    }

    private func appleFinished(_ result: Result<ASAuthorization, Error>) {
        switch result {
        case .success(let authorization):
            guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
                  let data = credential.identityToken, let identityToken = String(data: data, encoding: .utf8), !nonce.isEmpty else {
                store.errorMessage = "Apple didn't return a sign-in. Try again."; return
            }
            let parts = [credential.fullName?.givenName, credential.fullName?.familyName].compactMap { $0 }.filter { !$0.isEmpty }
            let raw = nonce; nonce = ""
            Task { _ = await store.signInWithApple(identityToken: identityToken, nonce: raw, name: parts.isEmpty ? nil : parts.joined(separator: " ")) }
        case .failure(let error):
            if (error as? ASAuthorizationError)?.code == .canceled { return }
            store.errorMessage = "Sign in with Apple didn't finish. Try again."
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var store: AppStore
    @State private var tab = 0
    @State private var keyboardPresented = false
    var body: some View {
        VStack(spacing: 0) {
            if store.isFixture {
                Text("SYNTHETIC OFFLINE FIXTURES").font(.caption2.bold()).padding(4).frame(maxWidth: .infinity).background(Theme.accentTint)
            }
            Group {
                switch tab {
                case 2: WorkoutsView()
                case 3: ProgressViewScreen()
                default: NavigationStack { HomeView(openWorkouts: { tab = 2 }) }
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            if !keyboardPresented { StudioTabBar(selection: $tab) }
        }.background(Theme.canvas).foregroundStyle(Theme.ink)
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in keyboardPresented = true }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboardPresented = false }
    }
}


struct WorkoutsView: View {
    var body: some View {
        ZStack {
            StudioBackground(alignment: .bottom)
            VStack(alignment: .leading, spacing: 14) {
                Text("Workouts").font(.largeTitle.bold())
                EmptyState(icon: "dumbbell.fill", title: "Coming soon", message: "Your training space is taking shape. Food logging is ready in Studio.")
                CompanionView(character: .male, state: .rest).frame(height: 224).frame(maxWidth: .infinity)
            }.padding(16)
        }
    }
}
