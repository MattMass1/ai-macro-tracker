# Native chat surfaces verification

Simulator acceptance verified on 2026-11-20: 37 tests passed, zero failed/skipped, on a fresh task-owned iPhone 17e / iOS 26.5 simulator. This is offline simulator evidence, not real-device or production verification.

The canonical synthetic response is Verification/Fixtures/chat-surfaces.json. The identical file is in the backend contract tests. OfflineFixtures.swift embeds the same four samples under DEBUG, and ContractTests verifies that they decode identically. No real user or backend data is used.

Implemented:
- Four discriminated native response types in CoachChat.swift.
- Per-card tolerant decoding: unknown kinds, primitives, nulls and malformed peers do not erase valid cards.
- Null nutrient preservation, finite/nonnegative nutrition checks, signed remaining values, canonical date/meal validation and at most four readable cards.
- Existing ProfileMetrics detection survives malformed peers; original text and quick replies are retained.
- AppStore propagates readable cards into the message feed; cards replace the corresponding bubble and unreadable responses fall back to text.
- Native Theme/PixelPanelShape/appCard/Jersey 15 cards reuse PixelBar. No food icons or generated UI are added.
- Confirm meal sends the existing Log it quick reply, never a save route. Earlier buttons are invalidated before sending. Cancel remains; duplicate Log it buttons are suppressed. Edit portion only prefills make it 2 servings.
- Receipts retain saved values and refresh today through the existing successful-turn path. Unsafe save failures offer no retry action. A future safe flag exposes Review & retry, which only prefills a totals-review request and does not save/resend confirmation.
- Code-owned failed-edit/nutrition-change notices, requested modifications and standard-recipe qualification remain visible in cards. Shortest round-trip number formatting preserves authoritative fractional values instead of rounding them to one decimal.
- Macro layout becomes single-column at accessibility text sizes. Existing PixelBar supplies bounded climbing animation and respects Reduce Motion; unknown nutrients are text, never zero-valued bars.

Owner design authority is the existing native app. No external artifact dependency remains.

Offline UI requests: a banana, Show my daily totals, simulate save failure, simulate safe retry, simulate changed nutrition, simulate failed edit, simulate modified meal and fractional totals. All are explicitly synthetic, DEBUG-only, network-free fixtures, not real saves. The safe-retry flag is a UI test vector, not a capability currently advertised by the backend failure builder.

Owner-approved replay exception: while open/retained, the backend replays identical preview JSON without new model/day reads. Confirmation, cancellation, replacement or expiry purge source results and replay safe closed text without stale cards or confirmation authority. Receipts use their original digest-bound operation snapshot; totals use the frozen turn snapshot. No migration is needed for additive existing JSONB fields.

The backend feature PR https://github.com/MattMass1/mmmacros-backend/pull/23 must deploy before the app feature PR is used. No migration, deployment, real-device or live-user-data success is claimed.

Acceptance command:

    xcodebuild test -project ios-macrobit/MacroBit.xcodeproj -scheme MacroBit -destination 'id=2FEED27A-935C-476F-8B9C-C9BF2AF61F21' CODE_SIGNING_ALLOWED=NO -parallel-testing-enabled NO -derivedDataPath ../resume-acceptance-derived-data -resultBundlePath ../logs/resume-acceptance.xcresult

The 78-file source manifest was verified before and after testing. All compiled Swift files match that manifest. Only this verification document and newly copied screenshots were added after the run. Full commands, source hashes, logs, result bundle location, prior failures and reviews are retained in the task's RESUME-REPORT.md and RESUME-RESULT.json.

New inspected native captures in Screenshots/:
- 09-chat-preview-OFFLINE.png: preview actions, unknown protein and day-after bars.
- 14-chat-preview-top-OFFLINE.png: complementary scroll position with title, food, portion and source. The tall card requires scrolling; neither image is a fabricated stitched screen.
- 10-chat-receipt-OFFLINE.png: full saved receipt and updated day bars.
- 11-chat-day-totals-OFFLINE.png: totals and unknown-not-zero qualification.
- 12-chat-save-failed-OFFLINE.png: unsafe failure with no retry action.
- 13-chat-safe-review-OFFLINE.png: safe review action and no-save/no-resend explanation.
- 15-chat-cancel-OFFLINE.png: Cancel message, no active confirmation or receipt.
- 16-chat-nutrition-change-OFFLINE.png and 17-chat-failed-edit-OFFLINE.png: readable preserved notices.
- 19-chat-fractional-totals-OFFLINE.png: 400.25 kcal and 1599.75 kcal remaining.

Mounted tests check portion editing only prefills, a single Log it confirmation, cancellation, safe review only prefills, preserved notices/modifications/standard-recipe qualification and fractional values. Unit tests cover all kinds, malformed/unknown peers, text fallback, metrics-form detection, null preservation and source precision. Qualification assertions passed, but the supplemental qualification screenshot was rejected as standalone evidence because its modification line met the top viewport edge.

Residual limits: four invalid-frame runtime warnings remain in the final xcresult; their origin was not established and they are not presented as fixed. AppIntents metadata extraction reports no framework dependency. Larger accessibility sizes are supported in source but were not exhaustively visually audited; font/resource, PixelBar timing, bounds and Reduce Motion tests passed. Earlier renderer ui-green failed with 31 passes and two UI-query timeouts; the resumed source passed four full runs (36, 37, 37 and 37 tests). Screenshot-only harness refinements added explicit vertical drags and a capture settling interval; production renderer code was not changed during resume. No live backend or real SQL integration was exercised.
