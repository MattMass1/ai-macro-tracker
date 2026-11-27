# Plan 003: Integrate the 96-Sprite 8-Bit Food Library (`design/whole-food-sprites-v1`)

> **Executor instructions**: Follow this plan step by step on macOS with Xcode after Plan 002 merges. Run every verification command and confirm the expected result before moving to the next step. Honor every boundary in `docs/design/food-sprites/v2/INTEGRATION.md`. Do NOT add AI attribution anywhere.
>
> **Drift check (run first)**:
> `git diff --stat f62416a..HEAD -- ios-macrobit/MacroBit/FoodViews.swift ios-macrobit/MacroBit/CoachChatView.swift ios-macrobit/MacroBitTests/StudioResourceTests.swift`
> And verify the asset source commit exists: `git rev-parse design/whole-food-sprites-v1` (expected `e92480fcca4f3e72629bcbe73821a11cdeba9903`).

## Status

- **Repository**: `MattMass1/ai-macro-tracker` (`ios-macrobit/`, pulling art from branch `design/whole-food-sprites-v1`)
- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: `002-ios-8bit-visuals-and-buttons.md`
- **Category**: direction / visual-detail
- **Planned at**: `master` commit `f62416a`, asset branch `e92480f`, 2026-11-27

## Why this matters

Branch `design/whole-food-sprites-v1` already contains a validated, palette-matched **96-sprite 32×32 logical-pixel food library** (`docs/design/food-sprites/v2/`: 96 SVGs, 384 nearest-neighbor RGBA PNGs at 32/64/128/256px, `catalog.json`, `STYLE-GUIDE.md`, and `INTEGRATION.md`). Meanwhile, `ios-macrobit` still renders Apple system emoji (`Text(item.emoji)`) on saved-meal buttons (`PixelPresetGrid`, `savedMealStrip`) and displays zero food sprites on `ChatSurfaceCard` previews and receipts. Integrating this pack immediately elevates Macro-Bit from text+emoji cards into an authentic 8-bit food-tracking game—without changing a single backend endpoint or nutrition rule.

## Current state

- `design/whole-food-sprites-v1` at `docs/design/food-sprites/v2/`:
  - `catalog.json` — 96 entries with stable namespaced IDs (`ingredient.*`, `prepared.*`, `drink.*`, `fallback.food`, `fallback.meal`), `display_name`, conservative exact `aliases`, and PNG paths (`png/32/`, `png/64/`, `png/128/`).
  - `INTEGRATION.md` — binding rules:
    1. Use only conservative exact lowercase alias matching (no fuzzy substring guessing—never pick `salmon` for an unknown salmon-named compound dish by substring).
    2. Use `fallback.food` for unmatched single foods and `fallback.meal` for unknown/compound dishes.
    3. Always keep the authoritative native food name visible alongside the sprite; treat the sprite as decorative (`.accessibilityHidden(true)`).
    4. Never infer nutrition, portion, cooking state, or allergens from a sprite; missing art must never block logging.
    5. Render at integer scales (`32pt` in chat cards, `32pt` or `40pt` in preset tiles) with `.interpolation(.none).antialiased(false)`.
- `ios-macrobit/MacroBit/FoodViews.swift`:
  - `PixelPresetGrid.tile(_ item:)` (line 364) renders `Text(item.emoji).font(.title2)`.
- `ios-macrobit/MacroBit/CoachChatView.swift`:
  - `savedMealStrip` (line 210) renders `Text(item.emoji).font(.title3)`.
  - `ChatSurfaceCard` (lines 305–335) renders food rows and receipt entries as text only.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Validate source sprite pack | `git show design/whole-food-sprites-v1:docs/design/food-sprites/v2/catalog.json \| python3 -m json.tool > /dev/null` | exit 0 |
| Full Xcode build + tests | `xcodebuild clean test -project ios-macrobit/MacroBit.xcodeproj -scheme MacroBit -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO` | `TEST SUCCEEDED`, 0 failures |

