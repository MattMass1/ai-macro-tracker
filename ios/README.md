# Macro Tracker for iOS

Native SwiftUI client for iOS 17 and later.

## Run

1. Open `MacroTracker.xcodeproj` in Xcode.
2. Copy `Config.xcconfig.template` to `Config.xcconfig` (only `MACRO_API_URL` is needed).
3. Select your development team under the MacroTracker target's Signing settings.
4. Choose an iPhone or simulator and run the `MacroTracker` scheme.

The first launch asks for an invite code. Claiming it issues a per-user device token that is stored in the device Keychain and sent as a Bearer header on every request. Sign out from the Coach tab (tap the coach avatar) to switch users; signing back in requires a new invite code.

`project.yml` is included for teams that use XcodeGen; the checked-in Xcode project works without XcodeGen.

## App icon

The AppIcon asset currently has a placeholder universal 1024×1024 slot. Replace it with the real app icon before submitting to the App Store.
