import SwiftUI
import UIKit

/// A chat message in the Coach feed. Besides text, an assistant turn can carry
/// one pending workout change (confirm / cancel) or ask for measurements.
struct CoachMessage: Identifiable {
    let id = UUID()
    let role: Role
    var text: String
    var approval: CoachApproval? = nil
    var showsMetricsForm = false
    /// One-tap answers under this reply; cleared as soon as anything is sent.
    var quickReplies: [String] = []

    enum Role { case user, assistant }
}

/// One conversation in the pixel studio, shared by meal and workout actions.
struct CoachChatView: View {
    @EnvironmentObject private var store: AppStore
    var openWorkouts: () -> Void = {}
    var body: some View { CoachConversationView(chat: store.chat, openWorkouts: openWorkouts) }
}

private struct CoachConversationView: View {
    @EnvironmentObject private var store: AppStore
    @EnvironmentObject private var auth: AuthService
    @ObservedObject var chat: CoachChatStore
    let openWorkouts: () -> Void
    @State private var showFoodDay = false
    private var messages: [CoachMessage] { chat.messages }
    private var showsSummary: Bool {
        messages.isEmpty || (messages.count == 1 && messages.first?.text == CoachChatStore.welcomeText)
    }
    private var input: String {
        get { chat.input }
        nonmutating set { chat.input = newValue }
    }
    private var isSending: Bool { chat.isSending }
    @State private var showSignOutConfirm = false
    private struct ScanPresentation: Identifiable {
        let id = UUID()
        let accountSession: AppStore.AccountSession
        let conversation: CoachChatStore.Context
    }
    @State private var scanPresentation: ScanPresentation?
    @State private var showManual = false
    @FocusState private var inputFocused: Bool

    private static let quickActions: [(label: String, message: String)] = [
        ("What's left today?", "What are my remaining macros for today?"),
        ("Today's workout", "What's my workout today and what's left?"),
        ("Swap today's workout", "I want to change today's workout."),
        ("Tomorrow", "What's tomorrow's workout?"),
    ]

