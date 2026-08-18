import CryptoKit
import LocalAuthentication
import Security

@MainActor
final class AuthService: ObservableObject {
    @Published private(set) var isUnlocked = false
    @Published private(set) var hasPasscode = false
    @Published var errorMessage: String?
    private let account = "local-passcode-sha256"
    private let service = "com.biz21.macrotracker.auth"

    init() { hasPasscode = readHash() != nil }

    func setPasscode(_ passcode: String) {
        guard passcode.count >= 4, passcode.allSatisfy(\.isNumber) else { errorMessage = "Use at least four digits."; return }
        guard saveHash(hash(passcode)) else { errorMessage = "Could not save the passcode securely."; return }
        hasPasscode = true
        isUnlocked = true
        errorMessage = nil
    }

    func unlock(_ passcode: String) {
        guard let stored = readHash(), hash(passcode) == stored else { errorMessage = "That passcode doesn’t match."; return }
        errorMessage = nil
        isUnlocked = true
    }

    func unlockWithBiometrics() async {
        let context = LAContext()
        var authError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &authError) else { return }
        do {
            if try await context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: "Unlock Macro Tracker") { isUnlocked = true; errorMessage = nil }
        } catch { errorMessage = "Biometric unlock wasn’t completed." }
    }

    func lock() { if hasPasscode { isUnlocked = false } }
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

