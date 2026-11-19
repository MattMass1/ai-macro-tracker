# Integration boundaries (proposal only)

No SwiftUI integration, asset-catalog membership, matcher, backend change or food-data mutation is included or tested. The pack is candidate-not-integrated.

`catalog.json` uses stable namespaced IDs: `ingredient.*`, `prepared.*`, `drink.*`, `fallback.*`. IDs identify artwork candidates, not canonical nutrition database IDs. Display names and category placement are curated conventions, not a food ontology. Ingredient and prepared-dish IDs remain separate, even when a dish can contain that ingredient.

Each proposed exact alias is deliberately conservative: the lower-case display spelling only, with no fuzzy synonyms, substrings, stemmer or language inference. Fallback entries have no food aliases. Consumers may propose a reviewed canonical-ID lookup later; this catalog does not detect arbitrary food names. Do not choose salmon for an unknown salmon-named compound dish merely by substring. Do not treat a generic fish or poultry illustration as reliable species recognition.

Suggested behavior after a separate integration task:

1. Prefer a reviewed canonical food/dish ID to an artwork ID.
2. Use only explicitly reviewed exact aliases when structured identity is absent. Display the authoritative food name alongside the icon.
3. Reuse the same artwork across amount and serving variants. Do not infer or alter portion, nutrition, cooking state, ingredient list or allergens from a sprite.
4. For unmatched individual foods use `fallback.food`. For unknown or compound dishes use `fallback.meal`. Missing assets, unsupported names and ambiguous identity must never block logging or replace the logged food text.
5. Treat the icon as decorative when the adjacent native label is present. Verify accessibility labels, Dynamic Type, reduced-contrast contexts, asset interpolation and actual32px rendering on device during integration.

The `default_fallback` field is an explicit proposed artwork fallback, not executable application behavior. `fallback_intent` explains the two neutral assets; ordinary entries use null. No measurement, detector, nutrition matcher or allergy-safety assertion is hidden in catalog metadata.

Preserve the original eight filenames at matchingSVG/256 sizes. New IDs should remain stable even if a future artwork version changes. Broccoli's retained256 PNG contains its historical diagonal edge; prefer the derived32/64/128 files at their matching scales rather than assuming all historical256 edges are exact32-grid repeats.

Native builds, device/UI tests, semantic mapping review, canonical food-data review and production rollout all remain separate authorized tasks. Do not merge this asset branch or wire it into production from this pack alone.
