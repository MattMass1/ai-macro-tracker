import AVFoundation
import SwiftUI
import UIKit

/// Native barcode camera: EAN-13 / EAN-8 / UPC-E (UPC-A is reported as EAN-13).
struct BarcodeScannerView: View {
    @Binding var paused: Bool
    var onCode: (String) -> Void
    var onClose: () -> Void
    var injectedCode: String? = nil

    @StateObject private var controller = BarcodeCaptureController()
    @State private var torchOn = false
    @State private var permissionDenied = false

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if permissionDenied {
                permissionView
            } else if let message = controller.errorMessage {
                cameraErrorView(message)
            } else {
                CameraPreview(session: controller.session)
                    .ignoresSafeArea()
                scanFrameOverlay
            }

            VStack(spacing: 0) {
                topBar
                Spacer()
            }
            .safeAreaPadding(.top, 8)
        }
        .onAppear { bindCallbacks() }
        .task { await boot() }
        .onDisappear {
            torchOn = false
            controller.setTorch(false)
            controller.stop()
        }
        .onChange(of: paused) { _, isPaused in
            if isPaused {
                torchOn = false
                controller.freeze()
            } else {
                torchOn = false
                controller.resume()
            }
        }
    }

    private var topBar: some View {
        HStack {
            chromeButton("xmark", label: "Close") { onClose() }
            Spacer()
            if controller.hasTorch {
                chromeButton(torchOn ? "flashlight.on.fill" : "flashlight.off.fill", label: torchOn ? "Torch off" : "Torch on") {
                    torchOn.toggle()
                    controller.setTorch(torchOn)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 14)
    }

    private var scanFrameOverlay: some View {
        GeometryReader { geometry in
            let width = max(0, min(280, geometry.size.width - 48))
            let height: CGFloat = 168
            let rect = CGRect(
                x: (geometry.size.width - width) / 2,
                y: (geometry.size.height - height) / 2 - 24,
                width: width,
                height: height
            )
            ZStack {
                Path { path in
                    path.addRect(CGRect(origin: .zero, size: geometry.size))
                    path.addRoundedRect(in: rect, cornerSize: CGSize(width: 18, height: 18))
                }
                .fill(Color.black.opacity(0.45), style: FillStyle(eoFill: true))

                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .stroke(Theme.accent, lineWidth: 3)
                    .frame(width: width, height: height)
                    .position(x: rect.midX, y: rect.midY)
            }
            .allowsHitTesting(false)
        }
    }

    private var permissionView: some View {
        VStack(spacing: 12) {
            Image(systemName: "camera.fill").font(.title).foregroundStyle(Theme.accent)
            Text("Camera access is off").font(.headline).foregroundStyle(.white)
            Text("Enable camera in Settings to scan barcodes.")
                .font(.subheadline)
                .foregroundStyle(.white.opacity(0.8))
                .multilineTextAlignment(.center)
            if let url = URL(string: UIApplication.openSettingsURLString) {
                Button("Open Settings") { UIApplication.shared.open(url) }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.accentTint)
            }
        }
        .padding(28)
    }

    private func cameraErrorView(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "barcode.viewfinder").font(.title).foregroundStyle(Theme.accent)
            Text("Camera unavailable").font(.headline).foregroundStyle(.white)
            Text(message).font(.subheadline).foregroundStyle(.white.opacity(0.8)).multilineTextAlignment(.center)
        }
        .padding(28)
    }

    private func chromeButton(_ systemImage: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 40, height: 40)
                .background(.ultraThinMaterial, in: Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    private func bindCallbacks() {
        controller.onCode = { code in
            guard !paused else { return }
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            paused = true
            onCode(code)
        }
    }

    private func boot() async {
        bindCallbacks()
        #if DEBUG
        if let injectedCode {
            paused = true
            onCode(injectedCode)
            return
        }
        #endif
        let granted = await BarcodeCaptureController.requestCameraAccess()
        guard granted else {
            permissionDenied = true
            return
        }
        controller.start()
    }
}

private enum BarcodeCaptureError: Error {
    case noCamera
}

