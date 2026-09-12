import Foundation

enum LiveCoachWireError: Error {
    case invalidEvent
}

enum LiveCoachWireCodec {
    static let maximumEventBytes = 65_536
    static let maximumAudioBytes = 24_000

    static func decode(_ data: Data) throws -> LiveCoachServerEvent? {
        guard data.count <= maximumEventBytes,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = object["type"] as? String else {
            throw LiveCoachWireError.invalidEvent
        }
        switch type {
        case "session.started":
            return .started
        case "session.input_transcript.delta", "session.output_transcript.delta":
            guard let delta = object["delta"] as? String else { return nil }
            let bounded = String(delta.prefix(4_096))
            return type == "session.input_transcript.delta"
                ? .inputTranscript(bounded)
                : .outputTranscript(bounded)
        case "session.output_audio.delta":
            guard let encoded = object["delta"] as? String,
                  encoded.count <= maximumEventBytes,
                  let audio = Data(base64Encoded: encoded),
                  !audio.isEmpty,
                  audio.count <= maximumAudioBytes,
                  audio.count.isMultiple(of: 2) else { return nil }
            return .outputAudio(audio)
        case "session.input_audio.muted":
            return .inputMuted(true)
        case "session.input_audio.unmuted":
            return .inputMuted(false)
        case "coach.activity":
            guard let stateRaw = object["state"] as? String,
                  let activityState = LiveCoachActivityState(rawValue: stateRaw),
                  let label = object["label"] as? String else { return nil }
            return .activity(state: activityState, label: String(label.prefix(160)))
        case "coach.meal_committed":
            guard let operationID = object["operation_id"] as? String,
                  let rawLabel = object["label"] as? String,
                  let dayTotalObject = object["day_total"] as? [String: Any],
                  let dayTotalData = try? JSONSerialization.data(withJSONObject: dayTotalObject),
                  let dayTotal = try? JSONDecoder().decode(MacroTotals.self, from: dayTotalData) else { return nil }
            let label = rawLabel.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !operationID.isEmpty, !label.isEmpty else { return nil }
            return .mealCommitted(
                operationID: operationID,
                label: String(label.prefix(120)),
                dayTotal: dayTotal
            )
        case "session.closed":
            let allowed = ["close_requested", "expired", "content", "remote_hangup", "connection_lost", "ended"]
            let reason = object["reason"] as? String
            return .closed(reason: reason.flatMap { allowed.contains($0) ? $0 : nil } ?? "ended")
        case "error":
            guard let code = object["code"] as? String,
                  let message = object["message"] as? String else { return nil }
            return .failure(
                code: String(code.prefix(64)),
                message: String(message.prefix(240))
            )
        default:
            return nil
        }
    }

    static func encodeAudio(_ audio: Data) throws -> Data {
        guard !audio.isEmpty,
              audio.count <= maximumAudioBytes,
              audio.count.isMultiple(of: 2) else {
            throw LiveCoachWireError.invalidEvent
        }
        return try encode([
            "type": "session.input_audio.append",
            "audio": audio.base64EncodedString(),
        ])
    }

    static func encodeMute(_ muted: Bool) throws -> Data {
        try encode([
            "type": muted ? "session.input_audio.mute" : "session.input_audio.unmute",
        ])
    }

    static func encodeClose() throws -> Data {
        try encode(["type": "session.close"])
    }

    private static func encode(_ event: [String: String]) throws -> Data {
        let data = try JSONSerialization.data(withJSONObject: event)
        guard data.count <= maximumEventBytes else { throw LiveCoachWireError.invalidEvent }
        return data
    }
}

actor LiveCoachCloseRace {
    private var result: Bool?
    private var waiters: [CheckedContinuation<Bool, Never>] = []

    func resolve(_ result: Bool) {
        guard self.result == nil else { return }
        self.result = result
        let pending = waiters
        waiters.removeAll()
        pending.forEach { $0.resume(returning: result) }
    }

    func value() async -> Bool {
        if let result { return result }
        return await withCheckedContinuation { waiters.append($0) }
    }
}

enum LiveCoachCloseDeadline {
    static func wait<Success>(
        for operation: Task<Success, Never>,
        timeout: Duration
    ) async -> Bool {
        let race = LiveCoachCloseRace()
        Task {
            _ = await operation.value
            await race.resolve(true)
        }
        Task {
            try? await Task.sleep(for: timeout)
            await race.resolve(false)
        }
        return await race.value()
    }
}

@MainActor
final class LiveCoachWebSocketTransport: NSObject, LiveCoachTransporting {
    private let baseURL: URL?
    private let tokenProvider: () -> String?
    private var redirectDelegate: LiveCoachNoRedirectDelegate?
    private var session: URLSession?
    private var socket: URLSessionWebSocketTask?
    private var receiverTask: Task<Void, Never>?
    private var eventContinuation: AsyncThrowingStream<LiveCoachServerEvent, Error>.Continuation?
    private var receivedClosed = false

    init(baseURL: URL? = Config.apiURL, tokenProvider: @escaping () -> String? = { KeychainStore.deviceToken }) {
        self.baseURL = baseURL
        self.tokenProvider = tokenProvider
    }

