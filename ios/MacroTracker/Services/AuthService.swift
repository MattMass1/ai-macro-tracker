import CryptoKit
import LocalAuthentication
import Security

@MainActor
final class AuthService: ObservableObject {
    /// Seconds the session may stay unlocked after the app leaves the foreground.
    /// Default is 5 minutes. Change this value to require the passcode sooner or later.
    static let unlockGracePeriod: TimeInterval = 300

    @Published private(set) var isUnlocked = false
    @Published private(set) var hasPasscode = false
    @Published var errorMessage: String?
    private let account = "local-passcode-sha256"
    private let service = "com.biz21.macrotracker.auth"
    private var lastUnlockedAt: Date?
    private var leftForegroundAt: Date?

    init() { hasPasscode = readHash() != nil }

    func setPasscode(_ passcode: String) {
        guard passcode.count >= 4, passcode.allSatisfy(\.isNumber) else { errorMessage = "Use at least four digits."; return }
        guard saveHash(hash(passcode)) else { errorMessage = "Could not save the passcode securely."; return }
        hasPasscode = true
        markUnlocked()
    }

    func unlock(_ passcode: String) {
        guard let stored = readHash(), hash(passcode) == stored else { errorMessage = "That passcode doesn’t match."; return }
        markUnlocked()
    }

    func unlockWithBiometrics() async {
        let context = LAContext()
        var authError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &authError) else { return }
        do {
            if try await context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: "Unlock Macro Tracker") { markUnlocked() }
        } catch { errorMessage = "Biometric unlock wasn’t completed." }
    }

    func lock() {
        guard hasPasscode else { return }
        isUnlocked = false
        lastUnlockedAt = nil
        leftForegroundAt = nil
    }

    /// Call when the scene moves to `.background`. Does not clear `isUnlocked`.
    func handleDidEnterBackground() {
        guard hasPasscode, isUnlocked else { return }
        leftForegroundAt = Date()
    }

    /// Call when the scene becomes `.active`. Locks only if the grace period has elapsed.
    func handleDidBecomeActive() {
        guard hasPasscode, isUnlocked, let leftForegroundAt else { return }
        if Date().timeIntervalSince(leftForegroundAt) >= Self.unlockGracePeriod {
            lock()
        } else {
            self.leftForegroundAt = nil
        }
    }

    private func markUnlocked() {
        isUnlocked = true
        lastUnlockedAt = Date()
        leftForegroundAt = nil
        errorMessage = nil
    }
    private func hash(_ value: String) -> Data { Data(SHA256.hash(data: Data(value.utf8))) }
    private func query() -> [String: Any] { [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account] }
    private func readHash() -> Data? {
        var q = query(); q[kSecReturnData as String] = true; q[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?; guard SecItemCopyMatching(q as CFDictionary, &result) == errSecSuccess else { return nil }
        return result as? Data
    }
    private func saveHash(_ data: Data) -> Bool {
        SecItemDelete(query() as CFDictionary)
        var q = query(); q[kSecValueData as String] = data; q[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        return SecItemAdd(q as CFDictionary, nil) == errSecSuccess
    }
}

