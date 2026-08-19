import Foundation

struct APIError: LocalizedError, Equatable {
    let status: Int
    let message: String
    var errorDescription: String? {
        switch status {
        case 401, 403: return "The app token was rejected. Check Config.xcconfig."
        case 503: return "The server is waking up. Try again in a moment."
        default: return message
        }
    }
}

final class APIClient {
    static let shared = APIClient()
    private let session: URLSession
    private let baseURL: URL?
    private let token: String
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init(baseURL: URL? = Config.apiURL, token: String = Config.token, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
        decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
    }

    func today() async throws -> DayPayload { try await get("api/today") }
    func day(_ date: String) async throws -> DayPayload { try await get("api/day/\(encoded(date))") }
    func presets() async throws -> PresetsPayload { try await get("api/presets") }
    func logMeal(_ body: LogMealBody) async throws -> DayPayload { try await send("api/log", body: body) }
    func logPreset(_ body: LogPresetBody) async throws -> DayPayload { try await send("api/log-preset", body: body) }
    func chat(_ message: String, date: String? = nil) async throws -> ChatPayload { try await send("api/chat", body: ChatRequest(message: message, date: date)) }
    func analyze(image: String, meal: String? = nil) async throws -> VisionPayload { try await send("api/vision-log", body: VisionRequest(image: image, meal: meal)) }
    func deleteMeal(_ id: String) async throws -> DayPayload { try await delete("api/meal/\(encoded(id))") }
    func exercises() async throws -> ExercisesPayload { try await get("api/exercises") }
    func plan() async throws -> WorkoutPlanPayload { try await get("api/plan") }
    func workoutStats() async throws -> WorkoutStatsPayload { try await get("api/workout-stats") }
    func workouts(_ date: String) async throws -> WorkoutsPayload { try await get("api/workouts/\(encoded(date))") }
    func lastWorkout(_ exercise: String) async throws -> LastWorkoutPayload { try await get("api/workouts/last?exercise=\(encoded(exercise))") }
    func logWorkout(_ body: LogWorkoutBody) async throws -> WorkoutEntry { try await send("api/workout", body: body) }
    func deleteWorkout(_ id: String) async throws -> DeletedPayload { try await delete("api/workout/\(encoded(id))") }
    func brief(_ date: String? = nil) async throws -> BriefPayload { try await get("api/brief\(date.map { "?date=\(encoded($0))" } ?? "")") }
    func saveBrief(_ text: String, date: String? = nil) async throws -> BriefPayload { try await send("api/brief", body: BriefRequest(text: text, date: date)) }

    private func get<T: Decodable>(_ path: String) async throws -> T { try await request(path, method: "GET", body: Optional<Data>.none) }
    private func delete<T: Decodable>(_ path: String) async throws -> T { try await request(path, method: "DELETE", body: Optional<Data>.none) }
    private func send<T: Decodable, Body: Encodable>(_ path: String, body: Body) async throws -> T { try await request(path, method: "POST", body: encoder.encode(body)) }

    private func request<T: Decodable>(_ path: String, method: String, body: Data?) async throws -> T {
        guard let baseURL else { throw APIError(status: 500, message: "MACRO_API_URL is missing from Config.xcconfig.") }
        guard !token.isEmpty else { throw APIError(status: 500, message: "APP_SHARED_TOKEN is missing from Config.xcconfig.") }
        guard let url = URL(string: path, relativeTo: baseURL.appendingPathComponent("/")) else { throw APIError(status: 500, message: "Invalid API URL.") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.timeoutInterval = 45
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(token, forHTTPHeaderField: "X-App-Token")
        let data: Data
        let response: URLResponse
        do { (data, response) = try await session.data(for: request) }
        catch { throw APIError(status: 503, message: "Could not reach the macro service.") }
        guard let http = response as? HTTPURLResponse else { throw APIError(status: 503, message: "The server returned an invalid response.") }
        guard (200..<300).contains(http.statusCode) else {
            let serverMessage = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw APIError(status: http.statusCode, message: serverMessage ?? String(data: data, encoding: .utf8) ?? "Request failed.")
        }
        do { return try decoder.decode(T.self, from: data) }
        catch { throw APIError(status: 500, message: "The server response could not be read: \(error.localizedDescription)") }
    }

    private func encoded(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))) ?? value
    }
}
