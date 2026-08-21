import SwiftUI

/// In-chat onboarding card: opens the searchable library so the user picks their plan.
struct ExercisePickerPromptCard: View {
    var isSaving: Bool = false
    let onChoose: () -> Void
    let onSkip: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    SectionLabel(text: "Your exercises")
                    Text("Search the library and pick what you want in the plan.")
                        .font(.subheadline)
                        .foregroundStyle(Theme.ink)
                }
                Spacer(minLength: 8)
                Image(systemName: "figure.strengthtraining.traditional")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(Theme.accent)
                    .frame(width: 44, height: 44)
                    .background(Theme.accentTint, in: Circle())
            }

            Button(action: onChoose) {
                Label("Search exercises", systemImage: "magnifyingglass")
                    .font(.subheadline.weight(.bold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(isSaving ? Theme.accent.opacity(0.35) : Theme.accent, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    .foregroundStyle(.white)
            }
            .disabled(isSaving)

            Button("Skip for now", action: onSkip)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.muted)
                .frame(maxWidth: .infinity)
                .disabled(isSaving)
        }
        .appCard()
    }
}
