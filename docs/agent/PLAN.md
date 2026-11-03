# Food agent v1: build plan

Status: plan for a local build on branch `claude/food-agent-v1` (base `cc00470`). Nothing here is pushed, deployed, migrated or enabled.
Brief: DevOps `mmmacros-agent-build/AGENT.md`. Product spec: DevOps `mmmacros-agent-spec/SPEC.md`.

## Ground truth from the code (read before building)

- Turn boundary: `CanvasService.turn` (`server/src/agent_canvas.py:655`), reached from `POST /api/agent-canvas/{id}/turn`. The model is injected as `agent=` (default `coach.run_agent`), so tests script it without network.
- Drafts (migration 004, applied in production per the brief) already give owner-scoped, revisioned drafts (`meal_drafts.revision` CAS in `Store.save_meal_draft`), and an idempotent commit `Store.commit_meal_draft` (`run_idempotent` key `meal-draft:{id}:{rev}`) with readback `Store.fetch_entry_revision`.
- **Confirmation gap:** `MealDrafts.handle` commits any `ready` consume draft in the same call (`meal_drafts.py:394`), and `settle()` commits at end of turn. The new path must never do this.
- No owner-scoped history search by food name exists. `lookup_catalog` is shared, not per user.
- `nutrition_entries` has no portion or identity columns; portion and identity go in `component_metadata` (JSONB) on the saved row.
- `langgraph` / `langchain` are not installed, and this run may not install packages. Pydantic 2.14.2 is available.

## Design in one paragraph

One new package `server/src/food_agent/`. Per turn: one structured model call returns a typed `AgentAction` (Pydantic v2, strict, `extra="forbid"`). Code validates it, runs typed tools (`find_food`, `recall_meal`, draft edits), saves the draft through the existing CAS draft store, and shows a preview bound to `(draft_id, revision, content_hash)`. A commit happens only when the user's own message is an unambiguous yes that code detects, exactly one open preview exists, and its revision and hash still match. The model cannot confirm. The commit goes through `Store.commit_meal_draft` (idempotent by draft revision). The agent then reads the row back and builds the receipt from it. A lost response or failed readback is reported as "could not verify it saved" and reconciled by the same operation key. The gate `FOOD_AGENT_ENABLED=true` (with optional `FOOD_AGENT_USER_IDS`) plus the live 004 schema check route the turn; with the gate off, `CanvasService.turn` is byte-for-byte the old path. Non-food turns are handed to the existing canvas path. No new migration is expected. The preview binding lives in `meal_drafts.pending_question` (JSON text, at most 1000 chars) and identity/portion/modifications live in `meal_draft_items.resolution` and the committed row's `component_metadata`.

## Work packages

Every acceptance behavior (AGENT.md §5) is written as a test that is run RED before the code, then GREEN. Writers record the RED run in their report.

| WP | What | Files | Model + reason | Proves it | Depends |
|---|---|---|---|---|---|
| 1 | Baseline + env | none (orchestrator) | orchestrator; quick re-check only (DevOps built env) | offline 1355/28s, PG 1382/1 pre-existing fail | none |
| 2 | Typed contracts + ADR-001 | `server/src/food_agent/__init__.py`, `contracts.py`, `docs/agent/ADR-001.md`, `server/tests/test_food_agent_contracts.py`, `server/requirements.txt` (pydantic) | **Opus**: designing the action schema, draft/preview contract and confirmation state machine | schema rejects extra fields like `confirmed`, nutrients and user ids; identity/portion/mods are separate types; portion math is pure and tested | 1 |
| 3 | Coordinator + confirmation/revision binding + commit/readback/unknown outcome | `food_agent/coordinator.py`, `food_agent/confirm.py`, small additive read methods in `server/src/store.py` (operation lookup by key), `server/tests/test_food_agent_flow.py`, `server/tests/test_food_agent_pg.py` | **Opus**: confirmation, idempotency and unknown-outcome logic | §5 items 4-8, 11 | 2 |
| 4 | `find_food` wrapping existing resolvers, identity guards, portion math | `food_agent/find_food.py`, `server/tests/test_food_agent_find_food.py` | **Opus**: identity vs portion vs modifications matching | real failures 2 and 3 at the tool level; fractions of servings; oz vs serving oz; named product means "matched to X" | 2 (parallel with 3: disjoint files) |
| 5 | `recall_meal` from owner-scoped history | `food_agent/recall.py`, one additive owner-scoped search method in `store.py`, `server/tests/test_food_agent_recall.py` | **Opus** design + implementation; **Sonnet** adds the extra PG tests afterwards | real failure 1; "not found" is honest; cross-tenant history never matches (§5 item 9) | 3 (store.py writer must be done) |
| 6 | Release gate + canvas hook + gate-off regression | `server/src/release_gates.py`, `server/src/agent_canvas.py` (hook only), `server/src/server.py` (factory only), `server/tests/test_food_agent_gate.py` | **Sonnet** (well specified), **Opus** review in WP9 | gate exact-`true`, canary list, malformed list closes; gate-off turn identical to baseline (§5 item 10); whole dialogues through `CanvasService.turn` for the three real failures | 3, 4, 5 |
| 7 | Migration | none expected | **Opus** only if 3-5 prove a table is required | n/a unless needed | 3-5 |
| 8 | Bench harness + fixtures | private labels and results in DevOps `mmmacros-agent-build/bench/` (NOT in repo); committed synthetic fixtures `server/tests/fixtures/food_agent_bench_synthetic.json`, harness `server/scripts/food_agent_bench.py`, test `server/tests/test_food_agent_bench.py` | **Sonnet** builds harness and fixtures; **Opus** reviews the scorer | scores per AGENT.md §8, numerator/denominator | 6 |
| 9 | Independent review + fix loop | review report in DevOps dir; fixes in a separate commit | **Opus** reviewer (never a writer); fixes by the original writer's model | BLOCKER/SHOULD-FIX resolved; probes become repo tests | 8 |
| 10 | Findings report | DevOps `FINDINGS.md`, `findings.html` | orchestrator | section 10 of the brief | 9 |

## Rules for every writer

- Only edit the files listed for your WP. Report problems elsewhere to the orchestrator.
- Never touch `web/lib/types.ts`, `web/lib/apiClient.ts`, `ios/`, or applied migrations.
- Tests never reach the network. Mock every provider. Use synthetic user UUIDs only.
- Unknown nutrients stay `None` end to end, never 0.
- Run `bash .../run_tests.sh` and, for PG work, `bash .../run_pg_tests.sh`. Report real counts.
- Do not commit. The orchestrator commits with explicit `git add`.

## Lane C (live provider evaluation)

Not run in this session (non-interactive; needs Matthew's go). The proposed plan is written into FINDINGS.md as a decision.
