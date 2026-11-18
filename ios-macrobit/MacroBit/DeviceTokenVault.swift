import Foundation
import Security

protocol TokenVault {
    func read() -> String?
    func save(_ token: String) -> Bool
    func clear()
}

struct DeviceTokenVault: TokenVault {
    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "com.biz21.macrobit.deviceToken",
         kSecAttrAccount as String: "device-token"]
    }
    func read() -> String? {
        var query = query
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
    func save(_ token: String) -> Bool {
        var attributes: [String: Any] = [kSecValueData as String: Data(token.utf8),
                                      kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        let result = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if result == errSecSuccess { return true }
        guard result == errSecItemNotFound else { return false }
        attributes.merge(query) { current, _ in current }
        return SecItemAdd(attributes as CFDictionary, nil) == errSecSuccess
    }
    func clear() { SecItemDelete(query as CFDictionary) }
}
