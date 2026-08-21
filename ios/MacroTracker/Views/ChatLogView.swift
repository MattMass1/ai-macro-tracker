import PhotosUI
import SwiftUI
import UIKit

struct ChatMessage: Identifiable {
    let id = UUID()
    let role: Role
    let text: String
    var widget: ChatWidget? = nil

    enum Role { case user, assistant }
}

struct ChatLogView: View {
    @EnvironmentObject private var store: AppStore
    @EnvironmentObject private var auth: AuthService
    @State private var messages: [ChatMessage] = []
    @State private var input = ""
    @State private var showSignOutConfirm = false
    @State private var isSending = false
    @State private var photoItem: PhotosPickerItem?
    @State private var image: UIImage?
    @State private var vision: VisionPayload?
    @State private var isAnalyzing = false
    @State private var analysisGeneration = 0
    @State private var showCamera = false
    @State private var showScanSheet = false
    @State private var scanMode: ScanFoodMode = .barcode
    @State private var showManual = false
    @State private var loggerSelection: WorkoutLoggerSelection?
    @State private var showExerciseLibrary = false
    @State private var swapTargetMessageId: UUID?
    @State private var pendingSwap: WorkoutLoggerSelection?
    @FocusState private var inputFocused: Bool

    var body: some View {
        ZStack { Theme.canvas.ignoresSafeArea()
            VStack(spacing: 0) {
                coachHeader
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 10) {
                            ForEach(messages) { message in
                                VStack(alignment: .leading, spacing: 10) {
                                    if !message.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                                        ChatBubble(message: message)
                                    }
                                    if message.role == .assistant, let widget = message.widget, widget.type == "metrics_form" {
                                        MetricsFormCard(
                                            fields: widget.fields,
                                            isSaving: isSending,
                                            onSave: { values in
                                                Task {
                                                    let saved = await submitMetrics(fields: widget.fields, values: values)
                                                    if saved { dismissWidget(message.id) }
                                                }
                                            },
                                            onDismiss: { dismissWidget(message.id) }
                                        )
                                    }
                                    if message.role == .assistant, let widget = message.widget, widget.type == "exercise_card" {
                                        ExerciseVideoCard(
                                            exercise: widget.exercise ?? ExerciseCardExercise(),
                                            onLogSet: { loggerSelection = $0 },
                                            onSwap: {
                                                swapTargetMessageId = message.id
                                                showExerciseLibrary = true
                                            }
                                        )
                                    }
                                }
                                .id(message.id)
                            }
                            if isSending { TypingBubble() }
                            if let image { VisionCard(image: image, result: vision, analyzing: isAnalyzing, onLog: logVision, onCancel: clearVision) }
                            Color.clear.frame(height: 1).id("end")
                        }.padding(16)
                    }
                    .scrollDismissesKeyboard(.interactively)
                    .onChange(of: messages.count) { _, _ in withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
                }
                composer
            }
        }
        .navigationBarHidden(true)
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { dismissKeyboard() }
                    .fontWeight(.semibold)
            }
        }
        .sheet(isPresented: $showManual) { ManualFoodView() }
        .sheet(item: $loggerSelection) { selection in
            WorkoutLoggerView(initialType: selection.type, initialExercise: selection.exercise)
        }
        .sheet(isPresented: $showExerciseLibrary, onDismiss: {
            if let selection = pendingSwap, let id = swapTargetMessageId {
                applyExerciseSwap(messageId: id, selection: selection)
            }
            pendingSwap = nil
            swapTargetMessageId = nil
        }) {
            ExerciseLibraryView { selection in
                pendingSwap = selection
                showExerciseLibrary = false
            }
        }
        .sheet(isPresented: $showCamera) { CameraPicker(image: $image) }
        .sheet(isPresented: $showScanSheet, onDismiss: { scanMode = .barcode }) {
            if scanMode == .photo {
                CameraPicker(image: $image)
            } else {
                ScanFoodSheet(
                    onChoosePhoto: { scanMode = .photo },
                    onLogged: { name, calories in
                        messages.append(ChatMessage(role: .assistant, text: "Logged \(name), \(Int(calories)) kcal."))
                    }
                )
            }
        }
        .onChange(of: imageIdentity) { old, new in
            guard old != new else { return }
            vision = nil
            analysisGeneration += 1
            let generation = analysisGeneration
            guard new != nil else { return }
            Task { await analyzeImage(generation: generation) }
        }
        .onChange(of: photoItem) { _, item in
            vision = nil
            analysisGeneration += 1
            Task { await loadPhoto(item) }
        }
        .onAppear { seedGreeting() }
        .confirmationDialog("Sign out?", isPresented: $showSignOutConfirm, titleVisibility: .visible) {
            Button("Sign out", role: .destructive) { auth.signOut() }
        } message: {
            Text("You'll need a new invite code to sign back in.")
        }
    }

    private var coachHeader: some View {
        HStack(spacing: 11) {
            Menu {
                Button("Sign out", systemImage: "rectangle.portrait.and.arrow.right", role: .destructive) { showSignOutConfirm = true }
            } label: {
                Image(systemName: "figure.mind.and.body")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                    .frame(width: 44, height: 44)
                    .background(Theme.accentTint, in: Circle())
            }
            VStack(alignment: .leading, spacing: 2) {
                Text("Coach").font(.headline).foregroundStyle(Theme.ink)
                HStack(spacing: 5) { Circle().fill(Theme.accent).frame(width: 7, height: 7); Text("Online").font(.caption).foregroundStyle(Theme.muted) }
            }
            Spacer()
            Button("Manual", systemImage: "slider.horizontal.3") { showManual = true }.font(.caption.weight(.semibold)).foregroundStyle(Theme.accent)
        }.padding(.horizontal, 16).padding(.vertical, 10).background(Theme.surface).overlay(alignment: .bottom) { Divider().overlay(Theme.divider) }
    }

    private var composer: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(spacing: 9) {
                Menu {
                    Button("Scan Barcode", systemImage: "barcode.viewfinder") {
                        dismissKeyboard()
                        scanMode = .barcode
                        showScanSheet = true
                    }
                    Button("Take Photo", systemImage: "camera") { showCamera = true }
                    PhotosPicker(selection: $photoItem, matching: .images) { Label("Choose Photo", systemImage: "photo") }
                } label: { Image(systemName: "camera.fill").font(.body).foregroundStyle(Theme.accent).frame(width: 42, height: 42).background(Theme.accentTint, in: Circle()) }
                TextField("Message Coach", text: $input, axis: .vertical).lineLimit(1...4).focused($inputFocused).padding(.horizontal, 14).padding(.vertical, 11).background(Theme.input, in: RoundedRectangle(cornerRadius: 18))
                    .submitLabel(.send)
                    .onSubmit { if !input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { Task { await send() } } }
                Button { Task { await send() } } label: { Image(systemName: "arrow.up").fontWeight(.bold).frame(width: 44, height: 44).background(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Color.secondary.opacity(0.14) : Theme.accent, in: Circle()).foregroundStyle(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Color.secondary : .white) }.disabled(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isSending)
            }.padding(12).background(Theme.surface)
        }
    }

    private func send() async {
        let message = input.trimmingCharacters(in: .whitespacesAndNewlines); guard !message.isEmpty else { return }
        input = ""
        inputFocused = false  // Dismiss the keyboard after sending
        await sendChat(message)
    }

    private func dismissKeyboard() {
        inputFocused = false
        UIApplication.shared.sendAction(
            #selector(UIResponder.resignFirstResponder),
            to: nil,
            from: nil,
            for: nil
        )
    }

    @discardableResult
    private func sendChat(_ message: String, metrics: ChatMetrics? = nil) async -> Bool {
        messages.append(ChatMessage(role: .user, text: message))
        isSending = true
        defer { isSending = false }
        // Render free tier cold-starts can take 30-60s; retry the server-waking case.
        for attempt in 0...2 {
            do {
                let response = try await store.chat(message, metrics: metrics)
                messages.append(ChatMessage(role: .assistant, text: response.reply, widget: response.widget))
                // The coach can log food or write targets/plan on any turn.
                await store.loadDay()
                if response.hasPlan == true || response.hasTargets == true { await store.loadWorkoutData() }
                return true
            } catch let error as APIError where error.status == 429 {
                messages.append(ChatMessage(role: .assistant, text: "You're at today's limit, ask Matt to raise it."))
                return false
            } catch {
                let text = error.localizedDescription
                let waking = text.lowercased().contains("waking") || text.lowercased().contains("try again") || text.lowercased().contains("timeout")
                if waking && attempt < 2 {
                    try? await Task.sleep(nanoseconds: 3_000_000_000)  // 3s, let the server finish waking
                    continue
                }
                messages.append(ChatMessage(role: .assistant, text: text))
                return false
            }
        }
        return false
    }

    private func submitMetrics(fields: [MetricsField], values: MetricsFieldValues) async -> Bool {
        let summary = metricsSummary(fields: fields, values: values)
        return await sendChat(summary, metrics: ChatMetrics(
            heightCm: values.numbers["height_cm"],
            weightKg: values.numbers["weight_kg"],
            goalWeightKg: values.numbers["goal_weight_kg"],
            age: values.numbers["age"].map { Int($0.rounded()) },
            activityLevel: values.texts["activity_level"]
        ))
    }

    private func dismissWidget(_ id: UUID) {
        guard let index = messages.firstIndex(where: { $0.id == id }) else { return }
        messages[index].widget = nil
    }

    private func applyExerciseSwap(messageId: UUID, selection: WorkoutLoggerSelection) {
        guard let index = messages.firstIndex(where: { $0.id == messageId }) else { return }
        guard case .exerciseCard(var card) = messages[index].widget else { return }
        card.exercise.name = selection.exercise
        card.exercise.workoutType = selection.type
        if let muscle = selection.muscleGroup { card.exercise.muscleGroup = muscle }
        if let equipment = selection.equipment { card.exercise.equipment = equipment }
        card.exercise.videoUrl = nil
        messages[index].widget = .exerciseCard(card)
    }

    private func metricsSummary(fields: [MetricsField], values: MetricsFieldValues) -> String {
        let parts: [String]
        if fields.isEmpty {
            let numbers = values.numbers.keys.sorted().compactMap { key -> String? in
                guard let value = values.numbers[key] else { return nil }
                return Self.usMetricSummary(key: key, value: value)
            }
            let texts = values.texts.keys.sorted().compactMap { key -> String? in
                guard let text = values.texts[key] else { return nil }
                return "\(key) \(text)"
            }
            parts = numbers + texts
        } else {
            parts = fields.compactMap { field in
                if field.isNumeric {
                    guard let value = values.numbers[field.key] else { return nil }
                    return Self.usMetricSummary(key: field.key, value: value)
                }
                guard let text = values.texts[field.key] else { return nil }
                return "\(MetricsField.defaultLabel(for: field.key)) \(text)"
            }
        }
        return "My metrics: " + parts.joined(separator: ", ")
    }

    private static func formatMetric(_ value: Double) -> String {
        value.rounded() == value ? String(Int(value)) : String(value)
    }

    private static func usMetricSummary(key: String, value: Double) -> String {
        switch key {
        case "height_cm":
            let totalInches = Int((value / 2.54).rounded())
            return "Height \(totalInches / 12) ft \(totalInches % 12) in"
        case "weight_kg":
            return "Weight \(formatPounds(value * 2.2046226218)) lb"
        case "goal_weight_kg":
            return "Goal weight \(formatPounds(value * 2.2046226218)) lb"
        case "age":
            return "Age \(formatMetric(value))"
        default:
            return "\(MetricsField.defaultLabel(for: key)) \(formatMetric(value))"
        }
    }

    private static func formatPounds(_ value: Double) -> String {
        let rounded = (value * 10).rounded() / 10
        return rounded.rounded() == rounded ? String(Int(rounded)) : String(format: "%.1f", rounded)
    }

    private func seedGreeting() {
        guard messages.isEmpty else { return }
        if needsOnboarding {
            messages.append(ChatMessage(role: .assistant, text: "Welcome! What should I call you?"))
        } else {
            messages.append(ChatMessage(role: .assistant, text: "Tell me what you ate and I'll log it."))
        }
    }

    private var needsOnboarding: Bool {
        if auth.freshClaim { return true }
        if let targets = store.day?.targets { return targets.calories <= 0 }
        return false
    }

    private var imageIdentity: ObjectIdentifier? { image.map { ObjectIdentifier($0) } }

    private func loadPhoto(_ item: PhotosPickerItem?) async {
        guard let item else { return }
        guard let data = try? await item.loadTransferable(type: Data.self), let picked = UIImage(data: data) else { return }
        guard photoItem == item else { return }
        image = picked
    }
    private func analyzeImage(generation: Int) async {
        guard generation == analysisGeneration, let data = image?.resizedJPEG(maxDimension: 1800, quality: 0.72) else { return }
        isAnalyzing = true; vision = nil
        defer { if generation == analysisGeneration { isAnalyzing = false } }
        do {
            let result = try await store.analyze(image: "data:image/jpeg;base64,\(data.base64EncodedString())")
            guard generation == analysisGeneration else { return }
            vision = result
        } catch {
            guard generation == analysisGeneration else { return }
            messages.append(ChatMessage(role: .assistant, text: error.localizedDescription)); clearVision()
        }
    }
    private func logVision() {
        guard let vision else { return }
        Task {
            let body = LogMealBody(name: vision.name, calories: vision.calories, protein: vision.protein, carbs: vision.carbs, fat: vision.fat, fiber: vision.fiber, macroSource: "Vision model (estimated from food photo)", meal: vision.meal, day: store.isToday ? nil : store.dateString)
            if await store.logMeal(body) { messages.append(ChatMessage(role: .assistant, text: "Logged \(vision.name), \(Int(vision.calories)) kcal.")); clearVision() }
        }
    }
    private func clearVision() { analysisGeneration += 1; image = nil; vision = nil; photoItem = nil; isAnalyzing = false }
}

