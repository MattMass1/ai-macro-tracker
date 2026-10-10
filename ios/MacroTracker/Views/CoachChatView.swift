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

    enum Role { case user, assistant }
}

/// The classic Coach tab: a plain message feed on top of the agent. The agent
/// keeps every ability it has (log food by text with at most one question,
/// swap today's or tomorrow's workout, answer macro questions); the phone only
/// shows bubbles, one confirm row for workout changes, and the measurements
/// form when asked.
struct CoachChatView: View {
    @EnvironmentObject private var store: AppStore
    @EnvironmentObject private var auth: AuthService
    @State private var messages: [CoachMessage] = []
    @State private var input = ""
    @State private var isSending = false
    @State private var showSignOutConfirm = false
    @State private var showScanSheet = false
    @State private var showManual = false
    @FocusState private var inputFocused: Bool

    private static let quickActions: [(label: String, message: String)] = [
        ("What's left today?", "What are my remaining macros for today?"),
        ("Today's workout", "What's my workout today and what's left?"),
        ("Swap today's workout", "I want to change today's workout."),
        ("Tomorrow", "What's tomorrow's workout?"),
    ]

    var body: some View {
        ZStack {
            Theme.canvas.ignoresSafeArea()
            VStack(spacing: 0) {
                coachHeader
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 10) {
                            ForEach(messages) { message in
                                VStack(alignment: .leading, spacing: 10) {
                                    if !message.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                                        CoachBubble(message: message)
                                    }
                                    if message.role == .assistant, let approval = message.approval, approval.status != "resolved" {
                                        ApprovalRow(approval: approval, isBusy: isSending,
                                                    onConfirm: { Task { await respond(to: approval, confirm: true, in: message.id) } },
                                                    onCancel: { Task { await respond(to: approval, confirm: false, in: message.id) } })
                                    }
                                    if message.role == .assistant, message.showsMetricsForm {
                                        MetricsFormCard(
                                            fields: MetricsField.standard,
                                            isSaving: isSending,
                                            onSave: { values in
                                                Task {
                                                    dismissMetricsForm(message.id)
                                                    await sendChat(Self.metricsSummary(fields: MetricsField.standard, values: values))
                                                }
                                            },
                                            onDismiss: { dismissMetricsForm(message.id) }
                                        )
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
                quickActionChips
                FatSecretAttribution().padding(.vertical, 6)
                composer
            }
        }
        .navigationBarHidden(true)
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { dismissKeyboard() }.fontWeight(.semibold)
            }
        }
        .sheet(isPresented: $showManual) { ManualFoodView() }
        .sheet(isPresented: $showScanSheet) {
            ScanFoodSheet(onLogged: { name, calories in
                messages.append(CoachMessage(role: .assistant, text: "Logged \(name), \(Int(calories)) kcal."))
            })
        }
        .task { await loadChatHistory() }
        .confirmationDialog("Sign out?", isPresented: $showSignOutConfirm, titleVisibility: .visible) {
            Button("Sign out", role: .destructive) { auth.signOut() }
        } message: {
            Text("You'll need a new invite code to sign back in.")
        }
    }

