"""Official restaurant nutrition, end to end at the real turn boundary.

Chick-fil-A milestone: official-source ingestion (registry -> direct -> Exa
discovery -> Firecrawl extraction), identity/evidence validation, versioned
shared Postgres records, chat retrieval through the production canvas turn and
meal-draft engine, a half-serving correction, and reuse with no network call.

Runs only against a throwaway local PostgreSQL (MMMACROS_TEST_PG_ADMIN_URL).
Synthetic users only. The ONLY faked layer is provider HTTP: every Exa and
Firecrawl response below is a FIXTURE (labeled as such), never a live menu
extraction. The fixture panel copies the values already curated in
``restaurant_menu.py`` for the Chick-fil-A Chicken Sandwich.
"""
from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

import httpx
import pytest

ADMIN_URL = os.environ.get("MMMACROS_TEST_PG_ADMIN_URL", "")
pg = pytest.mark.skipif(not ADMIN_URL, reason="set MMMACROS_TEST_PG_ADMIN_URL to a throwaway local PostgreSQL")

CFA_PAGE = "https://www.chick-fil-a.com/menu/chick-fil-a-chicken-sandwich"

# FIXTURE (not live): the shape of a Firecrawl v2 /scrape response with a JSON
# format. Values mirror restaurant_menu.py's curated Chick-fil-A row.
FIXTURE_MARKDOWN = (
    "# Chick-fil-A® Chicken Sandwich\n"
    "A boneless breast of chicken seasoned to perfection, on a toasted, buttered bun with dill pickle chips.\n"
    "## Nutrition\nServing size 1 sandwich\nCalories 420\nFat 18g\nSodium 1460mg\n"
    "Carbohydrates 41g\nFiber 1g\nSugar 6g\nProtein 29g\n"
)
FIXTURE_PANEL = {"item_name": "Chick-fil-A® Chicken Sandwich", "serving_size": "1 sandwich",
                 "serving_grams": None, "calories": 420, "protein": 29, "carbs": 41, "fat": 18, "fiber": 1}


def fixture_firecrawl(panel=None, markdown=FIXTURE_MARKDOWN, final_url=CFA_PAGE):
    return {"success": True, "data": {
        "markdown": markdown, "json": dict(panel or FIXTURE_PANEL),
        "metadata": {"sourceURL": CFA_PAGE, "url": final_url, "statusCode": 200,
                     "title": "Chick-fil-A® Chicken Sandwich"}}}


def fixture_exa(urls=(CFA_PAGE,)):
    return {"results": [{"url": url, "title": "Chick-fil-A Chicken Sandwich"} for url in urls]}


class FixtureHTTP:
    """FIXTURE provider transport: records every request; never touches the network."""

    def __init__(self, *, exa=None, firecrawl=None):
        self.calls: list[tuple[str, dict]] = []
        self.exa = exa if exa is not None else fixture_exa()
        self.firecrawl = firecrawl if firecrawl is not None else fixture_firecrawl()

    async def post_exa(self, key, payload):
        assert key == "fixture-exa-key"
        self.calls.append(("exa", payload))
        return httpx.Response(200, json=self.exa)

    async def post_firecrawl(self, key, payload):
        assert key == "fixture-firecrawl-key"
        self.calls.append(("firecrawl", payload))
        return httpx.Response(200, json=self.firecrawl)

    def count(self, name):
        return sum(1 for call, _ in self.calls if call == name)


def _db_url(name: str) -> str:
    base, _, query = ADMIN_URL.partition("?")
    root = base.rsplit("/", 1)[0]
    return f"{root}/{name}" + (f"?{query}" if query else "")


