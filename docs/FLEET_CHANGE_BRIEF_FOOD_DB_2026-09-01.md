# Fleet Change Brief — food catalog and database reliability

- **Goal:** Replace ad-hoc food lookups with a growing PostgreSQL verified-food catalog and source observations; reduce the live lookup surface from curated + OpenAI classifier + OpenFoodFacts + Tavily to catalog + curated + one primary FatSecret adapter, with OpenFoodFacts as a temporary no-key fallback. Add ordered, checksummed database migrations and the first tenant/idempotency integrity layer without deleting or rewriting any legacy user data.
- **Owner/model:** Primary Builder, Codex GPT-5.6 Sol. One writer.
- **Allowed paths:** `server/src/**`, `server/tests/**`, `server/scripts/**`, `server/migrations/**`, `server/render.yaml`, `docs/**`, `INSTRUCTIONS.md`. **Forbidden:** `ios/**`, `web/**`, secrets, production data, deployment, git push, destructive schema changes.
- **Contract impact:** Existing response fields and endpoints must remain backward compatible. `lookup_food` should keep returning the current normalized macro shape. Optional idempotency support may be additive only; do not require a new client header in this backend-only change.
- **Data impact:** Additive schema only. Fresh verified backup: `/opt/data/backups/macro-tracker/20260901T042203Z/` (`pg_restore --list` and all SHA-256 checks passed). Provide an idempotent dry-run/backfill script. Preserve every legacy `nutrition_entries` row and original `macro_source` verbatim. Never label community/logged values as verified merely because they exist or repeat. Legacy rows with missing sources become `community_observed`/unverified. Rollups must remain bit-for-bit equal before/after.
- **Catalog requirements:**
  1. Add canonical `food_items`, aliases/identifiers, immutable source/evidence records, immutable nutrition observations, and a link from every legacy/new nutrition entry to its catalog item/observation where available.
  2. Normalize names deterministically; keep brand/barcode/provider IDs separate; dedupe by normalized identity without merging conflicting products.
  3. Verification states must distinguish official/curated, exact barcode/provider, imported community observation, estimate, and unknown. Only trusted-source observations are eligible for verified lookup.
  4. Preserve serving basis, serving text/grams, calories/protein/carbs/fat/fiber, provider, external ID, source URL/reference, retrieval time, confidence, and raw-evidence hash/structured metadata. Do not persist third-party raw payloads unless licensing explicitly permits it.
  5. On every successful new food log, update/link the catalog in the same database transaction as the legacy entry and rollup refresh. Composite messages remain one legacy nutrition entry but may store component metadata when available without changing the public response.
  6. Add catalog-first exact lookup and conservative alias lookup. Do not allow an unverified/community observation to answer as verified macros.
- **Provider requirements:**
  1. Remove the OpenAI food-classification call and Tavily from the active food lookup path and prompts/tool descriptions. Remove dead USDA use from the active path; do not reintroduce USDA.
  2. Add a FatSecret OAuth2 client-credentials adapter behind `FATSECRET_CLIENT_ID`/`FATSECRET_CLIENT_SECRET`, mocked in tests. Normalize food search/get results and servings to the existing lookup response.
  3. Respect licensing with `FATSECRET_CACHE_ALLOWED=false` by default. Persist FatSecret observations only when explicit cache permission is enabled; otherwise use the result only for the initiating user request/log snapshot. Keep attribution metadata.
  4. Keep curated restaurant data first and OpenFoodFacts exact barcode/text as temporary fallback while FatSecret credentials are absent. The ordered path should be deterministic, not selected by another LLM.
  5. Add timeouts, bounded retries for transport/429/5xx only, and provider-specific error isolation.
- **Migration requirements:**
  1. Add an ordered migration runner with PostgreSQL advisory lock, per-file SHA-256 checksum, transactional application, immutable `schema_migrations`, and checksum-drift failure.
  2. Stop running the full `schema.sql` from every normal request-process startup. Startup may verify compatible migration state and fail readiness cleanly; provide a deploy/prestart migration command.
  3. Add macro/domain checks and tenant-composite FKs for current linked tables where additive and safe. Add `request_operations` with `UNIQUE(user_id,idempotency_key)`, request hash, status, stored response, and conflict semantics plus focused tests. `POST /api/log` accepts the key optionally and does not force clients to send one.
  4. Add protected readiness output for DB connectivity and migration compatibility without writes or secret values.
- **Backfill requirements:**
  1. Script operates across all tenants but exposes no private row content in logs. Dry-run prints aggregate counts only.
  2. Idempotent second run changes zero logical rows. Use stable legacy keys/unique constraints.
  3. Create catalog items for all distinct logged names and presets; create immutable observations from unique macro/source/serving signatures; link entries. Missing provenance remains explicitly unverified.
  4. Include pre/post invariant SQL: table counts, tenant nulls, cross-tenant links, day/meal rollup equality, catalog link coverage, verified/unverified/estimate counts, duplicate keys.
- **Operational impact:** New optional FatSecret env vars; Tavily and OpenAI remain used elsewhere if applicable but no longer by food lookup. No cron changes in this change. Render database was already upgraded separately to paid `basic_256mb`, 5 GB, `expiresAt=null`.
- **Required reviewers:** Independent Quality/Data Safety reviewer from a different model family after implementation; independent Release/Ops verifier after deploy.
- **Verification target:** Full backend pytest, migration on a restored/throwaway database if available, migration second-run no-op, backfill dry-run then clone apply/reapply, all invariant queries zero-delta, code diff/secret scan, production canary after review and approval gates.
- **Rollback method:** Additive tables/columns remain ignored behind catalog/provider flags; revert application commit and return to legacy lookup path. Never delete target tables or user history during rollback. Restore backup only for catastrophic migration failure.
- **Status/evidence:** implemented and backend-automated-verified locally (296
  tests passed on 2026-09-01). Immutable migration hashes and seed dry-run were
  captured. Restored-database migration/backfill/reapply invariants and
  independent review remain required; no production action has been taken.
