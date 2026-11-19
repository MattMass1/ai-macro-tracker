#if DEBUG
import Foundation

// Explicitly synthetic, network-free UI acceptance. Never installed in Release.
final class OfflineFixtureProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url?.path ?? ""
        var status = 200
        let text: String
        switch path {
        case "/api/today": text = Self.day
        case "/api/presets": text = #"{"presets":[{"name":"Synthetic shake","emoji":"🥛","calories":150,"protein":30,"carbs":3,"fat":2.5,"fiber":0,"meal":"Snack","sort_order":1},{"name":"Synthetic bowl","emoji":"🌯","calories":625,"protein":75,"carbs":38,"fat":22,"fiber":0,"meal":"Lunch","sort_order":2}]}"#
        case "/api/log-preset": text = Self.day
        case let route where route.hasPrefix("/api/meal/") && route.hasSuffix("/repeat"): text = Self.day
        case let route where route.hasPrefix("/api/meal/") && route.hasSuffix("/save-preset"): text = #"{"presets":[{"name":"Synthetic shake","emoji":"🥛","calories":150,"protein":30,"carbs":3,"fat":2.5,"fiber":0,"meal":"Snack","sort_order":1},{"name":"Synthetic bowl","emoji":"🌯","calories":625,"protein":75,"carbs":38,"fat":22,"fiber":0,"meal":"Lunch","sort_order":2},{"name":"Synthetic eggs","emoji":"🍽️","calories":140,"protein":12,"carbs":1,"fat":10,"fiber":0,"meal":"Breakfast","sort_order":3}]}"#
        case "/api/trends": text = #"{"days":[{"date":"2026-11-16","day_label":"Mon","calories":100,"protein":0,"carbs":0,"fat":0,"target_calories":2000,"target_protein":150,"macros_complete":false}],"weekly":[],"weight":{"current_kg":0,"goal_kg":0}}"#
        default:
            if path.hasPrefix("/api/agent-canvas/") && path.hasSuffix("/turn") {
                let body = Self.readBody(request)
                if body["message"] as? String == "00016000275287" {
                    text = #"{"reply":"Synthetic barcode preview. 1 serving, 100 kcal. Log this meal?","approval":null,"quickReplies":["Log it","Cancel"]}"#
                } else if body["message"] as? String == "a banana" {
                    text = #"{"reply":"Synthetic banana preview. 1 banana, 100 kcal. Log this meal?","approval":null,"quickReplies":["Log it","Cancel"]}"#
                } else {
                    text = #"{"reply":"Synthetic reply received. No backend writes occurred.","approval":null,"quickReplies":[]}"#
                }
            } else { status = 400; text = #"{"error":"Unsupported offline fixture route"}"# }
        }
        guard let url = request.url, let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: ["Content-Type":"application/json"]) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(text.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
    private static func readBody(_ request: URLRequest) -> [String: Any] {
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable { let count = stream.read(&bytes, maxLength: bytes.count); if count <= 0 { break }; data.append(contentsOf: bytes.prefix(count)) }
        }
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
    static let day = #"{"date":"2026-11-16","day_label":"Monday","day_timing":{"time_zone":"America/New_York","rollover_hour":4,"effective_date":"2026-11-16"},"has_targets":false,"macros_complete":false,"macro_note":"Synthetic calorie-only fixture. Nutrients unknown.","totals":{"calories":100,"protein":0,"carbs":0,"fat":0,"fiber":0},"targets":{"calories":2000,"protein":150,"carbs":200,"fat":60,"fiber":25},"remaining":{"calories":1900,"protein":150,"carbs":200,"fat":60,"fiber":25},"meals":[{"id":"synthetic-entry","name":"Synthetic calorie-only meal","meal":"Snack","calories":100,"protein":null,"carbs":null,"fat":null,"fiber":null},{"id":"c1000000-0000-4000-8000-000000000001","name":"Synthetic eggs","meal":"Breakfast","calories":140,"protein":12,"carbs":1,"fat":10,"fiber":0}]}"#
}
#endif
