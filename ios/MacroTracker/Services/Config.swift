import Foundation

enum Config {
    /// Declares that this build decodes unknown (null) protein/carbs/fat/fiber.
    /// The server only allows calorie-only writes for clients that send it, so
    /// older builds, which cannot decode null nutrients, never cause them.
    /// Send it ONLY from builds whose models keep those fields optional.
    static let nutrientContractHeader = "X-Nutrient-Contract"
    static let nutrientContract = "nullable-v1"

    static var apiURL: URL? {
        guard let raw = Bundle.main.object(forInfoDictionaryKey: "MACRO_API_URL") as? String,
              !raw.isEmpty, !raw.contains("$("), let url = URL(string: raw) else { return nil }
        return url
    }
}

