import AVFoundation
import Foundation

enum LiveCoachPlaybackEnqueueResult: Equatable {
    case accepted(generation: Int, becameActive: Bool)
    case flushed(generation: Int)
}

struct LiveCoachPlaybackQueueState {
    let capacity: Int
    private(set) var pendingCount = 0
    private var generation = 0

    init(capacity: Int) {
        self.capacity = max(1, capacity)
    }

    mutating func enqueue() -> LiveCoachPlaybackEnqueueResult {
        if pendingCount >= capacity {
            generation += 1
            pendingCount = 1
            return .flushed(generation: generation)
        }
        let becameActive = pendingCount == 0
        pendingCount += 1
        return .accepted(generation: generation, becameActive: becameActive)
    }

    mutating func complete(generation completedGeneration: Int) -> Bool {
        guard completedGeneration == generation, pendingCount > 0 else { return false }
        pendingCount -= 1
        return pendingCount == 0
    }

    mutating func reset() {
        generation += 1
        pendingCount = 0
    }
}

@MainActor
struct SystemMicrophonePermission: LiveCoachPermissionChecking {
    func requestPermission() async -> Bool {
        switch AVAudioApplication.shared.recordPermission {
        case .granted:
            return true
        case .denied:
            return false
        case .undetermined:
            return await withCheckedContinuation { continuation in
                AVAudioApplication.requestRecordPermission { granted in
                    continuation.resume(returning: granted)
                }
            }
        @unknown default:
            return false
        }
    }
}

enum LiveCoachAudioError: Error {
    case unsupportedFormat
    case invalidPCM
}

private final class LiveCoachMuteState: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    func set(_ value: Bool) {
        lock.lock()
        self.value = value
        lock.unlock()
    }

    func get() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

