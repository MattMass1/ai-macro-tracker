import SwiftUI

/// The studio is the home, not an extra hub before a second chat screen.
/// The conversation owns the only composer and uses AppStore's shared store.
struct HomeView: View {
    var openWorkouts: () -> Void = {}

    var body: some View {
        CoachChatView(openWorkouts: openWorkouts)
    }
}

/// Reference proportions at a 390-point phone width. The scene stays the focus
/// through composition, not by enlarging the character or shrinking the panel.
struct StudioLayout {
    let width: CGFloat
    let height: CGFloat
    static let titleSize: CGFloat = 28
    static let panelTitleSize: CGFloat = 16
    static let summaryTitleSize: CGFloat = 22
    static let dockLabelSize: CGFloat = 14

    var companionSize: CGFloat { min(144, max(0, width) * 144 / 390, max(0, height) * 0.24) }
    var floorHeight: CGFloat { companionSize + min(32, max(0, height) * 0.05) }
}

/// Layout receipts let mounted tests verify clipping without private UI APIs.
struct StudioLayoutFrames: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

extension View {
    func studioFrame(_ name: String) -> some View {
        background(GeometryReader { geometry in
            Color.clear.preference(key: StudioLayoutFrames.self, value: [name: geometry.frame(in: .global)])
        })
    }
}

/// Same three destinations, with the compact flat tab row from the reference.
struct StudioTabBar: View {
    @Binding var selection: Int
    private let tabs = [(0, "Today", "house"), (2, "Workouts", "dumbbell"), (3, "Progress", "chart.line.uptrend.xyaxis")]

    var body: some View {
        HStack(spacing: 12) {
            ForEach(tabs, id: \.0) { tab in
                Button { selection = tab.0 } label: {
                    VStack(spacing: 2) {
                        Image(systemName: tab.2).font(.system(size: 17, weight: .semibold))
                        Text(tab.1).font(Theme.pixelFont(17, relativeTo: .caption))
                    }
                    .foregroundStyle(Theme.sectionInk)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(selection == tab.0 ? Theme.accentTint : Color.clear)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(tab.1)
                .accessibilityIdentifier("studio.tab.\(tab.0)")
                .studioFrame("tab.\(tab.0)")
                .accessibilityAddTraits(selection == tab.0 ? .isSelected : [])
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 4)
        .background(Theme.dock, ignoresSafeAreaEdges: [])
        .clipped()
        .studioFrame("tabs")
    }
}

/// The coach on the studio floor. Observes the chat store so the pen moves
/// only while a request is actually pending.
struct CompanionStage: View {
    @ObservedObject var chat: AppStore
    let character: CompanionCharacter
    var size: CGFloat = 144

    var body: some View {
        CompanionView(character: character, state: chat.isSending ? .writing : .rest)
            .frame(width: size, height: size)
    }
}
