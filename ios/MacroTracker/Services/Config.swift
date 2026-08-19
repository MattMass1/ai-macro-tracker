import Foundation

enum Config {
    static var apiURL: URL? {
        guard let raw = Bundle.main.object(forInfoDictionaryKey: "MACRO_API_URL") as? String,
              !raw.isEmpty, !raw.contains("$("), let url = URL(string: raw) else { return nil }
        return url
    }
}

