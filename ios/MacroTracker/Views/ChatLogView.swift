import PhotosUI
import SwiftUI
import UIKit

struct ChatMessage: Identifiable { let id = UUID(); let role: Role; let text: String; enum Role { case user, assistant } }

struct ChatLogView: View {
    @EnvironmentObject private var store: AppStore
    @State private var messages = [ChatMessage(role: .assistant, text: "Tell me what you ate and I’ll log it.")]
    @State private var input = ""
    @State private var isSending = false
    @State private var photoItem: PhotosPickerItem?
    @State private var image: UIImage?
    @State private var vision: VisionPayload?
    @State private var isAnalyzing = false
    @State private var analysisGeneration = 0
    @State private var showCamera = false
    @State private var showManual = false
    @FocusState private var inputFocused: Bool

    var body: some View {
        ZStack { Theme.canvas.ignoresSafeArea()
            VStack(spacing: 0) {
                coachHeader
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 10) {
                            ForEach(messages) { message in ChatBubble(message: message).id(message.id) }
                            if isSending { TypingBubble() }
                            if let image { VisionCard(image: image, result: vision, analyzing: isAnalyzing, onLog: logVision, onCancel: clearVision) }
                            Color.clear.frame(height: 1).id("end")
                        }.padding(16)
                    }.onChange(of: messages.count) { _, _ in withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
                }
                composer
            }
        }
        .navigationBarHidden(true)
        .sheet(isPresented: $showManual) { ManualFoodView() }
        .sheet(isPresented: $showCamera) { CameraPicker(image: $image) }
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
    }

    private var coachHeader: some View {
        HStack(spacing: 11) {
            Text("🤖").font(.title3).frame(width: 40, height: 40).background(Theme.accentTint, in: Circle())
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
                    Button("Take Photo", systemImage: "camera") { showCamera = true }
                    PhotosPicker(selection: $photoItem, matching: .images) { Label("Choose Photo", systemImage: "photo") }
                } label: { Image(systemName: "camera.fill").font(.body).foregroundStyle(Theme.accent).frame(width: 42, height: 42).background(Theme.accentTint, in: Circle()) }
                TextField("Message Coach", text: $input, axis: .vertical).lineLimit(1...4).focused($inputFocused).padding(.horizontal, 14).padding(.vertical, 11).background(Theme.input, in: RoundedRectangle(cornerRadius: 18))
                Button { inputFocused = true } label: { Image(systemName: "mic.fill").foregroundStyle(Theme.muted).frame(width: 30, height: 42) }.accessibilityLabel("Use dictation")
                Button { Task { await send() } } label: { Image(systemName: "arrow.up").fontWeight(.bold).frame(width: 44, height: 44).background(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Color.secondary.opacity(0.14) : Theme.accent, in: Circle()).foregroundStyle(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Color.secondary : .white) }.disabled(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isSending)
            }.padding(12).background(Theme.surface)
        }
    }

    private func send() async {
        let message = input.trimmingCharacters(in: .whitespacesAndNewlines); guard !message.isEmpty else { return }
        input = ""; messages.append(ChatMessage(role: .user, text: message)); isSending = true; defer { isSending = false }
        do { let response = try await store.chat(message); messages.append(ChatMessage(role: .assistant, text: response.reply)); if !response.logged.isEmpty { await store.loadDay() } }
        catch { messages.append(ChatMessage(role: .assistant, text: error.localizedDescription)) }
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
            if await store.logMeal(body) { messages.append(ChatMessage(role: .assistant, text: "Logged \(vision.name) — \(Int(vision.calories)) kcal.")); clearVision() }
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

private struct ManualFoodView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""; @State private var meal = "Snack"; @State private var calories = ""; @State private var protein = ""; @State private var carbs = ""; @State private var fat = ""; @State private var fiber = ""
    let meals = ["Breakfast", "Lunch", "Dinner", "Snack"]
    var body: some View {
        NavigationStack {
            Form {
                Section("Food") { TextField("Name", text: $name); Picker("Meal", selection: $meal) { ForEach(meals, id: \.self) { Text($0) } } }
                Section("Macros") { numberField("Calories", $calories); numberField("Protein (g)", $protein); numberField("Carbs (g)", $carbs); numberField("Fat (g)", $fat); numberField("Fiber (g)", $fiber) }
                Section { Text("Enter the label values as written. You can delete the entry from Today if anything needs correcting.").font(.caption).foregroundStyle(.secondary) }
            }.navigationTitle("Manual entry").navigationBarTitleDisplayMode(.inline).toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }; ToolbarItem(placement: .confirmationAction) { Button("Log") { submit() }.disabled(name.trimmingCharacters(in: .whitespaces).isEmpty) } }
        }.presentationDetents([.medium, .large])
    }
    private func numberField(_ title: String, _ value: Binding<String>) -> some View { TextField(title, text: value).keyboardType(.decimalPad) }
    private func submit() { Task { let body = LogMealBody(name: name, calories: Double(calories) ?? 0, protein: Double(protein) ?? 0, carbs: Double(carbs) ?? 0, fat: Double(fat) ?? 0, fiber: Double(fiber) ?? 0, macroSource: "Manual iOS entry", meal: meal, day: store.isToday ? nil : store.dateString); if await store.logMeal(body) { dismiss() } } }
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