    static func makeRequest(baseURL: URL?, token: String?) throws -> URLRequest {
        guard let baseURL,
              var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              scheme == "https" || scheme == "http",
              components.host != nil,
              components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil else {
            throw LiveCoachConnectionError(
                message: "The server address is not configured.",
                retryable: false
            )
        }
        let cleanToken = token?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !cleanToken.isEmpty else {
            throw LiveCoachConnectionError(
                message: "Sign in again to use voice coaching.",
                retryable: false
            )
        }
        let endpoint = baseURL
            .appendingPathComponent("api", isDirectory: true)
            .appendingPathComponent("live-coach", isDirectory: false)
        components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false) ?? components
        components.scheme = scheme == "https" ? "wss" : "ws"
        components.query = nil
        components.fragment = nil
        components.user = nil
        components.password = nil
        guard let url = components.url else {
            throw LiveCoachConnectionError(
                message: "The server address is not configured.",
                retryable: false
            )
        }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 15)
        request.setValue("Bearer \(cleanToken)", forHTTPHeaderField: "Authorization")
        return request
    }

    func connect() async throws -> AsyncThrowingStream<LiveCoachServerEvent, Error> {
        let request = try Self.makeRequest(baseURL: baseURL, token: tokenProvider())
        receivedClosed = false
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.urlCredentialStorage = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.waitsForConnectivity = false
        configuration.timeoutIntervalForRequest = 15
        configuration.timeoutIntervalForResource = 620
        let delegate = LiveCoachNoRedirectDelegate()
        let session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
        let socket = session.webSocketTask(with: request)
        redirectDelegate = delegate
        self.session = session
        self.socket = socket
        socket.resume()

        do {
            var didStart = false
            for _ in 0..<8 where !didStart {
                let message = try await Self.receive(socket, timeout: 15)
                guard let event = try LiveCoachWireCodec.decode(Self.data(from: message)) else { continue }
                switch event {
                case .started:
                    didStart = true
                case .failure(let code, let message):
                    throw LiveCoachConnectionError(
                        message: message,
                        retryable: !["not_configured", "provider_access"].contains(code)
                    )
                case .closed:
                    throw LiveCoachConnectionError(
                        message: "The voice session ended before it started.",
                        retryable: true
                    )
                default:
                    continue
                }
            }
            guard didStart else {
                throw LiveCoachConnectionError(
                    message: "Voice coach did not finish connecting. Try again.",
                    retryable: true
                )
            }
        } catch let error as LiveCoachConnectionError {
            cleanUpSocket()
            throw error
        } catch {
            cleanUpSocket()
            throw LiveCoachConnectionError(
                message: "Voice coach could not connect. Try again.",
                retryable: true
            )
        }

        let pair = AsyncThrowingStream<LiveCoachServerEvent, Error>.makeStream(
            bufferingPolicy: .bufferingNewest(32)
        )
        eventContinuation = pair.continuation
        receiverTask = Task { [weak self] in
            await self?.receiveEvents(from: socket)
        }
        return pair.stream
    }

    func sendAudio(_ data: Data) async throws {
        guard let socket else { throw LiveCoachWireError.invalidEvent }
        let event = try LiveCoachWireCodec.encodeAudio(data)
        try await socket.send(.string(String(decoding: event, as: UTF8.self)))
    }

    func setMuted(_ muted: Bool) async throws {
        guard let socket else { throw LiveCoachWireError.invalidEvent }
        let event = try LiveCoachWireCodec.encodeMute(muted)
        try await socket.send(.string(String(decoding: event, as: UTF8.self)))
    }

    func close() async {
        guard let socket else { return }
        let event = try? LiveCoachWireCodec.encodeClose()
        let sendTask = Task {
            guard let event else { return }
            try? await socket.send(.string(String(decoding: event, as: UTF8.self)))
        }
        let sendFinished = await LiveCoachCloseDeadline.wait(
            for: sendTask,
            timeout: .milliseconds(500)
        )
        if sendFinished {
            let deadline = ContinuousClock.now.advanced(by: .milliseconds(1_500))
            while !receivedClosed && ContinuousClock.now < deadline {
                try? await Task.sleep(for: .milliseconds(25))
            }
        }
        sendTask.cancel()
        cleanUpSocket()
    }

    private func receiveEvents(from socket: URLSessionWebSocketTask) async {
        do {
            while !Task.isCancelled {
                let message = try await socket.receive()
                guard let event = try LiveCoachWireCodec.decode(Self.data(from: message)) else { continue }
                if case .started = event { continue }
                eventContinuation?.yield(event)
                if case .closed = event {
                    receivedClosed = true
                    eventContinuation?.finish()
                    cleanUpSocket()
                    return
                }
            }
        } catch {
            if !Task.isCancelled {
                eventContinuation?.finish(throwing: LiveCoachConnectionError(
                    message: "Voice coach lost the connection. Try again.",
                    retryable: true
                ))
            }
        }
    }

    private func cleanUpSocket() {
        receiverTask?.cancel()
        receiverTask = nil
        eventContinuation?.finish()
        eventContinuation = nil
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
        session?.invalidateAndCancel()
        session = nil
        redirectDelegate = nil
    }

    private static func data(from message: URLSessionWebSocketTask.Message) -> Data {
        switch message {
        case .data(let data): data
        case .string(let text): Data(text.utf8)
        @unknown default: Data()
        }
    }

    private static func receive(
        _ socket: URLSessionWebSocketTask,
        timeout: TimeInterval
    ) async throws -> URLSessionWebSocketTask.Message {
        try await withThrowingTaskGroup(of: URLSessionWebSocketTask.Message.self) { group in
            group.addTask { try await socket.receive() }
            group.addTask {
                try await Task.sleep(for: .seconds(timeout))
                throw LiveCoachConnectionError(
                    message: "Voice coach did not finish connecting. Try again.",
                    retryable: true
                )
            }
            guard let first = try await group.next() else {
                throw LiveCoachWireError.invalidEvent
            }
            group.cancelAll()
            return first
        }
    }
}

private final class LiveCoachNoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}
