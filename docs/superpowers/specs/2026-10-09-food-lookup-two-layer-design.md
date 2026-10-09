# Food Lookup v2: Two-Layer Search

Date: 2026-10-09. Status: approved by owner, not yet implemented.
Owner decisions: Layer 2 uses OpenAI built-in web search (no new vendors);
web-fallback results are one-tap confirm, never auto-logged.

## Problem

The food-resolution chain grew by accretion (curated menus, FatSecret,
OpenFoodFacts text search, generics, LLM estimates) and each layer added its
own failure mode. Confirmed findings:

1. Too many round trips: a simple food log can run 2-3 sequential LLM calls
   plus repeated DB reads plus a broad refresh; voice adds another agent hop.
2. Brittle fallback: unlisted foods hit a web step with a 2.5s timeout, and an
   infra failure surfaces as "give me a label" instead of "lookup unavailable".
3. No brand-first lookup with serving/variant matching or caching.
4. CONFIRMED BUG (cross-brand): `restaurant_menu.match_restaurant_items` picks
   the chain by finding a chain alias anywhere in the query. Panera's
   "Chipotle Chicken Avocado Melt" contains "chipotle", selects the Chipotle
   menu, then Chipotle's bare alias "chicken" (Adobo Chicken) matches. Result:
   Chipotle protein-cup macros logged for a Panera sandwich.
5. CONFIRMED BUG (portion basis): portion arithmetic is delegated to the LLM
   by prompt ("you own all portion arithmetic", server.py ~1292). When the
   model slips, a 200g portion is logged on the 100g basis. Nothing in code
   enforces scaling.

## Design

Two retrieval layers, nothing else.

### Layer 1: FatSecret, brand-first

- One text search per food component. Brand-aware matching replaces the
  current exact-identity gate: synonym normalization (bbq/barbecue etc.),
  token stems, and token-overlap scoring, same spirit as
  `agent_canvas.resolve_stored_name`.
- The server owns ALL portion math in code, basis-aware:
  per_serving vs per_100g scaling is computed and validated server-side.
  The LLM never performs portion arithmetic again (fixes bug 5).
- Variant disambiguation, any brand: when results contain close variants
  (fried/grilled, sizes), return one short pick-list question:
  "Chick-fil-A chicken sandwich: Fried (440 cal) or Grilled (320 cal)?"
  Reuses the existing clarification machinery; nothing hardcoded per chain.
- Cache confirmed resolutions (query -> provider item + serving) so repeat
  foods are instant and round trips drop.

### Layer 2: OpenAI web search fallback

- Runs only when Layer 1 returns nothing usable.
- One call to the existing OpenAI account using the built-in web-search tool:
  find the official/credible nutrition source for the named item, return
  macros + source URL + serving basis.
- Results are labeled estimates and ALWAYS one-tap confirm before any write.
- Infrastructure failure (timeout, provider error) says the lookup service
  was unreachable and to try again. It never asks the user to supply a label
  and never fabricates macros.

### Deletions

- Curated `restaurant_menu` as a matching layer: removed (bug 4's home).
  Its hand-verified rows become seed entries in the Layer 1 cache.
- OpenFoodFacts text search: removed. The OFF barcode path
  (`resolve_by_barcode`) STAYS; the barcode scanner is a different feature.
- Redundant lookup tools/branches in food_lookup collapse into the two layers.

## Invariants (unchanged creed)

- Never invent nutrition numbers; every write carries provenance.
- Tenancy isolation untouched; same write/idempotency path (`log_meal`).
- Estimates are labeled and confirmed; provider rows may auto-commit as today.
- Matt's data is never deleted.

## Implementation notes for the build session

- Entry points: `food_lookup.resolve_food`, the provider chain at ~line 651,
  relevance gates `_fatsecret_relevant` / `_relevant` / `_full_query_relevant`.
- Portion math enforcement replaces the prompt-delegated arithmetic in
  server.py (~1292) and the direct `macros_per_100g` consumers (server.py
  ~2170, food_lookup ~764/918).
- Update tests: test_food_lookup.py, test_everyday_unknown_macros.py; add
  cases for cross-brand non-contamination (Panera chipotle melt) and
  basis-scaling (200g on a per-100g row).
- Keep `_singularize`/generic whole-foods behavior for plain foods intact or
  fold into the Layer 1 cache; do not regress the sweet-potato fix.

## Related perf work (shipped separately)

- Canvas snapshot reads no longer queue behind in-flight turns (lock-free
  fast path) and per-turn latency telemetry (`mmacros.turn` rounds/elapsed)
  to guide cutting the 8-round LLM loop.
