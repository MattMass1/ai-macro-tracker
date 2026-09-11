# Macro Tracker for iOS

Native SwiftUI client for iOS 17 and later.

## Run

1. Open `MacroTracker.xcodeproj` in Xcode.
2. Copy `Config.xcconfig.template` to `Config.xcconfig` (only `MACRO_API_URL` is needed).
3. Select your development team under the MacroTracker target's Signing settings.
4. Choose an iPhone or simulator and run the `MacroTracker` scheme.

The first launch asks for an invite code. Claiming it issues a per-user device token stored in the Keychain and sent as a Bearer header on every request. Existing device tokens are reused. Sign out from the Coach avatar to switch users; signing back in requires a new invite code.

Open Coach, tap the voice button, and start a session. Voice can log meals, read consumed and remaining macros, suggest exercise replacements, change today's workout, and log sets. Specify the exercise, reps, weight, and pounds or kilograms. The coach asks for missing details before saving. Saved actions refresh today's macros, workout plan, set history, and progress during the session and when it ends.

The native transport integration test requires the loopback fixture in `server/tests/live_native_transport_fixture.py` listening on port 18765. Start it with the server dependencies installed and `PYTHONPATH=server/src:server/tests`, then run the MacroTracker test scheme. It uses test credentials and a fake provider, not production data.

`project.yml` is included for teams that use XcodeGen; the checked-in Xcode project works without XcodeGen.

## App icon

The AppIcon asset references the app's 1024×1024 `AppIcon.png`. Include that resource when creating a device or distribution build.