    var body: some View {
        VStack(spacing: 0) {
            GeometryReader { geometry in
                let layout = StudioLayout(width: geometry.size.width, height: geometry.size.height)
                ZStack(alignment: .bottom) {
                    StudioBackground(alignment: .bottom)
                    VStack(spacing: 12) {
                        coachHeader
                        VStack(spacing: 0) {
                            HStack {
                                Text("Coach / meals + workouts")
                                    .font(Theme.pixelFont(StudioLayout.panelTitleSize)).textCase(.uppercase)
                                Spacer()
                                Button { showFoodDay = true } label: {
                                    Image(systemName: "fork.knife").frame(width: 44, height: 44)
                                }
                                .accessibilityLabel("Meals and macros")
                                .accessibilityIdentifier("studio.meals")
                            }
                            .foregroundStyle(Theme.ink)
                            .padding(.horizontal, 14)
                            .background(Theme.accentTint)
                            ScrollViewReader { proxy in
                                ScrollView {
                                    LazyVStack(spacing: 10) {
                                        if showsSummary { studioSummary }
                                        ForEach(showsSummary ? [] : messages) { message in
                                            VStack(alignment: .leading, spacing: 10) {
                                                if !message.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                                                    CoachBubble(message: message)
                                                }
                                                if message.role == .assistant, let approval = message.approval, approval.status != "resolved" {
                                                    ApprovalRow(approval: approval, isBusy: isSending,
                                                                onConfirm: { Task { await respond(to: approval, confirm: true, in: message.id) } },
                                                                onCancel: { Task { await respond(to: approval, confirm: false, in: message.id) } })
                                                }
                                                if message.role == .assistant, !message.quickReplies.isEmpty {
                                                    QuickReplyRow(labels: message.quickReplies, isBusy: isSending) { label in
                                                        Task { await sendChat(label) }
                                                    }
                                                }
                                                if message.role == .assistant, message.showsMetricsForm {
                                                    MetricsFormCard(
                                                        fields: MetricsField.standard,
                                                        isSaving: isSending,
                                                        onSave: { values in
                                                            Task {
                                                                chat.dismissMetricsForm(message.id)
                                                                await sendChat(Self.metricsSummary(fields: MetricsField.standard, values: values))
                                                            }
                                                        },
                                                        onDismiss: { chat.dismissMetricsForm(message.id) }
                                                    )
                                                }
                                            }
                                            .id(message.id)
                                        }
                                        if isSending { TypingBubble() }
                                        quickActionChips
                                        Color.clear.frame(height: 1).id("end")
                                    }.padding(16)
                                }
                                .scrollDismissesKeyboard(.interactively)
                                .onChange(of: messages.count) { _, _ in
                                    if !showsSummary { withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
                                }
                            }
                            FatSecretAttribution().padding(.vertical, 4)
                        }
                        .background(Theme.surface, in: PixelPanelShape())
                        .overlay(PixelPanelShape().stroke(Theme.accentEdge, lineWidth: 2))
                        .padding(.horizontal, 20)
                        .accessibilityIdentifier("studio.conversation")
                        if showsCompanion {
                            CompanionStage(chat: chat, character: character, size: layout.companionSize)
                                .frame(maxWidth: .infinity)
                                .padding(.bottom, 8)
                                .frame(height: layout.floorHeight, alignment: .bottom)
                                .accessibilityIdentifier("studio.companion")
                        }
                    }
                    .padding(.top, 8)
                }
            }
            composer
                .fixedSize(horizontal: false, vertical: true)
        }
        .background(Theme.dock)
        .navigationBarHidden(true)
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { dismissKeyboard() }.fontWeight(.semibold)
            }
        }
        .sheet(isPresented: $showManual) { ManualFoodView() }
        .sheet(isPresented: $showFoodDay) {
            NavigationStack {
                VStack(spacing: 0) {
                    HStack {
                        Text("Meals and macros").font(Theme.pixelFont(26))
                        Spacer()
                        Button("Done") { showFoodDay = false }
                    }
                    .padding().background(Theme.dock)
                    TodayView(onAskCoach: { showFoodDay = false })
                }
            }
        }
        .sheet(item: $scanPresentation) { presentation in
            ScanFoodSheet(accountSession: presentation.accountSession, onLogged: { name, calories in
                chat.recordLoggedMeal(name: name, calories: calories, context: presentation.conversation)
            })
        }
        .task {
            await chat.loadHistory()
            await sendPendingFromHome()
        }
        .onChange(of: store.pendingCoachMessage) { _, _ in Task { await sendPendingFromHome() } }
        .confirmationDialog("Sign out?", isPresented: $showSignOutConfirm, titleVisibility: .visible) {
            Button("Sign out", role: .destructive) { auth.signOut() }
        } message: {
            Text("You'll need a new invite code to sign back in.")
        }
    }

    // MARK: - Pieces

    private var coachHeader: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Your day").font(Theme.pixelFont(StudioLayout.titleSize, relativeTo: .title))
                    .foregroundStyle(Theme.sectionInk)
                Text(store.selectedDate.formatted(.dateTime.weekday(.wide).month(.abbreviated).day()))
                    .font(.caption).foregroundStyle(Theme.muted)
            }
            Spacer()
            Menu {
                Picker("Coach artwork", selection: $characterRaw) {
                    ForEach(CompanionCharacter.allCases) { Text($0.displayName).tag($0.rawValue) }
                }
                Toggle("Show coach artwork", isOn: $showsCompanion)
                Button("Manual food entry", systemImage: "plus") { showManual = true }
                Button("Meals and macros", systemImage: "fork.knife") { showFoodDay = true }
                Button("Sign out", systemImage: "rectangle.portrait.and.arrow.right", role: .destructive) { showSignOutConfirm = true }
            } label: {
                Image(systemName: "slider.horizontal.3")
                    .frame(width: 44, height: 44)
                    .background(Theme.dock, in: PixelPanelShape())
                    .overlay(PixelPanelShape().stroke(Theme.edgeStrong))
            }
            .accessibilityLabel("Coach menu")
        }
        .padding(.leading, 64).padding(.trailing, 24)
    }

    private var studioSummary: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("What are we working on?").font(Theme.pixelFont(StudioLayout.summaryTitleSize, relativeTo: .title2))
            Text("Tell me what you ate, review your day, or plan your next workout.")
                .font(.footnote).foregroundStyle(Theme.muted)
            if let day = store.day {
                Button { showFoodDay = true } label: {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(store.isToday ? "Food today" : "Food log").font(Theme.pixelFont(20))
                            Text("\(day.meals.count) meals logged").font(.caption)
                        }
                        Spacer()
                        Text("\(Int(day.totals.calories.rounded())) kcal").font(Theme.pixelFont(20))
                        Image(systemName: "chevron.right")
                    }
                    .padding(12).background(Theme.accentTint, in: PixelPanelShape())
                }
                .buttonStyle(.plain)
            }
            Button(action: openWorkouts) {
                Label("View workout", systemImage: "dumbbell.fill")
            }
            .buttonStyle(PixelSecondaryButtonStyle(fullWidth: true))
            .accessibilityIdentifier("studio.workout")
        }
        .foregroundStyle(Theme.ink)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// One-tap starters for the things the coach does most; each is just a
    /// message, so there is nothing new to learn.
    private var quickActionChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Self.quickActions, id: \.label) { action in
                    Button(action.label) { Task { await sendChat(action.message) } }
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                        .padding(.horizontal, 10)
                        .frame(minHeight: 44)
                        .background(Theme.accentTint, in: PixelPanelShape())
                        .disabled(isSending)
                }
            }
        }
        .padding(.top, 4)
    }

    private var composer: some View {
        VStack(spacing: 6) {
            HStack {
                Text("Your companion").textCase(.uppercase).font(Theme.pixelFont(StudioLayout.dockLabelSize))
                Spacer()
                Text(chat.isResponding ? "Writing" : "Meals + workouts").font(.caption2)
            }
            .foregroundStyle(Theme.sectionInk)
            HStack(spacing: 4) {
                Menu {
                    Button("Scan Barcode", systemImage: "barcode.viewfinder") {
                        dismissKeyboard()
                        scanPresentation = ScanPresentation(accountSession: store.accountSession, conversation: chat.context)
                    }
                    Button("Manual food entry", systemImage: "plus") { dismissKeyboard(); showManual = true }
                } label: {
                    Text("Ask Coach").font(Theme.pixelFont(19))
                        .foregroundStyle(Theme.accent).frame(minHeight: 44)
                }
                .accessibilityLabel("Meal entry options")
                TextField("Ask, log or adjust", text: $chat.input, axis: .vertical)
                    .font(.footnote)
                    .accessibilityIdentifier("studio.composer")
                    .lineLimit(1...4)
                    .focused($inputFocused)
                    .padding(.vertical, 8)
                    .submitLabel(.send)
                    .onSubmit { if !trimmedInput.isEmpty { Task { await send() } } }
                Button { Task { await send() } } label: {
                    Image(systemName: "arrow.up").font(.subheadline.bold())
                        .frame(width: 44, height: 44)
                        .foregroundStyle(trimmedInput.isEmpty ? Theme.inactive : Theme.accent)
                }
                .disabled(trimmedInput.isEmpty || isSending)
                .accessibilityLabel("Send to Coach")
            }
            .padding(.leading, 10)
            .background(Theme.paper, in: PixelPanelShape())
            .overlay(PixelPanelShape().stroke(Theme.edgeStrong, lineWidth: 1))
            .studioFrame("composer.row")
        }
        .padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 4)
        .background(Theme.dock)
        .overlay(alignment: .top) { Rectangle().fill(Theme.accentEdge).frame(height: 2) }
        .studioFrame("composer")
    }

    private var trimmedInput: String { input.trimmingCharacters(in: .whitespacesAndNewlines) }

    @AppStorage(CompanionPreferences.characterKey) private var characterRaw = CompanionCharacter.male.rawValue
    @AppStorage(CompanionPreferences.visibleKey) private var showsCompanion = true
    private var character: CompanionCharacter { CompanionCharacter(rawValue: characterRaw) ?? .male }

    // MARK: - Actions

    /// A message typed into the home dock is sent exactly once, here.
    private func sendPendingFromHome() async {
        guard let message = store.pendingCoachMessage else { return }
        store.pendingCoachMessage = nil
        await sendChat(message)
    }

    private func send() async {
        let message = trimmedInput
        guard !message.isEmpty else { return }
        input = ""
        inputFocused = false
        await sendChat(message)
    }

    private func dismissKeyboard() {
        inputFocused = false
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }

    private func sendChat(_ message: String) async {
        if await chat.sendMessage(message) { await store.loadDay() }
    }

    private func respond(to approval: CoachApproval, confirm: Bool, in messageID: UUID) async {
        if await chat.respondToApproval(approval, confirm: confirm, in: messageID) {
            await store.loadWorkoutData()
            await store.loadDay()
        }
    }

    // MARK: - Measurements summary (the agent stages set_metrics from plain text)

    static func metricsSummary(fields: [MetricsField], values: MetricsFieldValues) -> String {
        let parts = fields.compactMap { field -> String? in
            if field.isNumeric {
                guard let value = values.numbers[field.key] else { return nil }
                return usMetricSummary(key: field.key, value: value)
            }
            guard let text = values.texts[field.key] else { return nil }
            return "\(MetricsField.defaultLabel(for: field.key)) \(text)"
        }
        return "My metrics: " + parts.joined(separator: ", ")
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
            return "Age \(Int(value.rounded()))"
        default:
            return "\(MetricsField.defaultLabel(for: key)) \(value.rounded() == value ? String(Int(value)) : String(value))"
        }
    }

    private static func formatPounds(_ value: Double) -> String {
        let rounded = (value * 10).rounded() / 10
        return rounded.rounded() == rounded ? String(Int(rounded)) : String(format: "%.1f", rounded)
    }
}

