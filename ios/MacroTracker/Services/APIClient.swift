import Foundation

extension Notification.Name {
    /// Posted when the server rejects the device token (401). AuthService
    /// listens and bounces the app back to the invite screen.
    static let deviceTokenRejected = Notification.Name("com.biz21.macrotracker.deviceTokenRejected")
}

struct APIError: LocalizedError, Equatable {
    let status: Int
    let message: String
    var errorDescription: String? {
        switch status {
        case 401: return "Your session is no longer valid. Enter an invite code to reconnect."
        case 503: return "The server is waking up. Try again in a moment."
        default: return message
        }
    }
}

final class APIClient {
    static let shared = APIClient()
    private let session: URLSession
    private let baseURL: URL?
    private let tokenProvider: () -> String?
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init(baseURL: URL? = Config.apiURL,
         tokenProvider: @escaping () -> String? = { KeychainStore.deviceToken },
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.tokenProvider = tokenProvider
        self.session = session
        decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
    }

    func claimInvite(code: String, label: String? = nil, displayName: String? = nil) async throws -> ClaimInvitePayload {
        try await request("api/claim-invite", method: "POST", body: encoder.encode(ClaimInviteBody(code: code, label: label, displayName: displayName)), authenticated: false)
    }

    func today() async throws -> DayPayload { try await get("api/today") }
    func day(_ date: String) async throws -> DayPayload { try await get("api/day/\(encoded(date))") }
    func presets() async throws -> PresetsPayload { try await get("api/presets") }
    func logMeal(_ body: LogMealBody) async throws -> DayPayload { try await send("api/log", body: body) }
    func logPreset(_ body: LogPresetBody) async throws -> DayPayload { try await send("api/log-preset", body: body) }
    func chat(_ message: String, metrics: ChatMetrics? = nil) async throws -> ChatReply {
        try await send("api/chat", body: ChatRequest(message: message, date: nil, metrics: metrics))
    }
    func chatHistory(limit: Int) async throws -> [ChatHistoryMessage] {
        struct ChatHistoryPayload: Decodable { let messages: [ChatHistoryMessage] }
        let payload: ChatHistoryPayload = try await get("api/chat/history?limit=\(limit)")
        return payload.messages
    }
    func barcodeFood(code: String) async throws -> BarcodeFoodPayload {
        try await send("api/food/barcode", body: BarcodeFoodRequest(code: code))
    }
    func deleteMeal(_ id: String) async throws -> DayPayload { try await delete("api/meal/\(encoded(id))") }
    func exercises() async throws -> ExercisesPayload { try await get("api/exercises") }
    func library(query: String? = nil) async throws -> LibraryPayload {
        let path = query.map { "api/library?q=\(encoded($0))" } ?? "api/library"
        return try await get(path)
    }
    func plan() async throws -> WorkoutPlanPayload { try await get("api/plan") }
    func savePlan(_ body: WorkoutPlanWrite) async throws -> SavePlanPayload { try await send("api/plan", body: body) }
    func workoutStats() async throws -> WorkoutStatsPayload { try await get("api/workout-stats") }
    func workouts(_ date: String) async throws -> WorkoutsPayload { try await get("api/workouts/\(encoded(date))") }
    func workoutHistory(exercise: String? = nil, limit: Int = 200) async throws -> WorkoutHistoryPayload {
        try await get("api/workouts/history?exercise=\(encoded(exercise ?? ""))&limit=\(limit)")
    }
    func lastWorkout(_ exercise: String) async throws -> LastWorkoutPayload { try await get("api/workouts/last?exercise=\(encoded(exercise))") }
    func logWorkout(_ body: LogWorkoutBody) async throws -> WorkoutEntry { try await send("api/workout", body: body) }
    func deleteWorkout(_ id: String) async throws -> DeletedPayload { try await delete("api/workout/\(encoded(id))") }
    func brief(_ date: String? = nil) async throws -> BriefPayload { try await get("api/brief\(date.map { "?date=\(encoded($0))" } ?? "")") }
    func saveBrief(_ text: String, date: String? = nil) async throws -> BriefPayload { try await send("api/brief", body: BriefRequest(text: text, date: date)) }
    func trends(days: Int) async throws -> TrendsPayload { try await get("api/trends?days=\(days)") }

    private func get<T: Decodable>(_ path: String) async throws -> T { try await request(path, method: "GET", body: Optional<Data>.none) }
    private func delete<T: Decodable>(_ path: String) async throws -> T { try await request(path, method: "DELETE", body: Optional<Data>.none) }
    private func send<T: Decodable, Body: Encodable>(_ path: String, body: Body) async throws -> T { try await request(path, method: "POST", body: encoder.encode(body)) }

    private func request<T: Decodable>(_ path: String, method: String, body: Data?, authenticated: Bool = true) async throws -> T {
        func debugPreview(_ data: Data, limit: Int = 400) -> String {
            let text = String(data: data, encoding: .utf8) ?? "<non-utf8 data, \(data.count) bytes>"
            if text.count > limit {
                let prefix = text.prefix(limit)
                return String(prefix) + " … (truncated, total \(text.count) chars)"
            }
            return text
        }

        guard let baseURL else { throw APIError(status: 500, message: "MACRO_API_URL is missing from Config.xcconfig.") }
        guard let url = URL(string: path, relativeTo: baseURL.appendingPathComponent("/")) else { throw APIError(status: 500, message: "Invalid API URL.") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.timeoutInterval = 45
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if authenticated {
            guard let token = tokenProvider(), !token.isEmpty else {
                NotificationCenter.default.post(name: .deviceTokenRejected, object: nil)
                throw APIError(status: 401, message: "Not signed in.")
            }
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        print("[API] → \(method) \(url.absoluteString)")

        let data: Data
        let response: URLResponse
        do { (data, response) = try await session.data(for: request) }
        catch { throw APIError(status: 503, message: "Could not reach the macro service.") }

        let statusCode = (response as? HTTPURLResponse)?.statusCode ?? -1
        
        guard let http = response as? HTTPURLResponse else { throw APIError(status: 503, message: "The server returned an invalid response.") }
        
        guard (200..<300).contains(http.statusCode) else {
            if authenticated, http.statusCode == 401 {
                NotificationCenter.default.post(name: .deviceTokenRejected, object: nil)
            }
            let preview = debugPreview(data)
            print("[API] ← \(http.statusCode) for \(method) \(url.absoluteString)\n[API] Body: \n\(preview)")
            let serverMessage = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw APIError(status: http.statusCode, message: serverMessage ?? (String(data: data, encoding: .utf8) ?? "Request failed."))
        }
        do {
            let decoded = try decoder.decode(T.self, from: data)
            print("[API] ← \(statusCode) OK for \(method) \(url.absoluteString)")
            return decoded
        } catch {
            let preview = debugPreview(data)
            print("[API] ✳︎ Decode failed for \(method) \(url.absoluteString): \n\(preview)\nError: \(error)")
            throw APIError(status: 500, message: "The server response could not be read: \(error.localizedDescription)")
        }
    }

    private func encoded(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))) ?? value
    }
}