    // MARK: - Pieces

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
                HStack(spacing: 5) {
                    Circle().fill(Theme.accent).frame(width: 7, height: 7)
                    Text("Online").font(.caption).foregroundStyle(Theme.muted)
                }
            }
            Spacer()
            Button("Manual", systemImage: "slider.horizontal.3") { showManual = true }
                .font(.caption.weight(.semibold)).foregroundStyle(Theme.accent)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
        .background(Theme.surface)
        .overlay(alignment: .bottom) { Divider().overlay(Theme.divider) }
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
                        .padding(.horizontal, 12).padding(.vertical, 7)
                        .background(Theme.accentTint, in: Capsule())
                        .disabled(isSending)
                }
            }
            .padding(.horizontal, 16)
        }
        .padding(.top, 4)
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
                } label: {
                    Image(systemName: "barcode.viewfinder").font(.body).foregroundStyle(Theme.accent)
                        .frame(width: 42, height: 42).background(Theme.accentTint, in: Circle())
                }
                TextField("Message Coach", text: $input, axis: .vertical)
                    .lineLimit(1...4)
                    .focused($inputFocused)
                    .padding(.horizontal, 14).padding(.vertical, 11)
                    .background(Theme.input, in: RoundedRectangle(cornerRadius: 18))
                    .submitLabel(.send)
                    .onSubmit { if !trimmedInput.isEmpty { Task { await send() } } }
                Button { Task { await send() } } label: {
                    Image(systemName: "arrow.up").fontWeight(.bold)
                        .frame(width: 44, height: 44)
                        .background(trimmedInput.isEmpty ? Color.secondary.opacity(0.14) : Theme.accent, in: Circle())
                        .foregroundStyle(trimmedInput.isEmpty ? Color.secondary : .white)
                }
                .disabled(trimmedInput.isEmpty || isSending)
            }
            .padding(12)
            .background(Theme.surface)
        }
    }

    private var trimmedInput: String { input.trimmingCharacters(in: .whitespacesAndNewlines) }

    // MARK: - Actions

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
        guard !isSending else { return }
        messages.append(CoachMessage(role: .user, text: message))
        isSending = true
        defer { isSending = false }
        // A Render cold start can take 30-60 s; retry the server-waking case.
        for attempt in 0...2 {
            do {
                let turn = try await store.chat.send(message)
                append(turn)
                await store.loadDay()  // the coach can log food on any turn
                return
            } catch let error as APIError where error.status == 429 {
                messages.append(CoachMessage(role: .assistant, text: "You're at today's coach limit."))
                return
            } catch {
                let text = error.localizedDescription
                let waking = text.lowercased().contains("waking") || text.lowercased().contains("try again")
                    || text.lowercased().contains("timeout")
                if waking && attempt < 2 {
                    try? await Task.sleep(nanoseconds: 3_000_000_000)
                    continue
                }
                messages.append(CoachMessage(role: .assistant, text: text))
                return
            }
        }
    }

    private func respond(to approval: CoachApproval, confirm: Bool, in messageId: UUID) async {
        guard !isSending else { return }
        isSending = true
        defer { isSending = false }
        do {
            let turn = try await store.chat.respond(approval: approval.id, confirm: confirm)
            if let index = messages.firstIndex(where: { $0.id == messageId }) {
                messages[index].approval = CoachApproval(id: approval.id, title: approval.title,
                                                         detail: approval.detail, status: "resolved")
            }
            let fallback = confirm ? "Done. Your workout is updated." : "Cancelled. Nothing was changed."
            append(turn, fallback: fallback)
            await store.loadWorkoutData()
        } catch {
            messages.append(CoachMessage(role: .assistant, text: error.localizedDescription))
        }
    }

    private func append(_ turn: CoachTurn, fallback: String = "") {
        let text = turn.reply?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        messages.append(CoachMessage(role: .assistant,
                                     text: text.isEmpty ? fallback : text,
                                     approval: turn.approval?.status == "pending" || turn.approval?.status == nil ? turn.approval : nil,
                                     showsMetricsForm: turn.wantsMetricsForm))
    }

    private func dismissMetricsForm(_ id: UUID) {
        guard let index = messages.firstIndex(where: { $0.id == id }) else { return }
        messages[index].showsMetricsForm = false
    }

    private func seedGreeting() {
        guard messages.isEmpty else { return }
        messages.append(CoachMessage(role: .assistant, text: "Tell me what you ate and I'll log it, or ask about today's workout."))
    }

    private func loadChatHistory() async {
        do {
            let history = try await store.chat.history(limit: 100)
            guard messages.isEmpty else { return }
            let restored = history.compactMap { item -> CoachMessage? in
                switch item.role {
                case "user": return CoachMessage(role: .user, text: item.content)
                case "assistant": return CoachMessage(role: .assistant, text: item.content)
                default: return nil
                }
            }
            if restored.isEmpty { seedGreeting() } else { messages = restored }
        } catch {
            seedGreeting()
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
                            in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                .foregroundStyle(Theme.ink)
                .shadow(color: message.role == .assistant ? .black.opacity(0.04) : .clear, radius: 4, y: 1)
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