@MainActor
final class LiveCoachAudioEngine: LiveCoachAudioHandling {
    private(set) var capturedAudio: AsyncStream<Data>
    private(set) var playbackActivity: AsyncStream<Bool>
    private(set) var lifecycleEvents: AsyncStream<LiveCoachAudioLifecycleEvent>

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let audioSession = AVAudioSession.sharedInstance()
    private var capturedContinuation: AsyncStream<Data>.Continuation
    private var playbackContinuation: AsyncStream<Bool>.Continuation
    private var lifecycleContinuation: AsyncStream<LiveCoachAudioLifecycleEvent>.Continuation
    private let muteState = LiveCoachMuteState()
    private var playbackQueue = LiveCoachPlaybackQueueState(capacity: 24)
    private var tapInstalled = false
    private var playerConnected = false
    private var notificationTokens: [NSObjectProtocol] = []
    private var observationGeneration = UUID()
    private var targetFormat: AVAudioFormat? {
        AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: 24_000,
            channels: 1,
            interleaved: false
        )
    }

    init() {
        (capturedAudio, capturedContinuation) = AsyncStream<Data>.makeStream(
            bufferingPolicy: .bufferingNewest(12)
        )
        (playbackActivity, playbackContinuation) = AsyncStream<Bool>.makeStream(
            bufferingPolicy: .bufferingNewest(4)
        )
        (lifecycleEvents, lifecycleContinuation) = AsyncStream<LiveCoachAudioLifecycleEvent>.makeStream(
            bufferingPolicy: .bufferingNewest(2)
        )
        engine.attach(player)
    }

    func start() throws {
        guard !engine.isRunning,
              let targetFormat else { throw LiveCoachAudioError.unsupportedFormat }
        try audioSession.setCategory(
            .playAndRecord,
            mode: .voiceChat,
            options: [.defaultToSpeaker, .allowBluetoothHFP]
        )
        try audioSession.setPreferredSampleRate(48_000)
        try audioSession.setPreferredIOBufferDuration(0.02)
        try audioSession.setActive(true)

        try rebuildGraph(targetFormat: targetFormat)
        engine.prepare()
        try engine.start()
        observeAudioSessionChanges()
    }

    func play(_ data: Data) throws {
        guard !data.isEmpty,
              data.count <= LiveCoachWireCodec.maximumAudioBytes,
              data.count.isMultiple(of: MemoryLayout<Int16>.size),
              let targetFormat,
              let buffer = AVAudioPCMBuffer(
                pcmFormat: targetFormat,
                frameCapacity: AVAudioFrameCount(data.count / MemoryLayout<Int16>.size)
              ),
              let destination = buffer.mutableAudioBufferList.pointee.mBuffers.mData else {
            throw LiveCoachAudioError.invalidPCM
        }
        data.copyBytes(
            to: destination.assumingMemoryBound(to: UInt8.self),
            count: data.count
        )
        buffer.frameLength = buffer.frameCapacity

        let enqueue = playbackQueue.enqueue()
        let generation: Int
        switch enqueue {
        case .accepted(let currentGeneration, let becameActive):
            generation = currentGeneration
            if becameActive { playbackContinuation.yield(true) }
        case .flushed(let currentGeneration):
            generation = currentGeneration
            player.stop()
            playbackContinuation.yield(false)
            playbackContinuation.yield(true)
        }
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self else { return }
                if self.playbackQueue.complete(generation: generation) {
                    self.playbackContinuation.yield(false)
                }
            }
        }
        if !player.isPlaying { player.play() }
    }

    func setMuted(_ muted: Bool) {
        muteState.set(muted)
    }

    func recoverFromConfigurationChange() throws {
        guard !audioSession.currentRoute.inputs.isEmpty,
              !audioSession.currentRoute.outputs.isEmpty,
              let targetFormat else { throw LiveCoachAudioError.unsupportedFormat }
        stopPlaybackForRouteSafety()
        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        engine.stop()
        if playerConnected {
            engine.disconnectNodeOutput(player)
            playerConnected = false
        }
        engine.reset()
        do {
            try rebuildGraph(targetFormat: targetFormat)
            engine.prepare()
            try engine.start()
        } catch {
            if tapInstalled {
                engine.inputNode.removeTap(onBus: 0)
                tapInstalled = false
            }
            engine.stop()
            throw error
        }
    }

    func stop() {
        muteState.set(false)
        observationGeneration = UUID()
        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        engine.stop()
        player.stop()
        playbackQueue.reset()
        playbackContinuation.yield(false)
        notificationTokens.forEach(NotificationCenter.default.removeObserver)
        notificationTokens.removeAll()
        try? audioSession.setActive(false, options: .notifyOthersOnDeactivation)
        replaceSessionStreams()
    }

    private func rebuildGraph(targetFormat: AVAudioFormat) throws {
        let input = engine.inputNode
        if !input.isVoiceProcessingEnabled {
            try input.setVoiceProcessingEnabled(true)
        }
        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0,
              inputFormat.channelCount > 0,
              let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
            throw LiveCoachAudioError.unsupportedFormat
        }
        if !playerConnected {
            engine.connect(player, to: engine.mainMixerNode, format: targetFormat)
            playerConnected = true
        }
        let continuation = capturedContinuation
        let muteState = muteState
        input.installTap(onBus: 0, bufferSize: 2_400, format: inputFormat) { buffer, _ in
            guard !muteState.get() else { return }
            let ratio = targetFormat.sampleRate / inputFormat.sampleRate
            let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * ratio)) + 32
            guard let output = AVAudioPCMBuffer(
                pcmFormat: targetFormat,
                frameCapacity: capacity
            ) else { return }
            var supplied = false
            var conversionError: NSError?
            let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
                if supplied {
                    inputStatus.pointee = .noDataNow
                    return nil
                }
                supplied = true
                inputStatus.pointee = .haveData
                return buffer
            }
            guard conversionError == nil,
                  status != .error,
                  output.frameLength > 0,
                  let bytes = output.audioBufferList.pointee.mBuffers.mData else { return }
            let count = Int(output.frameLength) * MemoryLayout<Int16>.size
            guard count <= LiveCoachWireCodec.maximumAudioBytes else { return }
            continuation.yield(Data(bytes: bytes, count: count))
        }
        tapInstalled = true
    }

    private func stopPlaybackForRouteSafety() {
        player.stop()
        playbackQueue.reset()
        playbackContinuation.yield(false)
    }

    private func replaceSessionStreams() {
        capturedContinuation.finish()
        playbackContinuation.finish()
        lifecycleContinuation.finish()
        (capturedAudio, capturedContinuation) = AsyncStream<Data>.makeStream(
            bufferingPolicy: .bufferingNewest(12)
        )
        (playbackActivity, playbackContinuation) = AsyncStream<Bool>.makeStream(
            bufferingPolicy: .bufferingNewest(4)
        )
        (lifecycleEvents, lifecycleContinuation) = AsyncStream<LiveCoachAudioLifecycleEvent>.makeStream(
            bufferingPolicy: .bufferingNewest(2)
        )
    }

    private func observeAudioSessionChanges() {
        guard notificationTokens.isEmpty else { return }
        let center = NotificationCenter.default
        let lifecycleContinuation = lifecycleContinuation
        let observationGeneration = UUID()
        self.observationGeneration = observationGeneration
        notificationTokens.append(center.addObserver(
            forName: AVAudioSession.routeChangeNotification,
            object: audioSession,
            queue: .main
        ) { [weak self] notification in
            let rawReason = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            Task { @MainActor [weak self] in
                guard let self,
                      self.observationGeneration == observationGeneration else { return }
                let reason = AVAudioSession.RouteChangeReason(rawValue: rawReason ?? 0)
                let change = LiveCoachAudioRouteChange(
                    reason: Self.routeReason(reason),
                    hasInput: !self.audioSession.currentRoute.inputs.isEmpty,
                    hasOutput: !self.audioSession.currentRoute.outputs.isEmpty
                )
                if change.reason == .oldDeviceUnavailable || !change.hasOutput {
                    self.stopPlaybackForRouteSafety()
                }
                lifecycleContinuation.yield(.routeEvaluated(change))
            }
        })
        notificationTokens.append(center.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: audioSession,
            queue: .main
        ) { [weak self] notification in
            let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            Task { @MainActor [weak self] in
                guard let self,
                      self.observationGeneration == observationGeneration,
                      raw == AVAudioSession.InterruptionType.began.rawValue else { return }
                self.stopPlaybackForRouteSafety()
                lifecycleContinuation.yield(.interrupted)
            }
        })
        notificationTokens.append(center.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self,
                      self.observationGeneration == observationGeneration else { return }
                self.stopPlaybackForRouteSafety()
                lifecycleContinuation.yield(.engineConfigurationChanged(
                    hasInput: !self.audioSession.currentRoute.inputs.isEmpty,
                    hasOutput: !self.audioSession.currentRoute.outputs.isEmpty,
                    engineRunning: self.engine.isRunning
                ))
            }
        })
    }

    private static func routeReason(
        _ reason: AVAudioSession.RouteChangeReason?
    ) -> LiveCoachAudioRouteChangeReason {
        switch reason {
        case .newDeviceAvailable: .newDeviceAvailable
        case .oldDeviceUnavailable: .oldDeviceUnavailable
        case .categoryChange: .categoryChange
        case .override: .override
        case .wakeFromSleep: .wakeFromSleep
        case .noSuitableRouteForCategory: .noSuitableRouteForCategory
        case .routeConfigurationChange: .routeConfigurationChange
        default: .unknown
        }
    }
}