private final class BarcodeCaptureController: NSObject, ObservableObject, AVCaptureMetadataOutputObjectsDelegate {
    let session = AVCaptureSession()
    @Published var hasTorch = false
    @Published var errorMessage: String?

    var onCode: ((String) -> Void)?

    private let sessionQueue = DispatchQueue(label: "com.biz21.macrotracker.barcode")
    private let output = AVCaptureMetadataOutput()
    private var device: AVCaptureDevice?
    private var configured = false
    private var locked = false
    private var lastCode: String?

    static func requestCameraAccess() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            return true
        case .notDetermined:
            return await AVCaptureDevice.requestAccess(for: .video)
        default:
            return false
        }
    }

    func start() {
        sessionQueue.async { [weak self] in
            guard let self else { return }
            do {
                try self.configureIfNeeded()
            } catch {
                DispatchQueue.main.async { self.errorMessage = "This device cannot scan barcodes." }
                return
            }
            self.lastCode = nil
            self.locked = false
            if !self.session.isRunning {
                self.session.startRunning()
            }
        }
    }

    func stop() {
        sessionQueue.async { [weak self] in
            guard let self else { return }
            self.setTorchLocked(false)
            if self.session.isRunning {
                self.session.stopRunning()
            }
        }
    }

    func freeze() {
        sessionQueue.async { [weak self] in
            guard let self else { return }
            self.locked = true
            self.setTorchLocked(false)
            if self.session.isRunning {
                self.session.stopRunning()
            }
        }
    }

    func resume() {
        start()
    }

    func setTorch(_ on: Bool) {
        sessionQueue.async { [weak self] in
            self?.setTorchLocked(on)
        }
    }

    func metadataOutput(
        _ output: AVCaptureMetadataOutput,
        didOutput metadataObjects: [AVMetadataObject],
        from connection: AVCaptureConnection
    ) {
        guard !locked else { return }
        guard let object = metadataObjects.first as? AVMetadataMachineReadableCodeObject,
              let value = object.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty
        else { return }
        guard value != lastCode else { return }
        lastCode = value
        locked = true
        DispatchQueue.main.async { [weak self] in
            self?.onCode?(value)
        }
    }

    deinit {
        if session.isRunning {
            session.stopRunning()
        }
    }

    private func configureIfNeeded() throws {
        guard !configured else { return }
        guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
            throw BarcodeCaptureError.noCamera
        }
        let input = try AVCaptureDeviceInput(device: camera)
        session.beginConfiguration()
        session.sessionPreset = .high
        guard session.canAddInput(input) else {
            session.commitConfiguration()
            throw BarcodeCaptureError.noCamera
        }
        session.addInput(input)
        guard session.canAddOutput(output) else {
            session.commitConfiguration()
            throw BarcodeCaptureError.noCamera
        }
        session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: sessionQueue)
        let wanted: [AVMetadataObject.ObjectType] = [.ean13, .ean8, .upce]
        output.metadataObjectTypes = wanted.filter { output.availableMetadataObjectTypes.contains($0) }
        session.commitConfiguration()
        device = camera
        configured = true
        let torch = camera.hasTorch
        DispatchQueue.main.async { self.hasTorch = torch }
    }

    private func setTorchLocked(_ on: Bool) {
        guard let device, device.hasTorch else { return }
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            if on, device.isTorchModeSupported(.on) {
                try device.setTorchModeOn(level: 1.0)
            } else if device.isTorchModeSupported(.off) {
                device.torchMode = .off
            }
        } catch {
            // Torch is optional; scanning still works without it.
        }
    }
}

private struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspectFill
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {
        uiView.previewLayer.session = session
    }

    final class PreviewView: UIView {
        let previewLayer = AVCaptureVideoPreviewLayer()

        override init(frame: CGRect) {
            super.init(frame: frame)
            backgroundColor = .black
            previewLayer.videoGravity = .resizeAspectFill
            layer.addSublayer(previewLayer)
        }

        required init?(coder: NSCoder) {
            return nil
        }

        override func layoutSubviews() {
            super.layoutSubviews()
            previewLayer.frame = bounds
            if let connection = previewLayer.connection, connection.isVideoRotationAngleSupported(90) {
                connection.videoRotationAngle = 90
            }
        }
    }
}
