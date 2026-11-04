import SwiftUI

/// The two pixel coaches from the design package (v17 runtime frames,
/// 128 x 128, one shared ground line and pivot). The male writing loop is the
/// approved v14 motion; the rest poses and the female cycle are the reviewed
/// candidates shipped in the same pack.
enum CompanionCharacter: String, CaseIterable, Identifiable {
    case male
    case female

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .male: return "Male coach"
        case .female: return "Female coach"
        }
    }

    var restImage: String { "companion-\(rawValue)-rest" }
    func writingImage(_ index: Int) -> String { "companion-\(rawValue)-writing-\(index)" }
    func accentImage(_ index: Int) -> String { "companion-\(rawValue)-accent-\(index)" }

    /// Every asset name this character needs; the tests check each one resolves.
    var assetNames: [String] {
        [restImage] + (0..<CompanionAnimation.poseCount).flatMap { [writingImage($0), accentImage($0)] }
    }
}

/// What the coach is doing. Writing follows the agent's real pending state;
/// rest is everything else (reply shown, cancelled, failed, nothing asked).
enum CompanionState: Equatable {
    case rest
    case writing
}

struct CompanionFrame: Equatable {
    let body: String
    let accent: String?
}

/// Playback rules, UI-free so they are unit-testable. Sequence and timing come
/// from the package manifest: four aligned writing poses played
/// 0 1 2 3 1 0 0 0 at 143 ms a slot, with the matching pixel accent over the pen.
/// The character never moves as a whole; only the frame swaps.
enum CompanionAnimation {
    static let poseCount = 4
    static let sequence = [0, 1, 2, 3, 1, 0, 0, 0]
    static let slotDuration: TimeInterval = 0.143
    static var loopDuration: TimeInterval { slotDuration * Double(sequence.count) }

    static func slot(elapsed: TimeInterval) -> Int {
        guard elapsed.isFinite, elapsed > 0 else { return 0 }
        return Int((elapsed / slotDuration).rounded(.down)) % sequence.count
    }

    static func writingPose(elapsed: TimeInterval) -> Int {
        sequence[slot(elapsed: elapsed)]
    }

    /// The frame to draw. `holdStill` covers Reduce Motion and an inactive
    /// scene: both show the grounded rest pose instead of a frozen mid-stroke.
    static func frame(character: CompanionCharacter, state: CompanionState, elapsed: TimeInterval, holdStill: Bool) -> CompanionFrame {
        guard state == .writing, !holdStill else {
            return CompanionFrame(body: character.restImage, accent: nil)
        }
        let pose = writingPose(elapsed: elapsed)
        return CompanionFrame(body: character.writingImage(pose), accent: character.accentImage(pose))
    }
}

/// Device-local companion choice. The server has no such field, so this is
/// not synced and is not part of the account.
enum CompanionPreferences {
    static let characterKey = "companion.character"
    static let visibleKey = "companion.visible"
}

/// Draws one coach at the given state. Writing plays the loop on a wall-clock
/// timeline anchored to the moment writing began; rest draws a single frame
/// and schedules nothing.
struct CompanionView: View {
    let character: CompanionCharacter
    let state: CompanionState

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var writingStarted = Date()

    private var holdStill: Bool { reduceMotion || scenePhase != .active }

    var body: some View {
        Group {
            if state == .writing, !holdStill {
                TimelineView(.periodic(from: writingStarted, by: CompanionAnimation.slotDuration)) { context in
                    sprite(CompanionAnimation.frame(
                        character: character,
                        state: .writing,
                        elapsed: context.date.timeIntervalSince(writingStarted),
                        holdStill: false
                    ))
                }
            } else {
                sprite(CompanionAnimation.frame(character: character, state: state, elapsed: 0, holdStill: true))
            }
        }
        .onChange(of: state) { _, newState in
            if newState == .writing { writingStarted = Date() }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(state == .writing ? "\(character.displayName) is writing in the journal" : "\(character.displayName) standing with the journal")
    }

    private func sprite(_ frame: CompanionFrame) -> some View {
        ZStack {
            pixelImage(frame.body)
            if let accent = frame.accent {
                pixelImage(accent)
            }
        }
        .aspectRatio(1, contentMode: .fit)
    }

    private func pixelImage(_ name: String) -> some View {
        Image(name)
            .resizable()
            .interpolation(.none)
            .antialiased(false)
    }
}

/// The shared pixel studio (package v11, 144 x 256 logical pixels), scaled
/// nearest-neighbour to fill its container. Equipment sits at the lower
/// edges, the floor is clear for the coach, and the upper wall stays quiet
/// behind the cards.
struct StudioBackground: View {
    var alignment: Alignment = .center

    var body: some View {
        GeometryReader { geometry in
            Image("studio-background")
                .resizable()
                .interpolation(.none)
                .antialiased(false)
                .aspectRatio(contentMode: .fill)
                .frame(width: geometry.size.width, height: geometry.size.height, alignment: alignment)
                .clipped()
        }
        .ignoresSafeArea()
        .accessibilityHidden(true)
    }
}
