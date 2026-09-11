import AVFoundation
import SwiftUI
import UIKit

/// Native barcode camera: EAN-13 / EAN-8 / UPC-E (UPC-A is reported as EAN-13).
struct BarcodeScannerView: View {
    @Binding var paused: Bool
    var onCode: (String) -> Void
    var onClose: () -> Void

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
        let granted = await BarcodeCaptureController.requestCameraAccess()
        guard granted else {
            permissionDenied = true
            return
        }
        controller.start()
    }
}

struct ScanFoodSheet: View {
    var onLogged: (String, Double) -> Void

    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var paused = false
    @State private var product: BarcodeFoodPayload?
    @State private var isLookingUp = false
    @State private var isLogging = false
    @State private var notFound = false
    @State private var statusMessage: String?
    @State private var gramsText = ""
    @FocusState private var gramsFocused: Bool

    var body: some View {
        ZStack {
            Theme.canvas.ignoresSafeArea()
            if let product {
                productCard(product)
            } else {
                scannerStack
            }
        }
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { gramsFocused = false }
                    .fontWeight(.semibold)
            }
        }
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
    }

    private var scannerStack: some View {
        ZStack(alignment: .top) {
            BarcodeScannerView(paused: $paused, onCode: handleCode, onClose: { dismiss() })
            VStack(spacing: 14) {
                if isLookingUp {
                    lookupBanner(text: "Looking up product…", progress: true)
                } else if notFound {
                    fallbackBanner
                } else if let statusMessage {
                    lookupBanner(text: statusMessage, progress: false)
                }
            }
            .safeAreaPadding(.top, 8)
            .padding(.top, 58)
            .padding(.horizontal, 16)
        }
    }

    private func lookupBanner(text: String, progress: Bool) -> some View {
        HStack(spacing: 10) {
            if progress { ProgressView().tint(Theme.accent) }
            Text(text).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
            Spacer(minLength: 0)
            if !progress {
                Button("Scan again") { resetScan() }
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.accent)
            }
        }
        .padding(14)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .shadow(color: .black.opacity(0.08), radius: 10, y: 4)
    }

    private var fallbackBanner: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("We could not find that barcode.")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Theme.ink)
            Text("Try scanning again or enter the food manually.")
                .font(.caption)
                .foregroundStyle(Theme.muted)
            Button("Scan again") { resetScan() }
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.accent)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Theme.accentTint, in: Capsule())
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .shadow(color: .black.opacity(0.08), radius: 10, y: 4)
    }

    private func productCard(_ product: BarcodeFoodPayload) -> some View {
        VStack(spacing: 0) {
            HStack {
                Button { dismiss() } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Theme.ink)
                        .frame(width: 36, height: 36)
                        .background(Theme.input, in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Close")
                Spacer()
                Text("Barcode")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.ink)
                Spacer()
                Color.clear.frame(width: 36, height: 36)
            }
            .padding(.horizontal, 16)
            .safeAreaPadding(.top, 8)
            .padding(.top, 12)

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 8) {
                        SectionLabel(text: "Scanned product")
                        Text(product.name)
                            .font(.system(size: 22, weight: .bold))
                            .foregroundStyle(Theme.ink)
                        Text("per 100g")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Theme.muted)
                        Text("\(Int(product.calories.rounded())) kcal")
                            .font(.system(size: 28, weight: .heavy).monospacedDigit())
                            .foregroundStyle(Theme.ink)
                        HStack(spacing: 8) {
                            macroChip("Protein", product.protein, Theme.protein)
                            macroChip("Carbs", product.carbs, Theme.carbs)
                            macroChip("Fat", product.fat, Theme.fat)
                            macroChip("Fiber", product.fiber, Theme.fiber)
                        }
                        if let serving = product.servingSize, !serving.isEmpty {
                            Text("Serving size \(serving)")
                                .font(.caption)
                                .foregroundStyle(Theme.muted)
                        }
                        Text(product.source)
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(Theme.muted)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .appCard()

                    VStack(alignment: .leading, spacing: 12) {
                        SectionLabel(text: "Portion")
                        Button {
                            Task { await logProduct(product, grams: nil) }
                        } label: {
                            groupLabel(isLogging ? nil : logOneServingTitle(product))
                        }
                        .disabled(isLogging)
                        .opacity(isLogging ? 0.6 : 1)

                        Text("How many grams?")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.ink)
                        TextField("Grams", text: $gramsText)
                            .keyboardType(.decimalPad)
                            .focused($gramsFocused)
                            .padding(14)
                            .background(Theme.input, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        Button {
                            guard let grams = parsedGrams else { return }
                            Task { await logProduct(product, grams: grams) }
                        } label: {
                            groupLabel(isLogging ? nil : "Log grams")
                        }
                        .disabled(isLogging || parsedGrams == nil)
                        .opacity(isLogging || parsedGrams == nil ? 0.45 : 1)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .appCard()
                }
                .padding(16)
                .padding(.bottom, 24)
            }
            .scrollDismissesKeyboard(.interactively)
        }
    }

    private func macroChip(_ title: String, _ value: Double, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title.uppercased())
                .font(.system(size: 9, weight: .bold))
                .tracking(0.4)
                .foregroundStyle(Theme.muted)
            Text("\(Int(value.rounded()))g")
                .font(.caption.weight(.bold).monospacedDigit())
                .foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 8)
        .padding(.horizontal, 8)
        .background(Theme.canvas, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func groupLabel(_ title: String?) -> some View {
        Group {
            if let title {
                Text(title).fontWeight(.bold)
            } else {
                ProgressView().tint(.white)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 14)
        .background(Theme.accent, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .foregroundStyle(.white)
    }

    private var parsedGrams: Double? {
        let value = Double(gramsText.replacingOccurrences(of: ",", with: "."))
        guard let value, value > 0 else { return nil }
        return value
    }

    private func logOneServingTitle(_ product: BarcodeFoodPayload) -> String {
        if product.macrosPerServing != nil || !(product.servingSize ?? "").isEmpty {
            return "Log 1 serving"
        }
        return "Log 1 unit"
    }

    private func handleCode(_ code: String) {
        Task { await lookup(code) }
    }

    private func lookup(_ code: String) async {
        isLookingUp = true
        notFound = false
        statusMessage = nil
        defer { isLookingUp = false }
        do {
            product = try await store.barcodeFood(code: code)
        } catch let error as APIError where error.status == 404 {
            notFound = true
        } catch {
            statusMessage = error.localizedDescription
        }
    }

    private func resetScan() {
        gramsFocused = false
        product = nil
        notFound = false
        statusMessage = nil
        gramsText = ""
        paused = false
    }

    private func logProduct(_ product: BarcodeFoodPayload, grams: Double?) async {
        gramsFocused = false
        let calories: Double
        let protein: Double
        let carbs: Double
        let fat: Double
        let fiber: Double
        if let grams {
            let factor = grams / 100
            calories = product.calories * factor
            protein = product.protein * factor
            carbs = product.carbs * factor
            fat = product.fat * factor
            fiber = product.fiber * factor
        } else if let serving = product.macrosPerServing {
            calories = serving.calories
            protein = serving.protein
            carbs = serving.carbs
            fat = serving.fat
            fiber = serving.fiber
        } else {
            calories = product.calories
            protein = product.protein
            carbs = product.carbs
            fat = product.fat
            fiber = product.fiber
        }
        let name: String
        if let grams {
            name = "\(product.name) (\(Self.formatGrams(grams))g)"
        } else {
            name = product.name
        }
        let source = product.source.trimmingCharacters(in: .whitespacesAndNewlines)
        let body = LogMealBody(
            name: name,
            calories: calories,
            protein: protein,
            carbs: carbs,
            fat: fat,
            fiber: fiber,
            macroSource: source.isEmpty ? "Barcode lookup" : source,
            meal: "Snack",
            day: store.isToday ? nil : store.dateString
        )
        isLogging = true
        defer { isLogging = false }
        if await store.logMeal(body) {
            onLogged(name, calories)
            dismiss()
        }
    }

    private static func formatGrams(_ value: Double) -> String {
        value.rounded() == value ? String(Int(value)) : String(format: "%g", value)
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
            if let connection = previewLayer.connection, connection.isVideoOrientationSupported {
                connection.videoOrientation = .portrait
            }
        }
    }
}