private struct ChatBubble: View {
    let message: ChatMessage
    var body: some View { HStack { if message.role == .user { Spacer(minLength: 52) }; Text(message.text).font(.subheadline).padding(.horizontal, 14).padding(.vertical, 11).background(message.role == .user ? Theme.accentTint : Theme.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous)).foregroundStyle(Theme.ink).shadow(color: message.role == .assistant ? .black.opacity(0.04) : .clear, radius: 4, y: 1); if message.role == .assistant { Spacer(minLength: 52) } } }
}
private struct TypingBubble: View {
    @State private var pulse = false
    var body: some View { HStack { HStack(spacing: 5) { ForEach(0..<3) { index in Circle().fill(Color.secondary).frame(width: 5, height: 5).opacity(pulse ? 0.3 : 1).animation(.easeInOut(duration: 0.7).repeatForever().delay(Double(index) * 0.15), value: pulse) } }.padding(14).background(Theme.surface, in: Capsule()); Spacer() }.onAppear { pulse = true } }
}
private struct VisionCard: View {
    let image: UIImage; let result: VisionPayload?; let analyzing: Bool; let onLog: () -> Void; let onCancel: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Image(uiImage: image).resizable().scaledToFill().frame(height: 180).frame(maxWidth: .infinity).clipped().clipShape(RoundedRectangle(cornerRadius: 15))
            if analyzing { Label("Reading the plate…", systemImage: "sparkle.magnifyingglass").font(.subheadline).foregroundStyle(.secondary) }
            if let result {
                Text(result.name).font(.headline)
                Text("\(Int(result.calories)) kcal  ·  \(Int(result.protein))p  ·  \(Int(result.carbs))c  ·  \(Int(result.fat))f").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                Text(result.note).font(.caption).foregroundStyle(.secondary)
                HStack { Button("Log this", systemImage: "checkmark") { onLog() }.buttonStyle(.borderedProminent); Button("Cancel", action: onCancel).buttonStyle(.bordered) }
            }
        }.appCard()
    }
}

