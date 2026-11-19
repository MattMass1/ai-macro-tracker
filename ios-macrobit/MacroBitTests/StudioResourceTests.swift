import XCTest
import UIKit
import SwiftUI
@testable import MacroBit

@MainActor
final class StudioResourceTests: XCTestCase {
    func testJerseyFontAndLicenseAreBundled() throws {
        XCTAssertNotNil(UIFont(name: "Jersey15-Regular", size: 20))
        let licenseURL = try XCTUnwrap(Bundle.main.url(forResource: "Jersey15-OFL", withExtension: "txt"))
        XCTAssertTrue(try String(contentsOf: licenseURL, encoding: .utf8).contains("SIL OPEN FONT LICENSE"))
    }
    func testAllCompanionFramesAndStudioResolve() {
        for character in CompanionCharacter.allCases {
            for name in character.assetNames { XCTAssertNotNil(UIImage(named: name), name) }
        }
        XCTAssertNotNil(UIImage(named: "studio-background"))
    }
    func testCompanionWritingAndReducedMotion() {
        XCTAssertEqual(CompanionAnimation.sequence, [0, 1, 2, 3, 1, 0, 0, 0])
        XCTAssertEqual(CompanionAnimation.frame(character: .male, state: .writing, elapsed: 0.3, holdStill: false).body, "companion-male-writing-2")
        XCTAssertEqual(CompanionAnimation.frame(character: .female, state: .writing, elapsed: 0.3, holdStill: true).body, "companion-female-rest")
        XCTAssertNil(CompanionAnimation.frame(character: .male, state: .rest, elapsed: 100, holdStill: false).accent)
    }
    func testStudioProportionsAndPixelPanel() {
        let layout = StudioLayout(width: 390, height: 700)
        XCTAssertEqual(layout.companionSize, 144)
        XCTAssertFalse(PixelPanelShape().path(in: CGRect(x: 0, y: 0, width: 300, height: 300)).isEmpty)
    }
    func testAppIdentityAndConfiguredOrigin() {
        XCTAssertEqual(Bundle.main.bundleIdentifier, "com.biz21.macrobit")
        XCTAssertEqual(Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String, "Macro-Bit")
        XCTAssertEqual(Config.apiURL?.absoluteString, "https://mmmacros-backend.onrender.com")
    }
}

final class PixelMeterTests: XCTestCase {
    func testBlocksFillProportionallyAndCap() {
        XCTAssertEqual(PixelMeter.filled(value: 0, target: 2000), 0)
        XCTAssertEqual(PixelMeter.filled(value: 1000, target: 2000), 8)
        XCTAssertEqual(PixelMeter.filled(value: 2000, target: 2000), PixelMeter.blocks)
        XCTAssertEqual(PixelMeter.filled(value: 5000, target: 2000), PixelMeter.blocks)
    }
    func testMissingTargetFillsNothingAndIsNeverOver() {
        XCTAssertEqual(PixelMeter.filled(value: 30, target: 0), 0)
        XCTAssertFalse(PixelMeter.isOver(value: 30, target: 0))
        XCTAssertEqual(PixelMeter.filled(value: .nan, target: 100), 0)
    }
    func testOver() {
        XCTAssertTrue(PixelMeter.isOver(value: 2100, target: 2000))
        XCTAssertFalse(PixelMeter.isOver(value: 2000, target: 2000))
    }
}
