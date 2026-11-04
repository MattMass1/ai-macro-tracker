import XCTest
import SwiftUI
@testable import MacroTracker

final class CompanionAnimationTests: XCTestCase {
    func testSequenceAndTimingMatchThePackageManifest() {
        XCTAssertEqual(CompanionAnimation.sequence, [0, 1, 2, 3, 1, 0, 0, 0])
        XCTAssertEqual(CompanionAnimation.slotDuration, 0.143, accuracy: 0.0001)
        XCTAssertEqual(CompanionAnimation.loopDuration, 1.144, accuracy: 0.001)
    }

    func testWritingPoseFollowsTheWallClock() {
        let slot = CompanionAnimation.slotDuration
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: 0), 0)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 1.5), 1)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 2.5), 2)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 3.5), 3)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 4.5), 1)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 5.5), 0)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 8.5), 0, "loops after eight slots")
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: slot * 9.5), 1)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: -1), 0)
        XCTAssertEqual(CompanionAnimation.writingPose(elapsed: .nan), 0)
    }

    func testRestAndHoldStillShowTheGroundedRestPose() {
        for character in CompanionCharacter.allCases {
            let rest = CompanionAnimation.frame(character: character, state: .rest, elapsed: 0.5, holdStill: false)
            XCTAssertEqual(rest, CompanionFrame(body: character.restImage, accent: nil))
            let reduced = CompanionAnimation.frame(character: character, state: .writing, elapsed: 0.5, holdStill: true)
            XCTAssertEqual(reduced, CompanionFrame(body: character.restImage, accent: nil), "Reduce Motion / inactive scene never freezes mid-stroke")
        }
    }

    func testWritingFrameCarriesTheMatchingAccent() {
        let frame = CompanionAnimation.frame(character: .female, state: .writing, elapsed: CompanionAnimation.slotDuration * 3.2, holdStill: false)
        XCTAssertEqual(frame, CompanionFrame(body: "companion-female-writing-3", accent: "companion-female-accent-3"))
    }

    func testEveryCompanionAssetShipsAtItsLogicalSize() throws {
        let bundle = Bundle(for: AppStore.self)
        for character in CompanionCharacter.allCases {
            XCTAssertEqual(character.assetNames.count, 9)
            for name in character.assetNames {
                let image = try XCTUnwrap(UIImage(named: name, in: bundle, compatibleWith: nil), "missing \(name)")
                XCTAssertEqual(image.size.width * image.scale, 128, name)
                XCTAssertEqual(image.size.height * image.scale, 128, name)
            }
        }
        let studio = try XCTUnwrap(UIImage(named: "studio-background", in: bundle, compatibleWith: nil))
        XCTAssertEqual(studio.size.width * studio.scale, 144)
        XCTAssertEqual(studio.size.height * studio.scale, 256)
    }
}

@MainActor
final class CoachRespondingStateTests: XCTestCase {
    func testPendingHomeMessageIsClearedOnReset() {
        let store = AppStore()
        store.pendingCoachMessage = "What's left today?"
        store.reset()
        XCTAssertNil(store.pendingCoachMessage)
    }

    func testChatStoreStartsAtRest() {
        let store = AppStore()
        XCTAssertFalse(store.chat.isResponding)
        XCTAssertEqual(store.chat.inFlight, 0)
    }
}