struct ManualFoodView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""; @State private var meal = "Snack"; @State private var calories = ""; @State private var protein = ""; @State private var carbs = ""; @State private var fat = ""; @State private var fiber = ""
    @FocusState private var isInputFocused: Bool
    let meals = ["Breakfast", "Lunch", "Dinner", "Snack"]
    var body: some View {
        NavigationStack {
            Form {
                Section("Food") { TextField("Name", text: $name).focused($isInputFocused); Picker("Meal", selection: $meal) { ForEach(meals, id: \.self) { Text($0) } } }
                Section("Macros") { numberField("Calories", $calories); numberField("Protein (g)", $protein); numberField("Carbs (g)", $carbs); numberField("Fat (g)", $fat); numberField("Fiber (g)", $fiber) }
                Section { Text("Enter the label values as written. You can delete the entry from Today if anything needs correcting.").font(.caption).foregroundStyle(.secondary) }
            }
            .scrollDismissesKeyboard(.interactively)
            .navigationTitle("Manual entry")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Log") { submit() }.disabled(name.trimmingCharacters(in: .whitespaces).isEmpty) }
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { isInputFocused = false }
                }
            }
        }.presentationDetents([.medium, .large])
    }
    private func numberField(_ title: String, _ value: Binding<String>) -> some View { TextField(title, text: value).keyboardType(.decimalPad).focused($isInputFocused) }
    private func submit() { isInputFocused = false; Task { let body = LogMealBody(name: name, calories: Double(calories) ?? 0, protein: Double(protein) ?? 0, carbs: Double(carbs) ?? 0, fat: Double(fat) ?? 0, fiber: Double(fiber) ?? 0, macroSource: "Manual iOS entry", meal: meal, day: store.isToday ? nil : store.dateString); if await store.logMeal(body) { dismiss() } } }
}

