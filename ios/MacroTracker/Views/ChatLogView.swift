import SwiftUI
import UIKit

struct ChatMessage: Identifiable {
    let id = UUID()
    let role: Role
    let text: String
    var widget: ChatWidget? = nil
    var showsExercisePicker = false
    var loggedMeals: [LoggedMeal] = []

    enum Role { case user, assistant }
}

struct ChatLogView: View {
    let scanFoodTrigger: Int

    init(scanFoodTrigger: Int = 0) {
        self.scanFoodTrigger = scanFoodTrigger
    }

    @EnvironmentObject private var store: AppStore
    @EnvironmentObject private var auth: AuthService
    @State private var messages: [ChatMessage] = []
    @State private var input = ""
    @State private var showSignOutConfirm = false
    @State private var isSending = false
    @State private var showScanSheet = false
    @State private var showManual = false
    @State private var showVoiceCoach = false
    @State private var loggerSelection: WorkoutLoggerSelection?
    @State private var showExerciseLibrary = false
    @State private var swapTargetMessageId: UUID?
    @State private var pendingSwap: WorkoutLoggerSelection?
    @State private var showOnboardingPickerSheet = false
    @State private var pendingOnboardingMetrics: PendingOnboardingMetrics?
    @State private var didSubmitOnboardingMetrics = false
    @State private var didOfferExercisePicker = false
    @State private var didCompleteExercisePicker = false
    @State private var handledScanFoodTrigger = 0
    @FocusState private var inputFocused: Bool

    private struct PendingOnboardingMetrics {
        let summary: String
        let metrics: ChatMetrics
    }

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
                                    if message.role == .assistant, message.showsExercisePicker {
                                        ExercisePickerPromptCard(
                                            isSaving: isSending,
                                            onChoose: { showOnboardingPickerSheet = true },
                                            onSkip: { Task { await skipExercisePicker() } }
                                        )
                                    }
                                    if message.role == .assistant, !message.loggedMeals.isEmpty {
                                        LoggedMealsCard(meals: message.loggedMeals)
                                    }
                                }
                                .id(message.id)
                            }
                            if isSending { TypingBubble() }
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
        .sheet(isPresented: $showVoiceCoach) {
            LiveCoachView(refreshData: { await store.refreshAfterVoice() })
        }
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
        .sheet(isPresented: $showOnboardingPickerSheet) {
            ExerciseLibraryView(allowsMultipleSelection: true) { exercises in
                showOnboardingPickerSheet = false
                Task { await confirmOnboardingExercises(exercises) }
            }
        }
        .sheet(isPresented: $showScanSheet) {
            ScanFoodSheet(
                onLogged: { name, calories in
                    messages.append(ChatMessage(role: .assistant, text: "Logged \(name), \(Int(calories)) kcal."))
                }
            )
        }
        .onChange(of: scanFoodTrigger) { _, _ in handleScanFoodTrigger() }
        .onAppear {
            handleScanFoodTrigger()
        }
        .task { await loadChatHistory() }
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
            Button {
                dismissKeyboard()
                showVoiceCoach = true
            } label: {
                Image(systemName: "waveform")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(Theme.accent)
                    .frame(width: 40, height: 40)
                    .background(Theme.accentTint, in: Circle())
            }
            .accessibilityLabel("Open Voice Coach")
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
                        showScanSheet = true
                    }
                } label: { Image(systemName: "barcode.viewfinder").font(.body).foregroundStyle(Theme.accent).frame(width: 42, height: 42).background(Theme.accentTint, in: Circle()) }
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
                messages.append(ChatMessage(
                    role: .assistant,
                    text: response.reply,
                    widget: response.widget,
                    loggedMeals: response.logged ?? []
                ))
                // The coach can log food or write targets/plan on any turn.
                await store.loadDay()
                if response.hasPlan == true || response.hasTargets == true { await store.loadWorkoutData() }
                maybeOfferExercisePicker(after: message, reply: response)
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
        pendingOnboardingMetrics = PendingOnboardingMetrics(
            summary: summary,
            metrics: ChatMetrics(
                heightCm: values.numbers["height_cm"],
                weightKg: values.numbers["weight_kg"],
                goalWeightKg: values.numbers["goal_weight_kg"],
                age: values.numbers["age"].map { Int($0.rounded()) },
                activityLevel: values.texts["activity_level"]
            )
        )
        didSubmitOnboardingMetrics = true
        offerExercisePicker(
            prompt: "Got your numbers. Pick the exercises you want in the plan.",
            presentSheet: true
        )
        return true
    }

    private func dismissWidget(_ id: UUID) {
        guard let index = messages.firstIndex(where: { $0.id == id }) else { return }
        messages[index].widget = nil
    }

    private func maybeOfferExercisePicker(after userMessage: String, reply: ChatReply) {
        guard !didCompleteExercisePicker, !didOfferExercisePicker else { return }
        if userMessage.localizedCaseInsensitiveContains("these are my exercises:") { return }
        if looksLikeMetricsMessage(userMessage) { didSubmitOnboardingMetrics = true }
        // `loadDay()` may have just written targets, which flips `needsOnboarding` off.
        let stillOnboarding = needsOnboarding || auth.freshClaim || didSubmitOnboardingMetrics
        guard stillOnboarding else { return }
        let basicsReady = didSubmitOnboardingMetrics || reply.hasTargets == true
        guard basicsReady else { return }
        offerExercisePicker(
            prompt: "Pick the exercises you want in the plan.",
            presentSheet: true
        )
    }

    private func offerExercisePicker(prompt: String, presentSheet: Bool) {
        guard !didCompleteExercisePicker else { return }
        didOfferExercisePicker = true
        if !messages.contains(where: { $0.showsExercisePicker }) {
            messages.append(ChatMessage(role: .assistant, text: prompt, showsExercisePicker: true))
        }
        if presentSheet { showOnboardingPickerSheet = true }
    }

    private func dismissExercisePickerCard() {
        if let index = messages.firstIndex(where: { $0.showsExercisePicker }) {
            messages[index].showsExercisePicker = false
        }
    }

    private func skipExercisePicker() async {
        didCompleteExercisePicker = true
        showOnboardingPickerSheet = false
        dismissExercisePickerCard()
        if let pending = pendingOnboardingMetrics {
            pendingOnboardingMetrics = nil
            await sendChat(pending.summary, metrics: pending.metrics)
        }
    }

    private func confirmOnboardingExercises(_ exercises: [LibraryExercise]) async {
        guard !exercises.isEmpty else { return }
        didCompleteExercisePicker = true
        dismissExercisePickerCard()
        let plan = WorkoutPlanWrite.fromPickedExercises(exercises)
        var savedOnServer = false
        if let plan {
            savedOnServer = await store.savePlan(plan, reportError: false)
        }
        let exerciseLine = onboardingExerciseMessage(exercises: exercises, savedOnServer: savedOnServer)
        var message = exerciseLine
        var metrics: ChatMetrics?
        if let pending = pendingOnboardingMetrics {
            let combined = pending.summary + ". " + exerciseLine
            message = combined.count <= 1000
                ? combined
                : pending.summary + ". I picked my exercises in the library picker. Keep that plan."
            metrics = pending.metrics
            pendingOnboardingMetrics = nil
        }
        await sendChat(message, metrics: metrics)
        // Coach may auto-assign on this turn; write the user's picks last so they win.
        if savedOnServer, let plan {
            _ = await store.savePlan(plan, reportError: false)
        }
    }

    private func onboardingExerciseMessage(exercises: [LibraryExercise], savedOnServer: Bool) -> String {
        let names = exercises.map(\.name)
        let preview = names.prefix(8).joined(separator: ", ")
        if savedOnServer {
            if names.count <= 8 {
                return "These are my exercises: \(preview). Keep this plan."
            }
            return "These are my \(names.count) exercises, including \(preview). Keep the plan I saved."
        }
        if names.count <= 8 {
            return "These are my exercises: \(preview). Use these for my workout plan."
        }
        return "These are my exercises: \(preview), and \(names.count - 8) more. Use these for my workout plan."
    }

    private func looksLikeMetricsMessage(_ text: String) -> Bool {
        let lower = text.lowercased()
        if lower.hasPrefix("my metrics:") { return true }
        return lower.contains("height") && lower.contains("weight")
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

    private func loadChatHistory() async {
        do {
            let history = try await store.chatHistory(limit: 100)
            guard messages.isEmpty else { return }
            let restored = history.compactMap { item -> ChatMessage? in
                let role: ChatMessage.Role
                switch item.role {
                case "user": role = .user
                case "assistant": role = .assistant
                default: return nil
                }
                return ChatMessage(role: role, text: item.content)
            }
            if restored.isEmpty {
                seedGreeting()
            } else {
                messages = restored
            }
        } catch {
            seedGreeting()
        }
    }

    private var needsOnboarding: Bool {
        if auth.freshClaim { return true }
        if let targets = store.day?.targets { return targets.calories <= 0 }
        return false
    }

    private func handleScanFoodTrigger() {
        guard scanFoodTrigger > handledScanFoodTrigger else { return }
        handledScanFoodTrigger = scanFoodTrigger
        dismissKeyboard()
        showScanSheet = true
    }
}

