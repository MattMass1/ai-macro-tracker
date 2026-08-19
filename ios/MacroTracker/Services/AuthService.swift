import Foundation

/// Auth is "a device token exists in the Keychain". The token is issued once
/// per invite code by POST /api/claim-invite and sent as a Bearer header on
/// every request. There is no local passcode.
@MainActor
final class AuthService: ObservableObject {
    @Published private(set) var isAuthenticated: Bool
    @Published private(set) var displayName: String?
    /// True only in the session where the invite was claimed. Routes the user
    /// into the Coach tab so onboarding starts in chat.
    @Published private(set) var freshClaim = false
    @Published private(set) var isClaiming = false
    @Published var errorMessage: String?

    private let api: APIClient
    private var tokenRejectedObserver: (any NSObjectProtocol)?

    init(api: APIClient = .shared) {
        self.api = api
        isAuthenticated = KeychainStore.deviceToken != nil
        tokenRejectedObserver = NotificationCenter.default.addObserver(forName: .deviceTokenRejected, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in self?.handleTokenRejected() }
        }
    }

    deinit {
        if let tokenRejectedObserver { NotificationCenter.default.removeObserver(tokenRejectedObserver) }
    }

    func claimInvite(code: String, displayName: String? = nil, deviceLabel: String? = nil) async {
        let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { errorMessage = "Enter your invite code."; return }
        guard !isClaiming else { return }
        isClaiming = true
        errorMessage = nil
        defer { isClaiming = false }
        do {
            let claim = try await api.claimInvite(code: trimmed, label: deviceLabel, displayName: displayName)
            guard KeychainStore.saveDeviceToken(claim.token) else {
                errorMessage = "Could not store your access key securely. Try again."
                return
            }
            displayName = claim.displayName
            freshClaim = true
            isAuthenticated = true
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func signOut() {
        KeychainStore.deleteDeviceToken()
        displayName = nil
        freshClaim = false
        isAuthenticated = false
        errorMessage = nil
    }

    private func handleTokenRejected() {
        guard isAuthenticated else { return }
        signOut()
        errorMessage = "Your session ended. Enter an invite code to reconnect."
    }
}
