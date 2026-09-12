import SwiftUI
import UIKit

struct LiveCoachPresentation: Equatable {
    let status: String
    let detail: String
    let symbol: String
    let primaryAction: String?
    let showsSettingsAction: Bool

    init(state: LiveCoachState) {
        switch state {
        case .ready:
            status = "Ready"
            detail = "Talk naturally with your coach. You can speak while the coach is responding."
            symbol = "waveform.circle"
            primaryAction = "Start voice session"
            showsSettingsAction = false
        case .connecting:
            status = "Connecting"
            detail = "Setting up a private voice session."
            symbol = "ellipsis.circle"
            primaryAction = nil
            showsSettingsAction = false
        case .listening:
            status = "Listening"
            detail = "Ask about today's nutrition, workout, or your current plan."
            symbol = "mic.circle.fill"
            primaryAction = nil
            showsSettingsAction = false
        case .speaking:
            status = "Coach speaking"
            detail = "You can interrupt naturally at any time."
            symbol = "waveform.circle.fill"
            primaryAction = nil
            showsSettingsAction = false
        case .failed(let message, let retryable, let settingsAvailable):
            status = "Could not continue"
            detail = message
            symbol = "exclamationmark.circle"
            primaryAction = retryable ? "Reconnect" : nil
            showsSettingsAction = settingsAvailable
        case .ended:
            status = "Session ended"
            detail = "Your microphone is off."
            symbol = "checkmark.circle"
            primaryAction = "Start another session"
            showsSettingsAction = false
        }
    }

    static func activityLabel(_ label: String) -> String? {
        let trimmed = label.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

@MainActor
struct LiveCoachView: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var controller: LiveCoachController

    init(controller: LiveCoachController? = nil) {
        let audio = LiveCoachAudioEngine()
        _controller = StateObject(wrappedValue: controller ?? LiveCoachController(
            permission: SystemMicrophonePermission(),
            transport: LiveCoachWebSocketTransport(),
            audio: audio
        ))
    }

    var body: some View {
        let presentation = LiveCoachPresentation(state: controller.state)
        NavigationStack {
            ZStack {
                Theme.canvas.ignoresSafeArea()
                ScrollView {
                    VStack(spacing: 20) {
                        statusCard(presentation)
                        activityChip
                        captions
                        controls(presentation)
                        Text("You can ask the coach to log meals. It will confirm what it logged.")
                            .font(.footnote)
                            .foregroundStyle(Theme.muted)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 24)
                    }
                    .padding(20)
                }
            }
            .navigationTitle("Voice Coach")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Close", systemImage: "xmark") {
                        Task {
                            await controller.end()
                            dismiss()
                        }
                    }
                }
            }
            .toolbarBackground(Theme.canvas, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .toolbarColorScheme(.light, for: .navigationBar)
        }
        .interactiveDismissDisabled(controller.state == .connecting)
        .onDisappear { Task { await controller.end() } }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active { Task { await controller.end() } }
        }
    }

    private func statusCard(_ presentation: LiveCoachPresentation) -> some View {
        VStack(spacing: 14) {
            Image(systemName: presentation.symbol)
                .font(.system(size: 58, weight: .medium))
                .symbolEffect(.pulse, isActive: controller.state == .connecting || controller.state == .speaking)
                .foregroundStyle(statusColor)
            Text(presentation.status)
                .font(.system(size: 28, weight: .bold, design: .rounded))
                .foregroundStyle(Theme.ink)
            Text(presentation.detail)
                .font(.subheadline)
                .foregroundStyle(Theme.muted)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 28)
        .appCard()
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var captions: some View {
        if !controller.userCaption.isEmpty || !controller.coachCaption.isEmpty {
            VStack(spacing: 12) {
                if !controller.userCaption.isEmpty {
                    captionCard(label: "You", text: controller.userCaption, accent: false)
                }
                if !controller.coachCaption.isEmpty {
                    captionCard(label: "Coach", text: controller.coachCaption, accent: true)
                }
            }
        }
    }

    private func captionCard(label: String, text: String, accent: Bool) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(label.uppercased())
                .font(.caption2.weight(.bold))
                .tracking(1.2)
                .foregroundStyle(accent ? Theme.accent : Theme.muted)
            Text(text)
                .font(.body)
                .foregroundStyle(Theme.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .appCard()
    }

    @ViewBuilder
    private var activityChip: some View {
        if let activityState = controller.activityState,
           let activityLabel = LiveCoachPresentation.activityLabel(controller.activityLabel) {
            HStack(spacing: 8) {
                Image(systemName: activityIcon(activityState))
                    .font(.footnote.weight(.semibold))
                    .symbolEffect(.pulse, isActive: activityState == .resolving || activityState == .logging)
                Text(activityLabel)
                    .font(.footnote.weight(.medium))
                    .lineLimit(2)
            }
            .foregroundStyle(activityColor(activityState))
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(activityColor(activityState).opacity(0.1), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .transition(.opacity)
            .animation(.easeInOut(duration: 0.2), value: controller.activityState)
        }
    }

    private func activityIcon(_ activityState: LiveCoachActivityState) -> String {
        switch activityState {
        case .resolving: return "arrow.triangle.2.circlepath"
        case .logging: return "square.and.pencil"
        case .done: return "checkmark.circle.fill"
        case .error: return "exclamationmark.circle.fill"
        }
    }

    private func activityColor(_ activityState: LiveCoachActivityState) -> Color {
        activityState == .error ? Theme.carbs : Theme.accent
    }

    @ViewBuilder
    private func controls(_ presentation: LiveCoachPresentation) -> some View {
        switch controller.state {
        case .ready, .ended:
            primaryButton(presentation.primaryAction ?? "Start voice session") {
                Task { await controller.start() }
            }
        case .connecting:
            ProgressView()
                .tint(Theme.accent)
            endButton(label: "Cancel")
        case .listening, .speaking:
            HStack(spacing: 12) {
                Button {
                    Task { await controller.toggleMute() }
                } label: {
                    Label(
                        controller.isMuted ? "Unmute" : "Mute",
                        systemImage: controller.isMuted ? "mic.slash.fill" : "mic.fill"
                    )
                    .frame(maxWidth: .infinity)
                    .frame(height: 50)
                }
                .buttonStyle(.bordered)
                .tint(Theme.accent)
                endButton(label: "End session")
            }
        case .failed(_, let retryable, _):
            if retryable {
                primaryButton("Reconnect") { Task { await controller.start() } }
            } else if presentation.showsSettingsAction {
                Button("Open Settings", systemImage: "gear") {
                    guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
                    UIApplication.shared.open(url)
                }
                .buttonStyle(.bordered)
                .tint(Theme.accent)
            }
        }
    }

    private func primaryButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: "waveform")
                .fontWeight(.semibold)
                .frame(maxWidth: .infinity)
                .frame(height: 52)
        }
        .buttonStyle(.borderedProminent)
        .tint(Theme.accent)
    }

    private func endButton(label: String) -> some View {
        Button(role: .destructive) {
            Task { await controller.end() }
        } label: {
            Label(label, systemImage: "phone.down.fill")
                .frame(maxWidth: .infinity)
                .frame(height: 50)
        }
        .buttonStyle(.bordered)
    }

    private var statusColor: Color {
        if case .failed = controller.state { return Theme.danger }
        return Theme.accent
    }
}