private struct CameraPicker: UIViewControllerRepresentable {
    @Environment(\.dismiss) private var dismiss; @Binding var image: UIImage?
    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeUIViewController(context: Context) -> UIImagePickerController { let picker = UIImagePickerController(); picker.sourceType = UIImagePickerController.isSourceTypeAvailable(.camera) ? .camera : .photoLibrary; picker.delegate = context.coordinator; return picker }
    func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}
    final class Coordinator: NSObject, UINavigationControllerDelegate, UIImagePickerControllerDelegate { let parent: CameraPicker; init(_ parent: CameraPicker) { self.parent = parent }; func imagePickerController(_ picker: UIImagePickerController, didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) { parent.image = info[.originalImage] as? UIImage; parent.dismiss() }; func imagePickerControllerDidCancel(_ picker: UIImagePickerController) { parent.dismiss() } }
}

private extension UIImage {
    func resizedJPEG(maxDimension: CGFloat, quality: CGFloat) -> Data? {
        let scale = min(1, maxDimension / max(size.width, size.height)); let target = CGSize(width: size.width * scale, height: size.height * scale)
        return UIGraphicsImageRenderer(size: target).image { _ in draw(in: CGRect(origin: .zero, size: target)) }.jpegData(compressionQuality: quality)
    }
}
