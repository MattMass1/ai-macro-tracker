import XCTest

final class StudioUITests: XCTestCase {
    override func setUp() { super.setUp(); continueAfterFailure = false }
    func attach(_ name: String, app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways
        add(attachment)
    }
    func testTokenScreenAndOfflineStudioScreens() throws {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.secureTextFields["device-token"].waitForExistence(timeout: 10))
        attach("01-token-screen", app: app)
        app.terminate()
        app.launchArguments = ["-fixture-ui"]
        app.launch()
        XCTAssertTrue(app.buttons["Meals and macros"].waitForExistence(timeout: 10))
        let composer = app.descendants(matching: .any).matching(identifier: "studio.composer").firstMatch
        composer.tap(); composer.typeText("a banana")
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.buttons["Log it"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["Cancel"].exists)
        attach("02-studio-preview-OFFLINE", app: app)
        app.buttons["Log it"].tap()
        XCTAssertTrue(app.staticTexts["Synthetic reply received. No backend writes occurred."].waitForExistence(timeout: 10))
        app.buttons["Meals and macros"].tap()
        XCTAssertTrue(app.staticTexts["Today"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.descendants(matching: .any)["pixel-macro-summary"].waitForExistence(timeout: 10))
        let tile = app.buttons["preset-Synthetic shake"]
        XCTAssertTrue(tile.waitForExistence(timeout: 10))
        attach("03-today-OFFLINE", app: app)
        tile.tap()
        XCTAssertTrue(app.staticTexts["preset-logged"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts["preset-logged"].label, "+ Synthetic shake")
        attach("07-preset-logged-OFFLINE", app: app)
        app.buttons["Done"].tap()
        app.buttons["studio.tab.3"].tap()
        XCTAssertTrue(app.staticTexts["Daily calories".uppercased()].waitForExistence(timeout: 10))
        attach("04-progress-OFFLINE", app: app)
        app.buttons["studio.tab.2"].tap()
        XCTAssertTrue(app.staticTexts["Coming soon"].waitForExistence(timeout: 10))
        attach("05-workouts-OFFLINE", app: app)
    }

    func testMountedInjectedScanGoesOnceToSharedChat() throws {
        let app = XCUIApplication(); app.launchArguments = ["-fixture-ui"]; app.launch()
        let options = app.buttons["meal-entry-options"]
        XCTAssertTrue(options.waitForExistence(timeout: 10)); options.tap()
        app.buttons["Scan Barcode"].tap()
        XCTAssertTrue(app.buttons["Log it"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label == %@", "00016000275287")).count, 1)
        XCTAssertTrue(app.staticTexts["Synthetic barcode preview. 1 serving, 100 kcal. Log this meal?"].exists)
        attach("06-scan-to-chat-OFFLINE", app: app)
    }
}