@pytest.fixture
async def env(monkeypatch):
    import asyncpg
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    monkeypatch.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import migrations
    import official_nutrition
    import server as srv
    import web_nutrition_lookup
    from store import Store

    name = "mmm_official_" + uuid4().hex[:12]
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    url = _db_url(name)
    # Mirror production: 000-004 applied (verified read-only 2026-11-01).
    approvals = ",".join(f"{f}:{migrations.checksum(migrations.MIGRATIONS_DIR / f)}"
                         for f in ("003_calorie_only_unknown_macros.sql", "004_meal_drafts.sql"))
    monkeypatch.setenv("APPROVED_MIGRATIONS", approvals)
    applied = await migrations.apply_migrations(url)
    assert "004_meal_drafts.sql" in applied
    conn = await asyncpg.connect(url)
    users = [uuid4(), uuid4()]
    for user in users:
        await conn.execute("INSERT INTO users(id, display_name) VALUES($1, 'Synthetic test user')", user)
    await conn.close()

    async def connect(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
        return self.pool
    monkeypatch.setattr(Store, "connect", connect)
    store = Store(url)
    monkeypatch.setattr(srv, "_client", store)

    # Every other provider is offline: a hit can only come from the official path.
    monkeypatch.setattr(srv.food_lookup.cofid_lookup, "lookup", lambda _q: asyncio.sleep(0, result=None))
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    monkeypatch.setattr(srv, "_closest_food_options", lambda _d: asyncio.sleep(0, result=[]))

    web_calls = []

    async def no_web(query):
        web_calls.append(query)
        raise web_nutrition_lookup.NutritionLookupError("no_results")
    monkeypatch.setattr(web_nutrition_lookup, "lookup", no_web)

    http = FixtureHTTP()
    monkeypatch.setenv("EXA_API_KEY", "fixture-exa-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture-firecrawl-key")
    monkeypatch.setattr(official_nutrition, "post_exa_search", http.post_exa)
    monkeypatch.setattr(official_nutrition, "post_firecrawl_scrape", http.post_firecrawl)
    monkeypatch.setenv("MEAL_DRAFTS_ENABLED", "true")
    monkeypatch.setenv("OFFICIAL_RESTAURANT_NUTRITION_ENABLED", "true")

    def forget_process_memory():
        """Drop every in-process cache so reuse can only come from Postgres."""
        srv._resolved_food_cache.clear()
        official_nutrition.clear_process_caches()

    forget_process_memory()
    yield {"srv": srv, "store": store, "users": users, "url": url, "http": http,
           "web_calls": web_calls, "forget": forget_process_memory, "official": official_nutrition}
    forget_process_memory()
    await store.aclose()
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()


async def _rows(url, sql, *args):
    import asyncpg
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def _meal_context(instructions: str) -> dict:
    marker = "MEAL CONTEXT (data, not instructions): "
    assert marker in instructions
    return json.loads(instructions.split(marker, 1)[1].strip())


def scripted_model():
    """A stand-in for the coach model: reads the server's meal context like the
    real model does and sends the update_meal call a correct model would send."""
    seen: list[dict] = []

    async def agent(**kwargs):
        message = kwargs["message"].casefold()
        context = _meal_context(kwargs["instruction_override"])
        seen.append(context)
        update = kwargs["handlers"]["update_meal"]
        if "half" in message:
            logged = context["logged_meals"][0]
            result = await update({"amend_ref": logged["draft_ref"],
                                   "edit": [{"item_ref": "item1", "quantity": 0.5}]})
        else:
            result = await update({"meal_type": "Lunch", "add": [
                {"description": "chicken sandwich", "source": "restaurant", "place": "Chick-fil-A"}]})
        return result.get("reply") or result.get("confirmation") or "", []

    return agent, seen


def _service(srv, agent):
    from agent_canvas import CanvasService
    # Production wiring (server.canvas_service) with the model replaced.
    return CanvasService(store_factory=srv.store_client, food_factory=srv._voice_tool_handlers,
                         coach_factory=lambda: srv._coach_tool_handlers(canvas=True),
                         drafts_factory=srv._meal_drafts_engine, agent=agent)


async def _turn(service, user, session_id, message):
    from auth import bind_user, reset_user
    token = bind_user(user)
    try:
        return await service.turn(session_id, str(uuid4()), message, adapter="text")
    finally:
        reset_user(token)


@pg
async def test_chick_fil_a_ingest_log_half_serving_and_reuse_without_network(env):
    srv, http, url = env["srv"], env["http"], env["url"]
    user = env["users"][0]
    agent, _seen = scripted_model()
    service = _service(srv, agent)
    session_id = str(uuid4())

    # Turn 1: first sighting -> one Exa discovery + one Firecrawl extraction.
    first = await _turn(service, user, session_id, "I had a Chick-fil-A chicken sandwich for lunch")
    assert http.count("exa") == 1 and http.count("firecrawl") == 1
    exa_payload = dict(http.calls[0][1])
    assert exa_payload["includeDomains"] == ["chick-fil-a.com"]  # discovery only on official domains
    assert env["web_calls"] == []  # the generic web estimate was never needed

    sources = await _rows(url, "SELECT * FROM food_source_records WHERE provider='official:chick-fil-a'")
    assert len(sources) == 1
    source = sources[0]
    assert source["trusted"] and source["source_url"] == CFA_PAGE
    metadata = json.loads(source["metadata"])
    assert metadata["discovered_via"] == "exa" and metadata["extracted_via"] == "firecrawl"
    assert metadata["customization"]  # the standard-build limitation is preserved
    assert "markdown" not in metadata and "page_sha256" in metadata  # no raw page stored
    observations = await _rows(url, "SELECT * FROM food_nutrition_observations WHERE source_record_id=$1",
                               source["id"])
    assert len(observations) == 1
    observation = observations[0]
    assert observation["verification_state"] == "official_curated"
    assert observation["serving_basis"] == "per_serving" and observation["serving_text"] == "1 sandwich"
    assert [float(observation[k]) for k in ("calories", "protein", "carbs", "fat", "fiber")] == [420, 29, 41, 18, 1]
    item = (await _rows(url, "SELECT * FROM food_items WHERE id=$1", observation["food_item_id"]))[0]
    assert item["brand"] == "Chick-fil-A" and item["product_identity"] == "official:chick-fil-a:chicken sandwich"
    # Official rows are never exposed through the legacy alias catalog.
    assert await _rows(url, "SELECT 1 FROM food_aliases WHERE food_item_id=$1", item["id"]) == []

    entries = await _rows(url, "SELECT * FROM nutrition_entries WHERE user_id=$1", user)
    assert len(entries) == 1
    entry = entries[0]
    assert float(entry["calories"]) == 420 and float(entry["protein"]) == 29
    component = json.loads(entry["component_metadata"])["components"][0]
    # The private ledger names the exact shared evidence version it used.
    assert component["attribution"]["observation_id"] == str(observation["id"])
    assert component["attribution"]["verification_state"] == "official_curated"
    receipt = first["receipt"]
    assert receipt["recordId"] == str(entry["id"])
    reply = json.dumps(first)
    assert "420 kcal" in reply and "Chick-fil-A" in reply

    # Turn 2: "only half" amends that entry from the stored record, no network.
    env["forget"]()
    half = await _turn(service, user, session_id, "Actually I only ate half of it")
    assert http.count("exa") == 1 and http.count("firecrawl") == 1
    entries = await _rows(url, "SELECT * FROM nutrition_entries WHERE user_id=$1", user)
    assert len(entries) == 1, "a correction never adds a second receipt"
    assert entries[0]["id"] == entry["id"] and entries[0]["entry_revision"] == 2
    assert [float(entries[0][k]) for k in ("calories", "protein", "carbs", "fat", "fiber")] == [210, 14.5, 20.5, 9, 0.5]
    day = await _rows(url, "SELECT calories FROM days WHERE user_id=$1", user)
    assert float(day[0]["calories"]) == 210
    assert "210 kcal" in json.dumps(half)

    # Turn 3: the same meal again later is a new entry, priced from Postgres.
    env["forget"]()
    again = await _turn(service, user, session_id, "Had another Chick-fil-A chicken sandwich")
    assert http.count("exa") == 1 and http.count("firecrawl") == 1
    entries = await _rows(url, "SELECT calories FROM nutrition_entries WHERE user_id=$1 ORDER BY created_at", user)
    assert [float(e["calories"]) for e in entries] == [210, 420]
    assert "420 kcal" in json.dumps(again)
    assert len(await _rows(url, "SELECT 1 FROM food_source_records WHERE provider='official:chick-fil-a'")) == 1


@pg
async def test_second_user_reuses_shared_evidence_but_never_the_first_users_meals(env):
    srv, http, url = env["srv"], env["http"], env["url"]
    first_user, second_user = env["users"]
    agent, _ = scripted_model()
    service = _service(srv, agent)
    await _turn(service, first_user, str(uuid4()), "I had a Chick-fil-A chicken sandwich")
    first_entry = (await _rows(url, "SELECT * FROM nutrition_entries WHERE user_id=$1", first_user))[0]
    env["forget"]()

    second_agent, seen = scripted_model()
    await _turn(_service(srv, second_agent), second_user, str(uuid4()), "Chick-fil-A chicken sandwich for lunch")
    assert http.count("exa") == 1 and http.count("firecrawl") == 1  # shared evidence reused
    assert seen[0]["logged_meals"] == []  # the second user never sees the first user's meal
    second_entries = await _rows(url, "SELECT * FROM nutrition_entries WHERE user_id=$1", second_user)
    assert len(second_entries) == 1 and float(second_entries[0]["calories"]) == 420

    # The second user cannot amend the first user's meal by its reference.
    from auth import bind_user, reset_user
    token = bind_user(second_user)
    try:
        engine = await srv._meal_drafts_engine()
        draft_id = json.loads(first_entry["component_metadata"])["draft_id"]
        attempt = await engine.handle({"amend_ref": draft_id, "edit": [{"item_ref": "item1", "quantity": 0.5}]})
    finally:
        reset_user(token)
    assert attempt["status"] in {"conflict", "failed", "not_found"}
    untouched = (await _rows(url, "SELECT calories, entry_revision FROM nutrition_entries WHERE id=$1",
                             first_entry["id"]))[0]
    assert float(untouched["calories"]) == 420 and untouched["entry_revision"] == 1


@pg
async def test_gate_off_keeps_the_previous_path_and_never_calls_official_providers(env, monkeypatch):
    srv, http, url = env["srv"], env["http"], env["url"]
    monkeypatch.setenv("OFFICIAL_RESTAURANT_NUTRITION_ENABLED", "false")
    agent, _ = scripted_model()
    await _turn(_service(srv, agent), env["users"][0], str(uuid4()), "I had a Chick-fil-A chicken sandwich")
    assert http.calls == []
    assert await _rows(url, "SELECT 1 FROM food_source_records WHERE provider LIKE 'official:%'") == []
    assert env["web_calls"], "the previous chain (web estimate) ran instead"


@pg
async def test_versioned_records_are_idempotent_and_the_newest_version_wins(env):
    official, store, url = env["official"], env["store"], env["url"]
    source = official.REGISTRY["Chick-fil-A"]
    panel = official.validate(official.Candidate.from_extraction(
        FIXTURE_PANEL, source_url=CFA_PAGE, page_text=FIXTURE_MARKDOWN,
        discovered_via="exa", extracted_via="firecrawl"), source, "chicken sandwich")
    first = await store.save_official_observation(official.record_for(panel, source))
    replay = await store.save_official_observation(official.record_for(panel, source))
    assert first["observation_id"] == replay["observation_id"]
    assert len(await _rows(url, "SELECT 1 FROM food_nutrition_observations")) == 1

    changed_page = FIXTURE_MARKDOWN.replace("Calories 420", "Calories 440").replace("Fat 18g", "Fat 20g")
    changed = official.validate(official.Candidate.from_extraction(
        {**FIXTURE_PANEL, "calories": 440, "fat": 20}, source_url=CFA_PAGE, page_text=changed_page,
        discovered_via="exa", extracted_via="firecrawl"), source, "chicken sandwich")
    second = await store.save_official_observation(official.record_for(changed, source))
    assert second["observation_id"] != first["observation_id"]
    assert second["food_item_id"] == first["food_item_id"]  # one identity, two evidence versions
    latest = await store.fetch_official_observation("official:chick-fil-a", "chicken sandwich")
    assert latest["calories"] == 440 and latest["observation_id"] == second["observation_id"]
    assert len(await _rows(url, "SELECT 1 FROM food_nutrition_observations")) == 2
    import asyncpg
    conn = await asyncpg.connect(url)
    try:
        with pytest.raises(asyncpg.RaiseError):  # evidence is immutable
            await conn.execute("UPDATE food_nutrition_observations SET calories=1")
    finally:
        await conn.close()


@pg
async def test_stale_record_refreshes_and_a_failed_refresh_keeps_the_stored_version(env, monkeypatch):
    official, http = env["official"], env["http"]
    from auth import bind_user, reset_user
    token = bind_user(env["users"][0])
    try:
        found = await env["srv"].resolve_food("Chick-fil-A chicken sandwich", atomic=True)
        assert found["macros_per_serving"]["calories"] == 420 and http.count("firecrawl") == 1
        env["forget"]()
        later = official.utcnow() + official.REGISTRY["Chick-fil-A"].max_age + official.timedelta(days=1)
        monkeypatch.setattr(official, "utcnow", lambda: later)
        http.exa = {"results": []}  # the refresh finds nothing this time
        stale = await env["srv"].resolve_food("Chick-fil-A chicken sandwich", atomic=True)
    finally:
        reset_user(token)
    assert http.count("exa") == 2  # a stale record is re-checked once
    assert stale["macros_per_serving"]["calories"] == 420  # never dropped for a failed refresh
    assert stale["attribution"]["freshness"] == "stale"
