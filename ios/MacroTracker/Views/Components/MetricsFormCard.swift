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
                VStack(alignment: .leading, spacing: 4) {
                    SectionLabel(text: "Your metrics")
                    Text("US units")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                }
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
                    Text(displayLabel(for: field))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.ink)
                    editor(for: field)
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

    @ViewBuilder
    private func editor(for field: MetricsField) -> some View {
        if field.key == "height_cm" {
            HStack(spacing: 10) {
                unitInput(key: "height_feet", placeholder: "5", unit: "ft", keyboard: .numberPad)
                unitInput(key: "height_inches", placeholder: "10", unit: "in", keyboard: .numberPad)
            }
        } else {
            HStack(spacing: 8) {
                TextField(displayPlaceholder(for: field), text: binding(for: field.key))
                    .keyboardType(keyboard(for: field))
                    .focused($inputFocused)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .foregroundStyle(Theme.ink)
                if let unit = displayUnit(for: field) {
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

    private func unitInput(key: String, placeholder: String, unit: String, keyboard: UIKeyboardType) -> some View {
        HStack(spacing: 8) {
            TextField(placeholder, text: binding(for: key))
                .keyboardType(keyboard)
                .focused($inputFocused)
                .multilineTextAlignment(.trailing)
            Text(unit)
                .font(.subheadline)
                .foregroundStyle(Theme.muted)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .background(Theme.input, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    private func binding(for key: String) -> Binding<String> {
        Binding(
            get: { values[key] ?? "" },
            set: { values[key] = $0 }
        )
    }

    private func parsedNumber(for field: MetricsField) -> Double? {
        switch field.key {
        case "height_cm":
            return Self.heightCentimeters(
                feet: values["height_feet"] ?? "",
                inches: values["height_inches"] ?? ""
            )
        case "weight_kg", "goal_weight_kg":
            guard let pounds = Self.parseNumber(values[field.key] ?? "") else { return nil }
            return Self.kilograms(fromPounds: pounds)
        default:
            return Self.parseNumber(values[field.key] ?? "", integer: field.key == "age")
        }
    }

    private func displayLabel(for field: MetricsField) -> String {
        MetricsField.defaultLabel(for: field.key)
    }

    private func displayUnit(for field: MetricsField) -> String? {
        MetricsField.defaultUnit(for: field.key)
    }

    private func displayPlaceholder(for field: MetricsField) -> String {
        MetricsField.defaultPlaceholder(for: field.key) ?? displayLabel(for: field)
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

    static func heightCentimeters(feet rawFeet: String, inches rawInches: String) -> Double? {
        let feetText = rawFeet.trimmingCharacters(in: .whitespacesAndNewlines)
        let inchesText = rawInches.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let feet = Int(feetText), (3...8).contains(feet) else { return nil }
        guard let inches = Int(inchesText), (0...11).contains(inches) else { return nil }
        return Double((feet * 12) + inches) * 2.54
    }

    static func kilograms(fromPounds pounds: Double) -> Double {
        pounds * 0.45359237
    }

    static func parseText(_ raw: String) -> String? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.count <= 40 else { return nil }
        return trimmed
    }
}