private struct CoachBubble: View {
    let message: CoachMessage
    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 52) }
            Text(message.text)
                .font(.subheadline)
                .padding(.horizontal, 14).padding(.vertical, 11)
                .background(message.role == .user ? Theme.accentTint : Theme.surface,
                            in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
                .foregroundStyle(Theme.ink)
                .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius).stroke(message.role == .user ? Theme.edgeStrong : Theme.edge))
            if message.role == .assistant { Spacer(minLength: 52) }
        }
    }
}

/// The one card that survives from the canvas: a workout change needs a tap.
private struct ApprovalRow: View {
    let approval: CoachApproval
    let isBusy: Bool
    let onConfirm: () -> Void
    let onCancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(approval.title ?? "Confirm this change?")
                .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
            if let detail = approval.detail, !detail.isEmpty {
                Text(detail).font(.footnote).foregroundStyle(Theme.muted)
            }
            HStack(spacing: 10) {
                Button("Confirm", action: onConfirm)
                    .font(.subheadline.weight(.semibold)).foregroundStyle(.white)
                    .padding(.horizontal, 16).padding(.vertical, 9)
                    .background(Theme.accent, in: Capsule())
                Button("Cancel", action: onCancel)
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.accent)
                    .padding(.horizontal, 16).padding(.vertical, 9)
                    .background(Theme.accentTint, in: Capsule())
            }
            .disabled(isBusy)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .appCard(padding: 14)
    }
}

/// One-tap answers under the coach's latest reply. The first is the main
/// action ("Log it"); the rest are outlined. Each tap sends its label.
private struct QuickReplyRow: View {
    let labels: [String]
    let isBusy: Bool
    let onTap: (String) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(labels.enumerated()), id: \.element) { index, label in
                    let primary = index == 0
                    Button(label) { onTap(label) }
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(primary ? Color.white : Theme.accent)
                        .padding(.horizontal, 16).padding(.vertical, 9)
                        .background(primary ? Theme.accent : Theme.accentTint, in: Capsule())
                        .accessibilityHint("Sends \(label)")
                }
            }
        }
        .disabled(isBusy)
    }
}

private struct TypingBubble: View {
    @State private var pulse = false
    var body: some View {
        HStack {
            HStack(spacing: 5) {
                ForEach(0..<3) { index in
                    Circle().fill(Color.secondary).frame(width: 5, height: 5)
                        .opacity(pulse ? 0.3 : 1)
                        .animation(.easeInOut(duration: 0.7).repeatForever().delay(Double(index) * 0.15), value: pulse)
                }
            }
            .padding(14).background(Theme.surface, in: Capsule())
            Spacer()
        }
        .onAppear { pulse = true }
    }
}
