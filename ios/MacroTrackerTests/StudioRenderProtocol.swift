import Foundation

/// Synthetic, immediate render responses. No network or save request is used.
/// This is UI-fixture verification, not live-backend verification.
final class StudioRenderProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url else { return }
        guard request.httpMethod == "GET" else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }
        let body: String
        switch url.path {
        case "/api/chat/history":
            body = url.host == "studio-chat-render.invalid"
                ? #"{"messages":[{"role":"user","content":"Help me review this chicken bowl."},{"role":"assistant","content":"Harvest chicken bowl\n520 kcal · Protein 32 g · Carbs 58 g · Fat 18 g\n\nReview your portion before confirming. This simulator example is not saved."}]}"#
                : #"{"messages":[]}"#
        case "/api/today":
            body = #"{"date":"2026-11-16","day_label":"Today","totals":{"calories":1200,"protein":90,"carbs":110,"fat":40,"fiber":18},"targets":{"calories":2100,"protein":175,"carbs":210,"fat":70,"fiber":30},"remaining":{"calories":900,"protein":85,"carbs":100,"fat":30,"fiber":12},"meals":[]}"#
        case "/api/presets": body = #"{"presets":[]}"#
        case "/api/brief": body = #"{"date":"2026-11-16","text":null}"#
        case "/api/exercises": body = #"{"exercises":[]}"#
        case "/api/plan": body = #"{"rotation":["Legs"],"upcoming":[{"type":"Legs","exercises":[{"name":"Barbell Squat"}]}],"core":[],"has_plan":true}"#
        case "/api/workout-stats":
            body = #"{"today":{"date":"2026-11-16","entries":0,"exercises":[]},"week":{"week_start":"2026-11-16","week_label":"This week","days_logged":0,"total_sets":0,"total_volume":0,"streak_weeks":0},"coverage":{"muscle_groups":{},"workout_types":{},"untouched":[]},"prs":[],"plan":{"today":{"type":"Legs","exercises":[]},"next":[]}}"#
        case let path where path.hasPrefix("/api/workouts/"):
            body = #"{"date":"2026-11-16","day_label":"Today","workouts":[]}"#
        default:
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }
        guard let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: ["Content-Type": "application/json"]) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
