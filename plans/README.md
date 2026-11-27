# Macro-Bit Implementation Plans & Execution Index

Generated on 2026-11-27.

## Repository Topology (Why There Are Two Repos)

- **`MattMass1/ai-macro-tracker`** (`master` @ `f62416a`) — holds the **active iPhone app** under `ios-macrobit/` (`Macro-Bit`, SwiftUI) plus legacy `server/` (old Python), `web/` (old Next.js), and `ios/` (old `MacroTracker`). Branch `design/whole-food-sprites-v1` (`e92480f`) holds the unmerged 96-sprite 8-bit food library.
- **`MattMass1/mmmacros-backend`** (`main` @ `ed8c683`) — holds the **active TypeScript/Node 24 + Postgres API backend** deployed on Render at `https://mmmacros-backend.onrender.com`, which `ios-macrobit` calls via `MACRO_API_URL`.

## Execution Order & Status

| Plan / PR | Repo | Title | Priority | Effort | Depends on | Where to Run | Status |
|---|---|---|---|---|---|---|---|
| Backend PR | `mmmacros-backend` | Fix food lookup pipeline reliability (parallel `needs_web`, `ModelBudgetError` handling, expired `fdc_fact` eviction, O(1) `findById` on confirm) | P1 | S | — | Automated via PR + GitHub Actions CI | IN PROGRESS |
| `005` | `mmmacros-backend` DB | Verify & safely link `massmatt516@me.com` head account (read-only audit + non-destructive link SQL) | P1 | S | — | Operator `psql` on Render Postgres | TODO |
| `001` | `ai-macro-tracker` | Simplify onboarding (fix Studio flash race, Character Select + Quest flow) & fix client glitches (`0 OVER`, silent home errors, preset refresh, `ChatJournal` cards) | P1 | M | — | MacBook + Xcode Simulator | TODO |
| `002` | `ai-macro-tracker` | Unify 8-bit visual system, 3D `PixelBevel` buttons, RPG dialogue boxes, pixel forms/charts/scanner, and custom pixel tab icons | P1 | L | `001` | MacBook + Xcode Simulator | TODO |
| `003` | `ai-macro-tracker` | Integrate validated 96-sprite 32×32 food library from `design/whole-food-sprites-v1` into saved meals and chat cards | P2 | M | `002` | MacBook + Xcode Simulator | TODO |
| `004` | `ai-macro-tracker` | Add procedural 8-bit chiptune `PixelAudio` SFX, iOS 17 haptics, interactive Companion reactions, and derived Daily Quests + Day Streak HUD | P2 | L | `002` | MacBook + Xcode Simulator | TODO |

## Dependency Notes

- **Backend PR** and **Plan `005`** are independent of the iOS Xcode plans and can land immediately.
- **Plan `001`** should merge before **`002`** because both edit `Onboarding.swift`, `CoachChatView.swift`, and `FoodViews.swift`.
- **Plans `003` and `004`** both build on **`002`**'s unified pixel components and can be executed in parallel or in either order once `002` lands.

## Findings Considered and Rejected

- **`AppStore.checkSetup()` setting `needsSetup = false` on error (`AppStore.swift:86-90`)**: Intentionally designed and unit-tested in PR #26 so a transient backend/cold-start failure never traps a user on the onboarding screen. Keep as-is.
- **Strict fresh-row capacity ceiling in `FdcCacheRepository.put()`**: Intentionally tested in `test/storage/fdc-cache.integration.test.ts`. Evict *only* expired (`> 7 days`) rows when at capacity, preserving the hard ceiling on fresh records.
- **Failed USDA provider line aborting `prepare()` (`test/runtime/fdc-runtime.test.ts`)**: Intentionally prevents authorizing a partial meal when USDA returns HTTP 429/500 or times out. Keep `failed`/`timeout` fail-closed.
