import SwiftUI

enum Theme {
    static let accent = Color(red: 0.15, green: 0.56, blue: 0.29)
    static let ink = Color(red: 0.08, green: 0.10, blue: 0.09)
    static let canvas = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.06, green: 0.07, blue: 0.065, alpha: 1) : UIColor(red: 0.96, green: 0.95, blue: 0.92, alpha: 1) })
    static let surface = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.11, green: 0.12, blue: 0.115, alpha: 1) : UIColor(red: 1, green: 0.995, blue: 0.98, alpha: 1) })
    static let muted = Color.secondary
    static let calories = Color(red: 0.86, green: 0.45, blue: 0.22)
    static let protein = accent
    static let carbs = Color(red: 0.20, green: 0.52, blue: 0.72)
    static let fat = Color(red: 0.78, green: 0.61, blue: 0.16)
    static let fiber = Color(red: 0.42, green: 0.49, blue: 0.30)
    static let danger = Color(red: 0.75, green: 0.20, blue: 0.18)
}

extension View {
    func appCard(padding: CGFloat = 16) -> some View {
        self.padding(padding).background(Theme.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }
}

struct SectionLabel: View {
    let text: String
    var body: some View { Text(text.uppercased()).font(.caption2.weight(.bold)).tracking(1.6).foregroundStyle(.secondary) }
}

struct EmptyState: View {
    let icon: String; let title: String; let message: String
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Image(systemName: icon).font(.title2).foregroundStyle(Theme.accent)
            Text(title).font(.headline)
            Text(message).font(.subheadline).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity, alignment: .leading).appCard()
    }
}