## Scope

**In scope:**
- `ios-macrobit/MacroBit/Resources/Assets.xcassets/FoodSprites/` (new folder containing `.imageset`s imported from `design/whole-food-sprites-v1:docs/design/food-sprites/v2/png/{32,64,128}/` plus `food-sprite-catalog.json` bundled in `Resources/`)
- `ios-macrobit/MacroBit/FoodSprites.swift` (new file: `FoodSpriteCatalog`, `FoodSpriteView`)
- `ios-macrobit/MacroBit/FoodViews.swift` (`PixelPresetGrid`, `SavedMealEditor` sprite preview)
- `ios-macrobit/MacroBit/CoachChatView.swift` (`savedMealStrip`, `ChatSurfaceCard`)
- `ios-macrobit/MacroBit.xcodeproj/project.pbxproj` (register `FoodSprites.swift` and `food-sprite-catalog.json`)
- `ios-macrobit/MacroBitTests/StudioResourceTests.swift`

**Out of scope:**
- Do NOT merge `docs/design/food-sprites/v1/` or preview HTML galleries into the app bundle.
- Do NOT modify `APIClient.swift`, `Models.swift`, or backend food matching.
- Do NOT remove `Preset.emoji` from API models (`SavedMealEditor` keeps emoji compatibility with the backend API).

## Git workflow

- Branch: `app/macrobit-food-sprites` from `master` (after Plan 002 merges)
- Commit style: `Macro-Bit: integrate 96-sprite 8-bit food library into cards and saved meals`
- No AI attribution anywhere.

## Steps

### Step 1: Import 32/64/128px PNGs into `Assets.xcassets/FoodSprites` as 1x/2x/3x imagesets

