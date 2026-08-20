import SwiftUI
import UIKit

struct MetricsFieldValues: Equatable {
    var numbers: [String: Double] = [:]
    var texts: [String: String] = [:]
}

/// In-chat onboarding card: height / weight / goal / age / activity inputs with Save, skippable.
struct MetricsFormCard: View {
    let fields: [MetricsField]
    var isSaving: Bool = false
    let onSave: (MetricsFieldValues) -> Void
    let onDismiss: () -> Void

    @State private var values: [String: String] = [:]
    @FocusState private var inputFocused: Bool

    private var parsed: MetricsFieldValues {
        var numbers: [String: Double] = [:]
        var texts: [String: String] = [:]
        for field in fields {
            if field.isNumeric {
                if let number = parsedNumber(for: field) { numbers[field.key] = number }
            } else if let text = Self.parseText(values[field.key] ?? "") {
                texts[field.key] = text
            }
        }
        return MetricsFieldValues(numbers: numbers, texts: texts)
    }

    private var canSave: Bool {
        guard !fields.isEmpty, !isSaving else { return false }
        return fields.allSatisfy { field in
            field.isNumeric ? parsed.numbers[field.key] != nil : parsed.texts[field.key] != nil
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top) {
                SectionLabel(text: "Your metrics")
                Spacer(minLength: 8)
                Button {
                    inputFocused = false
                    onDismiss()
                } label: {
                    Image(systemName: "xmark")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(Theme.muted)
                        .frame(width: 44, height: 44)
                        .background(Theme.input, in: Circle())
                }
                .accessibilityLabel("Skip")
                .disabled(isSaving)
            }

            ForEach(fields, id: \.key) { field in
                VStack(alignment: .leading, spacing: 6) {
                    Text(field.label)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.ink)
                    HStack(spacing: 8) {
                        TextField(field.placeholder ?? field.label, text: binding(for: field.key))
                            .keyboardType(keyboard(for: field))
                            .focused($inputFocused)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .foregroundStyle(Theme.ink)
                        if let unit = field.unit, !unit.isEmpty {
                            Text(unit)
                                .font(.subheadline)
                                .foregroundStyle(Theme.muted)
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 11)
                    .background(Theme.input, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
            }

            Button(action: submit) {
                Text("Save")
                    .fontWeight(.bold)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(canSave ? Theme.accent : Theme.accent.opacity(0.35), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    .foregroundStyle(.white)
            }
            .disabled(!canSave)
        }
        .appCard()
    }

    private func keyboard(for field: MetricsField) -> UIKeyboardType {
        guard field.isNumeric else { return .default }
        return field.key == "age" ? .numberPad : .decimalPad
    }

    private func binding(for key: String) -> Binding<String> {
        Binding(
            get: { values[key] ?? "" },
            set: { values[key] = $0 }
        )
    }

    private func parsedNumber(for field: MetricsField) -> Double? {
        Self.parseNumber(values[field.key] ?? "", integer: field.key == "age")
    }

    private func submit() {
        guard canSave else { return }
        inputFocused = false
        onSave(parsed)
    }

    static func parseNumber(_ raw: String, integer: Bool = false) -> Double? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: ",", with: ".")
        guard let value = Double(trimmed), value.isFinite, value > 0 else { return nil }
        if integer, value != value.rounded() { return nil }
        return value
    }

    static func parseText(_ raw: String) -> String? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.count <= 40 else { return nil }
        return trimmed
    }
}