private struct ChatBubble: View {
    let message: ChatMessage
    var body: some View { HStack { if message.role == .user { Spacer(minLength: 52) }; Text(message.text).font(.subheadline).padding(.horizontal, 14).padding(.vertical, 11).background(message.role == .user ? Theme.accentTint : Theme.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous)).foregroundStyle(Theme.ink).shadow(color: message.role == .assistant ? .black.opacity(0.04) : .clear, radius: 4, y: 1); if message.role == .assistant { Spacer(minLength: 52) } } }
}
private struct LoggedMealsCard: View {
    let meals: [LoggedMeal]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Logged", systemImage: "checkmark.circle.fill")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Theme.accent)
            ForEach(Array(meals.enumerated()), id: \.offset) { _, meal in
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(meal.name)
                        .font(.subheadline)
                        .foregroundStyle(Theme.ink)
                    Spacer(minLength: 12)
                    Text("\(Int(meal.calories.rounded())) kcal")
                        .font(.subheadline.weight(.semibold))
                        .monospacedDigit()
                        .foregroundStyle(Theme.ink)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .appCard(padding: 14)
    }
}
private struct TypingBubble: View {
    @State private var pulse = false
    var body: some View { HStack { HStack(spacing: 5) { ForEach(0..<3) { index in Circle().fill(Color.secondary).frame(width: 5, height: 5).opacity(pulse ? 0.3 : 1).animation(.easeInOut(duration: 0.7).repeatForever().delay(Double(index) * 0.15), value: pulse) } }.padding(14).background(Theme.surface, in: Capsule()); Spacer() }.onAppear { pulse = true } }
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