1. Extract `docs/design/food-sprites/v2/catalog.json` and `png/{32,64,128}/*.png` from `design/whole-food-sprites-v1` (using `git archive design/whole-food-sprites-v1 docs/design/food-sprites/v2 | tar ...` into a temporary staging dir outside the target bundle, then generating `.imageset` folders).
2. Create `ios-macrobit/MacroBit/Resources/Assets.xcassets/FoodSprites/Contents.json` (`{"info":{"author":"xcode","version":1}}`).
3. For each of the 96 entries in `catalog.json`:
   - Asset name: `"food-\(id.replacingOccurrences(of: ".", with: "-"))"` (e.g. `food-ingredient-banana`, `food-fallback-food`, `food-fallback-meal`).
   - Map `png/32/<file>.png` → `1x`, `png/64/<file>.png` → `2x`, `png/128/<file>.png` → `3x` in `<asset>.imageset/Contents.json` (preserves exact crisp 32pt logical grid across `@2x`/`@3x` iPhones, avoiding broccoli's historical 256px diagonal edge as noted in `INTEGRATION.md`).
4. Copy a stripped runtime manifest `ios-macrobit/MacroBit/Resources/food-sprite-catalog.json` containing only `{ id, assetName, aliases, isFallback }` and add it to the `MacroBit` target's Copy Bundle Resources in Xcode.

**Verify**: `ls ios-macrobit/MacroBit/Resources/Assets.xcassets/FoodSprites/*.imageset | wc -l` → `96`.

### Step 2: Implement conservative `FoodSpriteCatalog` & `FoodSpriteView`

Create `ios-macrobit/MacroBit/FoodSprites.swift`:
1. `FoodSpriteCatalog.shared`: loads `food-sprite-catalog.json` once from `Bundle.main`, builds an exact lowercase trimmed `[String: String]` dictionary mapping each reviewed alias → `assetName`.
2. `func assetName(forFoodName raw: String, isCompoundMeal: Bool = false) -> String`:
   - Normalize `raw` (`trimmingCharacters(in: .whitespacesAndNewlines).lowercased()`, collapsing internal whitespace).
   - Check exact match in `aliasMap`. If found, return that `assetName`.
   - Never perform substring matching (strictly honors `INTEGRATION.md`: `"Do not choose salmon for an unknown salmon-named compound dish merely by substring"`).
   - Otherwise return `isCompoundMeal ? "food-fallback-meal" : "food-fallback-food"`.
3. `struct FoodSpriteView: View`:
   - Props: `name: String`, `isCompoundMeal: Bool = false`, `size: CGFloat = 32`.
   - Renders `Image(assetName).resizable().interpolation(.none).antialiased(false).frame(width: size, height: size).accessibilityHidden(true)` inside a subtle recessed 8-bit frame (`Theme.paper` + 1px `Theme.edge` pixel border) when used on tiles so dark sprite outlines stay crisp on both light and dark surfaces.

**Verify**: Build & run `MacroBitTests` → `TEST SUCCEEDED`.

### Step 3: Wire `FoodSpriteView` into Saved Meals and Chat Surface Cards

1. In `ios-macrobit/MacroBit/FoodViews.swift`:
   - In `PixelPresetGrid.tile(_ item:)`, replace `Text(item.emoji).font(.title2)` with `FoodSpriteView(name: item.name, isCompoundMeal: true, size: 32)` (if the user customized an explicit non-default emoji that does not map to a known sprite, optionally show the sprite with the small emoji badge OR show sprite when exact-matched and fall back gracefully to `FoodSpriteView` with `food-fallback-meal`).
2. In `ios-macrobit/MacroBit/CoachChatView.swift`:
   - In `savedMealStrip`, replace `Text(item.emoji).font(.title3)` with `FoodSpriteView(name: item.name, isCompoundMeal: true, size: 28)`.
   - In `ChatSurfaceCard`:
     - On `.mealPreview` food rows (`ForEach(meal.foods)`), render `HStack(alignment: .top, spacing: 10)` with `FoodSpriteView(name: food.name, isCompoundMeal: false, size: 32)` beside the food name, portion, kcal, and source.
     - On `.mealReceipt` entry rows, render `FoodSpriteView(name: entry.name, isCompoundMeal: true, size: 32)` beside `entry.name`.

**Verify**: Run full `xcodebuild clean test` → `TEST SUCCEEDED`, and visually inspect updated offline screenshots (`09-chat-preview-OFFLINE.png`, `10-chat-receipt-OFFLINE.png`, `40-home-saved-meals.png`).

## Test plan

Add tests in `ios-macrobit/MacroBitTests/StudioResourceTests.swift`:
- `testAll96FoodSpritesAndFallbacksResolve` — load `food-sprite-catalog.json`, assert count == 96, and assert `UIImage(named: entry.assetName) != nil` for every single sprite including `food-fallback-food` and `food-fallback-meal`.
- `testFoodSpriteMatchingIsStrictlyConservative` — `"Banana"` / `"banana"` → `food-ingredient-banana`; `"Salmon teriyaki rice bowl"` → `food-fallback-meal` (NOT `food-ingredient-salmon`); `""` → `food-fallback-food`.

## Done criteria

- [ ] All 96 imagesets exist in `Assets.xcassets/FoodSprites/` and pass `testAll96FoodSpritesAndFallbacksResolve`.
- [ ] Conservative matching test passes (zero substring false positives).
- [ ] `xcodebuild clean test` passes all unit and UI tests with 0 failures.
- [ ] No backend or API model files were modified.

## STOP conditions

- Stop if `catalog.json` on `design/whole-food-sprites-v1` has missing PNG files for any entry in `png/{32,64,128}/`.
- Stop if adding `FoodSprites` causes `StudioUITests` layout overlap or exceeds 44pt/accessibility wrapping bounds on iPhone SE / standard simulator widths.

## Maintenance notes

- To support more curated aliases later, only add explicitly reviewed exact strings to `food-sprite-catalog.json` (never regex/substring matching) and keep `INTEGRATION.md`'s safety rules intact.
