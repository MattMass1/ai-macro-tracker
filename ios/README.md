# Macro Tracker for iOS

Native SwiftUI client for iOS 17 and later.

## Run

1. Open `MacroTracker.xcodeproj` in Xcode.
2. Set `APP_SHARED_TOKEN` in `Config.xcconfig` to the value used by the Render service.
3. Select your development team under the MacroTracker target's Signing settings.
4. Choose an iPhone or simulator and run the `MacroTracker` scheme.

The first launch asks you to create a local numeric passcode. Its SHA-256 digest is stored in the device Keychain, and the app locks whenever it enters the background. Face ID is offered after the passcode is created.

`project.yml` is included for teams that use XcodeGen; the checked-in Xcode project works without XcodeGen.

