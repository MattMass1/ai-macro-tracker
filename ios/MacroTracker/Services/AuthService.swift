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
    private let tokenProvider: () -> String?
    private let tokenSaver: (String) -> Bool
    private let tokenDeleter: () -> Void
    private var tokenRejectedObserver: (any NSObjectProtocol)?

    init(
        api: APIClient = .shared,
        tokenProvider: @escaping () -> String? = { KeychainStore.deviceToken },
        tokenSaver: @escaping (String) -> Bool = { KeychainStore.saveDeviceToken($0) },
        tokenDeleter: @escaping () -> Void = { KeychainStore.deleteDeviceToken() }
    ) {
        self.api = api
        self.tokenProvider = tokenProvider
        self.tokenSaver = tokenSaver
        self.tokenDeleter = tokenDeleter
        isAuthenticated = tokenProvider() != nil
        tokenRejectedObserver = NotificationCenter.default.addObserver(forName: .deviceTokenRejected, object: nil, queue: .main) { [weak self] notification in
            guard let rejection = notification.object as? DeviceTokenRejection else { return }
            Task { @MainActor [weak self] in self?.handleTokenRejected(rejection) }
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
            guard tokenSaver(claim.token) else {
                errorMessage = "Could not store your access key securely. Try again."
                return
            }
            self.displayName = claim.displayName
            freshClaim = true
            isAuthenticated = true
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func signOut() {
        tokenDeleter()
        displayName = nil
        freshClaim = false
        isAuthenticated = false
        errorMessage = nil
    }

    private func handleTokenRejected(_ rejection: DeviceTokenRejection) {
        guard isAuthenticated, rejection.matches(token: tokenProvider()) else { return }
        signOut()
        errorMessage = "Your session ended. Enter an invite code to reconnect."
    }
}
