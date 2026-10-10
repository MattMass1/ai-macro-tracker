# Food Lookup v2: Two-Layer Search

Date: 2026-10-09. Status: IMPLEMENTED (server suite green, 2026-10-09).
Owner decisions: Layer 2 uses OpenAI built-in web search (no new vendors);
web-fallback results are one-tap confirm, never auto-logged.

Implementation notes (what shipped, where it differs from the text below):

- Layer 1 gate: one identity rule (`_full_query_relevant`) is used both to
  pick which search hit to inspect and to accept it. Hits are ranked (exact
  item name before brand-anchored broader items) and at most two are fetched.
- Variant disambiguation: when no hit IS the named item but several
  acceptable brand-anchored hits differ in identity (Fried vs Grilled,
  sizes), `search_fatsecret` returns nothing and the coach asks which, listing
  them (`_ambiguous_variants`). An exact item name still auto-commits, so the
  common case stays one turn.
- Did-you-mean (`fatsecret_name_options`) reuses the hits from the failed
  resolution's own search; no second provider round trip. The same reuse
  means the whole-phrase probe and the component pass never search the same
  text twice (`_fatsecret_search_hits`, 60 s window).
- Round-trip budget (measured with a stubbed provider): a compound staple log
  ("2 eggs and 2 slices of bacon") fell from 5 provider searches to 1, a
  branded compound from 13 calls to 8. A compound phrase is probed whole
  only (its fragments can never pass the whole-phrase gate), and ladder
  fragments led by a connective or article are dropped (`_query_variants`).
- Outage honesty: `web_nutrition_lookup.recent_failure` exposes the fresh
  failure reason; `server._unresolved_food_question` says the lookup service
  was unreachable for infrastructure reasons and otherwise asks what the food
  was (brand/dish/preparation). The canvas and voice paths never ask for a
  nutrition label.
- OpenFoodFacts text search is deleted (`search_openfoodfacts`, `_relevant`);
  the barcode path (`search_openfoodfacts_by_code`) is unchanged.
- Curated `restaurant_menu` rows are retained as a module (future cache seeds)
  but are not consulted by the chain; seeding them into the Layer 1 cache is a
  data change for the owner to schedule.
- Legacy web `/api/chat` path: a lookup-sourced item with a stated weight is
  now re-scaled by the server from the resolved per-100g panel
  (`_upgrade_estimate`), so bug 5 cannot recur there either. Deliberately
  unchanged: that path's owner-approved allowance to log a flagged "ESTIMATE"
  when nothing resolves (`coach.py` SYSTEM_PROMPT/TOOLS,
  `_coach_tool_handlers.lookup_food`); only its stale provider wording was
  corrected. The canvas and voice paths never estimate.
- Presets are read once per food tool call and shared by every component it
  resolves (`server._turn_presets`, a call-scoped holder), instead of one DB
  read per component. No cross-call caching, so nothing can go stale after
  save_preset.
- Not changed: `coach.py max_rounds=8` is a safety cap, not a per-turn cost
  (a food log is one tool round plus the reply); cut it only against
  `mmacros.turn` telemetry.
- Production measurements (2026-10-09, end-to-end canvas text turns incl. the
  OpenAI brain, read-only macro questions, no writes), before -> after the
  variant short-circuit commit:
  - local staples ("2 eggs and 2 slices of bacon"): 5.4 s -> 3.8 s
  - Layer 1 branded hit (Chick-fil-A chicken sandwich, 420 kcal): 3.6 s -> 2.8 s
  - Layer 1 variant question (Panera BBQ smokehouse: whole/half/duet): 16.3 s -> 4.4 s
  - Layer 2 web estimate (200 g cooked red quinoa): 13.0 s verified on one run,
    "couldn't verify" after ~15 s on another -- the web layer is bounded by
    its 12 s search budget and is nondeterministic by design
  - unresolvable food (zebra steak): ~15 s, reported as "took too long"
  Layer 2's cost is the OpenAI web_search call itself; it only runs when
  Layer 1 has nothing, and it is never spent on a variant question.
- Voice path (ElevenLabs webhook `/api/voice/elevenlabs/agent-turn`, i.e. the
  `adapter="voice"` turn on the server, speech legs excluded), same questions:
  - staples: 25.7 s -> 2.9 s. The slow run was an unresolved compound paying
    the web budget per part and again for the whole phrase; the whole-phrase
    retry is now Layer 1 only, so the worst case is one web budget.
  - Chick-fil-A chicken sandwich: 2.8 s; Panera variant question: 3.7-4.5 s.
  Voice and text share this server path; the voice numbers differ from text
  only by model variance.
  Measurement recipe: POST `/api/agent-canvas/{uuid}/turn` with
  `{"turn_id": uuid, "message": "...do not log anything..."}`, diff
  `/api/today` meals before/after.

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
