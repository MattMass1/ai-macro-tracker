import XCTest

final class StudioUITests: XCTestCase {
    override func setUp() { super.setUp(); continueAfterFailure = false }
    func attach(_ name: String, app: XCUIApplication) {
        Thread.sleep(forTimeInterval: 1)
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways
        add(attachment)
    }
    func scrollChat(_ app: XCUIApplication, towardBottom: Bool) {
        let upper = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.3))
        let lower = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.7))
        for _ in 0..<2 {
            (towardBottom ? lower : upper).press(forDuration: 0.1, thenDragTo: towardBottom ? upper : lower)
        }
    }
    func testSetupWalksBothStepsAndEntersStudio() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-fixture-ui", "-fixture-setup"]
        app.launch()
        let feet = app.textFields["setup-feet"]
        XCTAssertTrue(feet.waitForExistence(timeout: 10))
        attach("20-setup-about-empty", app: app)
        app.buttons["setup-next"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["setup-error"].waitForExistence(timeout: 5))
        for (id, value) in [("setup-feet", "5"), ("setup-inches", "11"), ("setup-pounds", "180"), ("setup-age", "30")] {
            let field = app.textFields[id]; field.tap(); field.typeText(value)
            app.buttons["Done"].firstMatch.tap()
            XCTAssertEqual(field.value as? String, value)
        }
        attach("21-setup-about-filled", app: app)
        app.buttons["setup-next"].tap()
        XCTAssertTrue(app.buttons["setup-activity-moderate"].waitForExistence(timeout: 5))
        attach("22-setup-plan-top", app: app)
        app.swipeUp(); app.swipeUp()
        XCTAssertTrue(app.buttons["setup-goal-lose"].isHittable)
        app.buttons["setup-goal-lose"].tap()
        XCTAssertEqual(app.buttons["setup-goal-lose"].value as? String, "Selected")
        attach("22-setup-plan-bottom", app: app)
        app.buttons["setup-save"].tap()
        XCTAssertTrue(app.buttons["Meals and macros"].waitForExistence(timeout: 10))
        attach("23-setup-done-studio", app: app)
    }

    func testMyTargetsOpensPrefilledFromTheMenuAndCancelReturns() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-fixture-ui"]
        app.launch()
        XCTAssertTrue(app.buttons["Coach menu"].waitForExistence(timeout: 10))
        app.buttons["Coach menu"].tap()
        app.buttons["My targets"].tap()
        XCTAssertTrue(app.textFields["setup-feet"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.textFields["setup-pounds"].value as? String, "180")
        XCTAssertTrue(app.descendants(matching: .any)["targets-current"].exists)
        attach("30-my-targets", app: app)
        app.buttons["setup-cancel"].tap()
        XCTAssertTrue(app.buttons["Coach menu"].waitForExistence(timeout: 10))
        app.buttons["Coach menu"].tap()
        app.buttons["Delete account"].tap()
        XCTAssertTrue(app.buttons["delete-account-confirm"].firstMatch.waitForExistence(timeout: 5))
        attach("31-delete-confirm", app: app)
    }

    func testTokenScreenAndOfflineStudioScreens() throws {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.buttons["sign-in-with-apple"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["use-device-token"].exists)
        XCTAssertFalse(app.secureTextFields["device-token"].exists)
        attach("01-token-screen", app: app)
        app.terminate()
        app.launchArguments = ["-fixture-ui"]
        app.launch()
        XCTAssertTrue(app.buttons["Meals and macros"].waitForExistence(timeout: 10))
        let composer = app.descendants(matching: .any).matching(identifier: "studio.composer").firstMatch
        composer.tap(); composer.typeText("a banana")
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.buttons["Confirm meal"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["Cancel"].exists)
        attach("02-studio-preview-OFFLINE", app: app)
        app.buttons["Confirm meal"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["chat.surface.meal_receipt"].waitForExistence(timeout: 10))
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
        let again = app.buttons["repeat-Synthetic eggs"]
        XCTAssertTrue(again.waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["repeat-Synthetic calorie-only meal"].exists, "unknown-macro meals can't be copied")
        again.tap()
        XCTAssertTrue(app.staticTexts["preset-logged"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts["preset-logged"].label, "+ Synthetic eggs")
        let save = app.buttons["save-Synthetic eggs"]
        XCTAssertTrue(save.exists); save.tap()
        XCTAssertTrue(app.staticTexts["saved-Synthetic eggs"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["preset-Synthetic eggs"].waitForExistence(timeout: 10))
        attach("08-meal-buttons-OFFLINE", app: app)
        let shake = app.buttons["preset-Synthetic shake"]
        XCTAssertTrue(shake.waitForExistence(timeout: 10))
        shake.press(forDuration: 1.2)
        app.buttons["Edit name, emoji or slot…"].tap()
        let name = app.textFields["edit-preset-name"]
        XCTAssertTrue(name.waitForExistence(timeout: 10))
        name.tap(); name.press(forDuration: 1.0)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        name.typeText("Synthetic morning shake")
        attach("09-edit-saved-meal-OFFLINE", app: app)
        app.buttons["save-preset-edit"].tap()
        XCTAssertTrue(app.buttons["preset-Synthetic morning shake"].waitForExistence(timeout: 10))
        let bowl = app.buttons["preset-Synthetic bowl"]
        bowl.press(forDuration: 1.2)
        app.buttons["Remove from saved meals"].tap()
        let confirm = app.buttons["confirm-remove-preset"].firstMatch
        XCTAssertTrue(confirm.waitForExistence(timeout: 10)); confirm.tap()
        XCTAssertTrue(app.buttons["preset-Synthetic shake"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["preset-Synthetic bowl"].exists)
        app.buttons["Done"].tap()
        app.buttons["studio.tab.3"].tap()
        XCTAssertTrue(app.staticTexts["Daily calories".uppercased()].waitForExistence(timeout: 10))
        attach("04-progress-OFFLINE", app: app)
        app.buttons["studio.tab.2"].tap()
        XCTAssertTrue(app.staticTexts["Coming soon"].waitForExistence(timeout: 10))
        attach("05-workouts-OFFLINE", app: app)
    }

    func testMountedChatSurfacesPreviewConfirmAndReadCards() throws {
        let app = XCUIApplication(); app.launchArguments = ["-fixture-ui", "-companion.visible", "NO"]; app.launch()
        let composer = app.descendants(matching: .any).matching(identifier: "studio.composer").firstMatch
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        composer.tap(); composer.typeText("a banana"); app.buttons["send-message"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["chat.surface.meal_preview"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["Confirm meal"].exists)
        XCTAssertFalse(app.buttons["Log it"].exists)
        XCTAssertTrue(app.buttons["Cancel"].exists)
        scrollChat(app, towardBottom: true)
        attach("09-chat-preview-OFFLINE", app: app)
        scrollChat(app, towardBottom: false)
        XCTAssertTrue(app.staticTexts["Review meal"].isHittable)
        attach("14-chat-preview-top-OFFLINE", app: app)
        scrollChat(app, towardBottom: true)
        app.buttons["Edit portion"].tap()
        XCTAssertEqual(composer.value as? String, "make it 2 servings")
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label == %@", "a banana")).count, 1)
        app.toolbars.buttons["Done"].tap()
        app.buttons["Confirm meal"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["chat.surface.meal_receipt"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label == %@", "Log it")).count, 1)
        scrollChat(app, towardBottom: true)
        attach("10-chat-receipt-OFFLINE", app: app)
        composer.tap(); composer.typeText("Show my daily totals"); app.buttons["send-message"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["chat.surface.day_totals"].waitForExistence(timeout: 10))
        scrollChat(app, towardBottom: true)
        attach("11-chat-day-totals-OFFLINE", app: app)
        composer.tap(); composer.typeText("simulate save failure"); app.buttons["send-message"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["chat.surface.save_failed"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["Review & retry"].exists)
        attach("12-chat-save-failed-OFFLINE", app: app)
        composer.tap(); composer.typeText("simulate safe retry"); app.buttons["send-message"].tap()
        XCTAssertTrue(app.buttons["Review & retry"].waitForExistence(timeout: 10))
        attach("13-chat-safe-review-OFFLINE", app: app)
        app.buttons["Review & retry"].tap()
        XCTAssertEqual(composer.value as? String, "Show my daily totals")
    }

    func testMountedPreviewCancelNeverConfirms() throws {
        let app = XCUIApplication(); app.launchArguments = ["-fixture-ui", "-companion.visible", "NO"]; app.launch()
        let composer = app.descendants(matching: .any).matching(identifier: "studio.composer").firstMatch
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        composer.tap(); composer.typeText("a banana"); app.buttons["send-message"].tap()
        XCTAssertTrue(app.buttons["Cancel"].waitForExistence(timeout: 10))
        app.buttons["Cancel"].tap()
        XCTAssertTrue(app.staticTexts["Synthetic reply received. No backend writes occurred."].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["Confirm meal"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["chat.surface.meal_receipt"].exists)
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label == %@", "Cancel")).count, 1)
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label == %@", "Log it")).count, 0)
        attach("15-chat-cancel-OFFLINE", app: app)
    }

    func testMountedWarningsModificationsAndFractionalTotals() throws {
        let app = XCUIApplication(); app.launchArguments = ["-fixture-ui", "-companion.visible", "NO"]; app.launch()
        let composer = app.descendants(matching: .any).matching(identifier: "studio.composer").firstMatch
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        for (request, notice) in [
            ("simulate changed nutrition", "Nutrition changed. Review the updated amounts and confirm again."),
            ("simulate failed edit", "I couldn't process that change. Nothing was saved. Here is the unchanged preview.")
        ] {
            composer.tap(); composer.typeText(request); app.buttons["send-message"].tap()
            XCTAssertTrue(app.staticTexts[notice].waitForExistence(timeout: 10))
            XCTAssertTrue(app.buttons["Confirm meal"].exists)
            app.staticTexts[notice].tap()
            XCTAssertTrue(app.staticTexts[notice].isHittable)
            attach(request == "simulate changed nutrition" ? "16-chat-nutrition-change-OFFLINE" : "17-chat-failed-edit-OFFLINE", app: app)
        }
        composer.tap(); composer.typeText("simulate modified meal"); app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["remove tomato"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["STANDARD recipe; nutrition unchanged for minor garnish changes"].exists)
        app.staticTexts["remove tomato"].tap()
        attach("18-chat-qualification-OFFLINE", app: app)
        composer.tap(); composer.typeText("fractional totals"); app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["400.25 / 2000 kcal"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["1599.75 kcal remaining"].exists)
        scrollChat(app, towardBottom: true)
        attach("19-chat-fractional-totals-OFFLINE", app: app)
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
