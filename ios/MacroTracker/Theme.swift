import SwiftUI

enum Theme {
    static let accent = Color(red: 15 / 255, green: 81 / 255, blue: 50 / 255)
    static let accentDark = Color(red: 26 / 255, green: 58 / 255, blue: 42 / 255)
    static let accentTint = Color(red: 230 / 255, green: 244 / 255, blue: 236 / 255)
    static let ink = Color(red: 26 / 255, green: 29 / 255, blue: 35 / 255)
    static let sectionInk = Color(red: 55 / 255, green: 65 / 255, blue: 81 / 255)
    static let canvas = Color(red: 245 / 255, green: 246 / 255, blue: 248 / 255)
    static let surface = Color.white
    static let muted = Color(red: 138 / 255, green: 147 / 255, blue: 166 / 255)
    static let divider = Color(red: 232 / 255, green: 235 / 255, blue: 240 / 255)
    static let input = Color(red: 243 / 255, green: 244 / 255, blue: 246 / 255)
    static let inactive = Color(red: 154 / 255, green: 163 / 255, blue: 181 / 255)
    static let calories = accent
    static let protein = accent
    static let carbs = Color(red: 180 / 255, green: 83 / 255, blue: 9 / 255)
    static let fat = Color(red: 219 / 255, green: 39 / 255, blue: 119 / 255)
    static let fiber = Color(red: 124 / 255, green: 58 / 255, blue: 237 / 255)
    static let danger = Color(red: 190 / 255, green: 45 / 255, blue: 45 / 255)
}

extension View {
    func appCard(padding: CGFloat = 16) -> some View {
        self
            .padding(padding)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            .shadow(color: .black.opacity(0.05), radius: 6, y: 2)
    }
}

struct SectionLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.caption2.weight(.bold))
            .tracking(1.4)
            .foregroundStyle(Theme.muted)
    }
}

struct FatSecretAttribution: View {
    var body: some View {
        if let destination = URL(string: "https://platform.fatsecret.com") {
            Link("Powered by fatsecret Platform API", destination: destination)
                .font(.caption2)
                .foregroundStyle(Theme.muted)
                .underline(color: Theme.muted.opacity(0.5))
                .accessibilityHint("Opens the fatsecret Platform website")
        }
    }
}

struct EmptyState: View {
    let icon: String
    let title: String
    let message: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Image(systemName: icon).font(.title2).foregroundStyle(Theme.accent)
            Text(title).font(.headline).foregroundStyle(Theme.ink)
            Text(message).font(.subheadline).foregroundStyle(Theme.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .appCard()
    }
}
