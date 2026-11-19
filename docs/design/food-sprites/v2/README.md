# Macro-Bit food library v2

Status: candidate, not integrated. Asset-only continuation of the eight-icon v1 study. This pack contains 96 curated identities: the eight unchanged original SVG and 256px PNG masters plus 88 new original drawings. Coverage is curated everyday coverage, not empirical coverage of user logs.

Open `index.html` directly in a browser. It is a portable, offline static gallery with readable system-font labels and locally bundled Jersey 15 display headings. `sheets/index.json` lists 16 numbered category contact sheets. Every icon appears enlarged at 128px and at its exact 32px chat export on light and dark backgrounds. Numbers are stable review handles, not food database identifiers.

## Contents

- `svg/`: 96 editable, passive 32 x 32 logical-grid SVGs, intrinsic size 256px.
- `png/32`, `png/64`, `png/128`, `png/256`: transparent RGBA exports at four integer sizes, 384 files.
- `catalog.json`: stable namespaced IDs, category, display name, conservative proposed exact aliases, explicit fallback intent and asset paths. This is catalog data, not a functioning matcher.
- `original-eight.json`: hashes and v1 relative paths for retained original masters.
- `sheets/`: numbered contact-sheet PNGs, local HTML sources and sheet inventory.
- `STYLE-GUIDE.md`, `INTEGRATION.md`, `EXPANSION-GAPS.md`: art contract, integration limits and future suggestions.
- `QA.md`, `CHROME-CHECK.json`: visual-review findings and renderer-equivalence evidence, not independent approval.
- `tools/`: portable Python standard-library PNG/SVG codec, runnable validator and codec/negative tests. No installs required.
- `fonts/`: Jersey 15 Regular and its unchanged OFL license.
- `SHA256.json`: full immutable-file inventory, excluding the hash file itself.

## Validate offline

From this folder:

    python tools/test_validator.py
    python tools/validate.py
    python tools/validate.py --v1 ../v1 --zip /path/to/macro-bit-food-library-v2.zip

Use ordinary Python, not optimized `-O`. Validation reads and prints results; it does not change the pack. The optional v1 check compares actual source bytes. The optional ZIP check validates CRC, member inventory and every archived byte against this folder. Tests and automated integrity checks do not establish artistic recognition, app behavior or nutrition correctness.

The new artwork and its four exports follow integer nearest-neighbor scaling. Legacy exception: the unchanged broccoli SVG includes a diagonal stem edge. Its original 256px PNG is retained byte-for-byte; its newly derived 32/64/128px exports use the logical cell-center grid interpretation. See QA for measured difference. v1 itself is frozen and not modified.

## Provenance and rights

Illustrations are original AI-assisted, deterministically authored local SVG geometry made for Macro-Bit, not stock, emoji, screenshots, brand logos or paid model generations. The 88 new sources use filled integer rectangles and orthogonal stepped polygons. Colors follow the eight existing muted olive/natural-tone masters. The complete v1 SVG/256px originals and bundled font/license were copied unchanged. No exclusive-rights, legal clearance or empirical-recognition guarantee is made.

Jersey 15 Regular is redistributed under the included SIL Open Font License in `fonts/Jersey15-OFL.txt`; it is not relicensed by the illustration provenance statement. The font is embedded only in local preview headings; body labels use native system fallbacks. Inherited whitespace in the OFL file is intentionally preserved rather than rewriting the license.

AI-assisted concept - run a trademark search before commercial use.

This pack follows the selected sage/cream pixel Studio direction documented in v1, not legacy Design B rounded-card UI styling. It does not rewrite global guidelines. No application code, asset catalog, backend, food record, production account or machine configuration is changed. Independent read-only review is still required before the authorized asset-only commit/push; this candidate pack is not a merge, PR, deploy or release approval.
