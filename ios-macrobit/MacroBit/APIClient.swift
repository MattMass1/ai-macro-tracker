import Foundation

struct APIError: LocalizedError {
    let status: Int
    let message: String
    var errorDescription: String? {
        switch status {
        case 401: return "That token didn't work"
        case 409: return "Still working on your last message"
        case 503: return "The server is waking up. This can take about a minute. Try again when you're ready. \(message)"
        case 500, 502: return "Something went wrong. You can send your message again."
        default: return message
        }
    }
}

final class APIClient {
    private let baseURL: URL?
    private let tokenProvider: () -> String?
    private let session: URLSession
    private let onUnauthorized: () -> Void

    init(baseURL: URL? = Config.apiURL, tokenProvider: @escaping () -> String?, session: URLSession = .shared, onUnauthorized: @escaping () -> Void = {}) {
        self.baseURL = baseURL; self.tokenProvider = tokenProvider
        self.session = session; self.onUnauthorized = onUnauthorized
    }

    func today() async throws -> DayPayload { try await request("api/today") }
    func day(_ date: String) async throws -> DayPayload { try await request("api/day/\(encoded(date))") }
    func presets() async throws -> PresetsPayload { try await request("api/presets") }
    func trends(days: Int) async throws -> TrendsPayload { try await request("api/trends?days=\(days == 7 ? 7 : 30)") }
    func logMeal(_ body: LogMealBody) async throws -> DayPayload { try await request("api/log", method: "POST", body: encode(body)) }
    func logPreset(_ body: LogPresetBody) async throws -> DayPayload { try await request("api/log-preset", method: "POST", body: encode(body)) }
    func deleteMeal(_ id: String) async throws -> DayPayload { try await request("api/meal/\(encoded(id))", method: "DELETE") }
    /// Logs a meal already in the log again. The server copies the stored numbers; the app never sends any.
    func repeatMeal(_ id: String, _ body: RepeatMealBody = RepeatMealBody()) async throws -> DayPayload { try await request("api/meal/\(encoded(id))/repeat", method: "POST", body: encode(body)) }
    /// Saves a meal already in the log as a saved-meal button, with its stored numbers.
    func saveMealAsPreset(_ id: String, _ body: SaveMealPresetBody = SaveMealPresetBody()) async throws -> PresetsPayload { try await request("api/meal/\(encoded(id))/save-preset", method: "POST", body: encode(body)) }
    func editPreset(_ body: EditPresetBody) async throws -> PresetsPayload { try await request("api/presets/edit", method: "POST", body: encode(body)) }
    func removePreset(_ body: RemovePresetBody) async throws -> PresetsPayload { try await request("api/presets/remove", method: "POST", body: encode(body)) }
    func turn(sessionID: UUID, turnID: UUID, message: String) async throws -> CoachTurn {
        try await request("api/agent-canvas/\(sessionID.uuidString.lowercased())/turn", method: "POST", body: JSONEncoder().encode(["turn_id": turnID.uuidString.lowercased(), "message": message]), chat: true)
    }

    private func encode<T: Encodable>(_ body: T) throws -> Data {
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        return try encoder.encode(body)
    }

    private func request<T: Decodable>(_ path: String, method: String = "GET", body: Data? = nil, chat: Bool = false) async throws -> T {
        guard let baseURL, let url = URL(string: path, relativeTo: baseURL.appendingPathComponent("/")) else {
            throw APIError(status: 0, message: "Set MACRO_API_URL in Config.xcconfig before connecting.")
        }
        guard let token = tokenProvider(), !token.isEmpty else {
            onUnauthorized(); throw APIError(status: 401, message: "Not signed in")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method; request.httpBody = body
        request.timeoutInterval = chat ? 120 : 45
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("nullable-v1", forHTTPHeaderField: "X-Nutrient-Contract")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        // No request, body, response or credential logging, including Debug builds.
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        if http.statusCode == 401 { onUnauthorized() }
        if chat && data.count > 48_000 { throw PayloadError.invalidPayload }
        guard (200..<300).contains(http.statusCode) else {
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            throw APIError(status: http.statusCode, message: object?["error"] as? String ?? "Request failed. Nothing was confirmed.")
        }
        if chat {
            guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let reply = object["reply"] as? String,
                  !reply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  let labels = object["quickReplies"] as? [String], labels.count <= 4,
                  labels.allSatisfy({ !$0.isEmpty && $0.count <= 40 }) else { throw PayloadError.invalidPayload }
        }
        let decoder = JSONDecoder()
        if !chat { decoder.keyDecodingStrategy = .convertFromSnakeCase }
        return try decoder.decode(T.self, from: data)
    }

    private func encoded(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-._~"))) ?? value
    }
}

enum Config {
    static var apiURL: URL? {
        guard let raw = Bundle.main.object(forInfoDictionaryKey: "MACRO_API_URL") as? String,
              let url = URL(string: raw), url.scheme == "https", url.host == "mmmacros-backend.onrender.com" else { return nil }
        return url
    }
}
