import SwiftUI
import UIKit

/// One pixel language, from the room to the controls (design package v12):
/// sage and cream surfaces, stepped borders and Jersey 15 display lettering.
/// Paragraphs and editable inputs stay native for readability and accessibility.
enum Theme {
    static let profileCanvas = Color(red: 14 / 255, green: 43 / 255, blue: 36 / 255)
    static let profileSurface = Color(red: 23 / 255, green: 65 / 255, blue: 49 / 255)
    static let profileMint = Color(red: 192 / 255, green: 217 / 255, blue: 154 / 255)
    static let profileInk = Color(red: 248 / 255, green: 248 / 255, blue: 215 / 255)
    static let profileMuted = Color(red: 205 / 255, green: 224 / 255, blue: 184 / 255)
    static let profileOutline = Color(red: 92 / 255, green: 138 / 255, blue: 87 / 255)
    static let profileAccent = Color(red: 204 / 255, green: 234 / 255, blue: 114 / 255)
    static let profileDanger = Color(red: 255 / 255, green: 185 / 255, blue: 164 / 255)
    static let profileWellInk = Color(red: 19 / 255, green: 49 / 255, blue: 34 / 255)
    static let profileWellMuted = Color(red: 58 / 255, green: 78 / 255, blue: 42 / 255)
    static let accent = Color(red: 22 / 255, green: 86 / 255, blue: 57 / 255)        // #165639 forest
    static let accentDark = Color(red: 16 / 255, green: 61 / 255, blue: 40 / 255)    // #103d28
    static let accentEdge = Color(red: 69 / 255, green: 103 / 255, blue: 71 / 255)   // #456747
    static let accentTint = Color(red: 229 / 255, green: 236 / 255, blue: 216 / 255) // #e5ecd8 moss
    static let ink = Color(red: 32 / 255, green: 35 / 255, blue: 33 / 255)           // #202321
    static let sectionInk = Color(red: 56 / 255, green: 81 / 255, blue: 59 / 255)    // #38513b
    static let canvas = Color(red: 238 / 255, green: 241 / 255, blue: 232 / 255)     // pale sage wall
    static let surface = Color(red: 247 / 255, green: 247 / 255, blue: 239 / 255)    // #f7f7ef panel
    static let paper = Color(red: 250 / 255, green: 249 / 255, blue: 237 / 255)      // #faf9ed input
    static let dock = Color(red: 232 / 255, green: 237 / 255, blue: 223 / 255)       // #e8eddf
    static let edge = Color(red: 166 / 255, green: 181 / 255, blue: 164 / 255)       // #a6b5a4
    static let edgeStrong = Color(red: 145 / 255, green: 162 / 255, blue: 133 / 255) // #91a285
    static let muted = Color(red: 80 / 255, green: 96 / 255, blue: 78 / 255)         // #50604e
    static let divider = Color(red: 215 / 255, green: 223 / 255, blue: 207 / 255)
    static let input = paper
    static let inactive = Color(red: 128 / 255, green: 141 / 255, blue: 124 / 255)
    static let calories = accent
    static let protein = accent
    static let carbs = Color(red: 180 / 255, green: 83 / 255, blue: 9 / 255)
    static let fat = Color(red: 219 / 255, green: 39 / 255, blue: 119 / 255)
    static let fiber = Color(red: 124 / 255, green: 58 / 255, blue: 237 / 255)
    static let danger = Color(red: 190 / 255, green: 45 / 255, blue: 45 / 255)

    static func pixelFont(_ size: CGFloat, relativeTo style: Font.TextStyle = .headline) -> Font {
        .custom("Jersey15-Regular", size: size, relativeTo: style)
    }

    static let cardRadius: CGFloat = 10
    static let controlRadius: CGFloat = 3
}

/// A stepped silhouette, not a rounded modern card. Insets stay pixel aligned.
struct PixelPanelShape: Shape {
    func path(in rect: CGRect) -> Path {
        let step = min(4, min(rect.width, rect.height) / 4)
        let x = rect.minX, y = rect.minY, r = rect.maxX, b = rect.maxY
        let points = [
            CGPoint(x: x + step * 2, y: y), CGPoint(x: r - step * 2, y: y),
            CGPoint(x: r - step * 2, y: y + step), CGPoint(x: r - step, y: y + step),
            CGPoint(x: r - step, y: y + step * 2), CGPoint(x: r, y: y + step * 2),
            CGPoint(x: r, y: b - step * 2), CGPoint(x: r - step, y: b - step * 2),
            CGPoint(x: r - step, y: b - step), CGPoint(x: r - step * 2, y: b - step),
            CGPoint(x: r - step * 2, y: b), CGPoint(x: x + step * 2, y: b),
            CGPoint(x: x + step * 2, y: b - step), CGPoint(x: x + step, y: b - step),
            CGPoint(x: x + step, y: b - step * 2), CGPoint(x: x, y: b - step * 2),
            CGPoint(x: x, y: y + step * 2), CGPoint(x: x + step, y: y + step * 2),
            CGPoint(x: x + step, y: y + step), CGPoint(x: x + step * 2, y: y + step)
        ]
        return Path { path in
            path.addLines(points)
            path.closeSubpath()
        }
    }
}

extension View {
    /// A cream panel with a sage edge, the card used everywhere in the app.
    func appCard(padding: CGFloat = 16) -> some View {
        self
            .padding(padding)
            // Shadow on the panel only; on the whole view it ghosts every line of text.
            .background(PixelPanelShape().fill(Theme.surface).shadow(color: Theme.accentEdge.opacity(0.3), radius: 0, y: 3))
            .overlay(PixelPanelShape().stroke(Theme.edgeStrong, lineWidth: 1))
    }
}

/// Forest-green block button with the pixel bottom edge (design v12 `.primary`).
struct PixelPrimaryButtonStyle: ButtonStyle {
    var fullWidth = true
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.pixelFont(20))
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .frame(maxWidth: fullWidth ? .infinity : nil, minHeight: 46)
            .background(Theme.accent, in: PixelPanelShape())
            .overlay(alignment: .bottom) { Rectangle().fill(Theme.accentDark).frame(height: 3) }
            .overlay(PixelPanelShape().stroke(Theme.accentDark, lineWidth: 2))
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

/// Moss-tinted secondary block button (design v12 `.secondary`).
struct PixelSecondaryButtonStyle: ButtonStyle {
    var fullWidth = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.pixelFont(20))
            .foregroundStyle(Theme.accent)
            .padding(.horizontal, 14)
            .frame(maxWidth: fullWidth ? .infinity : nil, minHeight: 44)
            .background(Theme.accentTint, in: PixelPanelShape())
            .overlay(PixelPanelShape().stroke(Theme.edgeStrong, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.8 : 1)
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
