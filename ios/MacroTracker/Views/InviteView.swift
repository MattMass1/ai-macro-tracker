import SwiftUI
import UIKit

/// First-launch gate: claims an invite code and stores the device token in
/// the Keychain. This is the first screen a new user ever sees.
struct InviteView: View {
    @EnvironmentObject private var auth: AuthService
    @State private var code = ""
    @FocusState private var focused: Bool

    var body: some View {
        ZStack {
            Theme.canvas.ignoresSafeArea()
            VStack(alignment: .leading, spacing: 28) {
                Spacer()
                Image(systemName: "leaf.fill")
                    .font(.system(size: 34, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                VStack(alignment: .leading, spacing: 8) {
                    Text("Macro Coach")
                        .font(.system(size: 36, weight: .bold, design: .rounded))
                        .tracking(-1)
                        .foregroundStyle(Theme.ink)
                    Text("Enter your invite code to join. Your coach takes it from there.")
                        .foregroundStyle(Theme.muted)
                }
                TextField("Invite code", text: $code)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.asciiCapable)
                    .submitLabel(.go)
                    .onSubmit(join)
                    .focused($focused)
                    .padding(16)
                    .background(Theme.surface, in: RoundedRectangle(cornerRadius: 16))
                if let error = auth.errorMessage {
                    Label(error, systemImage: "exclamationmark.circle.fill")
                        .font(.footnote)
                        .foregroundStyle(Theme.danger)
                }
                Button(action: join) {
                    Group {
                        if auth.isClaiming { ProgressView().tint(.white) }
                        else { Text("Join").fontWeight(.bold) }
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 15)
                    .background(Theme.accent, in: RoundedRectangle(cornerRadius: 16))
                    .foregroundStyle(.white)
                }
                .disabled(joinDisabled)
                .opacity(joinDisabled ? 0.45 : 1)
                Spacer().frame(height: 36)
            }
            .padding(24)
        }
        .onAppear { focused = true }
    }

    private var joinDisabled: Bool {
        auth.isClaiming || code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func join() {
        guard !joinDisabled else { return }
        Task { await auth.claimInvite(code: code, deviceLabel: UIDevice.current.name) }
    }
}
