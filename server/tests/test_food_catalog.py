"""Focused catalog, provider, and migration reliability tests."""
from __future__ import annotations

from pathlib import Path
import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import httpx

import food_lookup
import migrations
from auth import bind_user, reset_user
from food_catalog import evidence_hash, normalize_food_name, provenance_state
from store import (IdempotencyConflict, MAX_COMPONENT_METADATA_BYTES, Store,
                   _component_metadata_json)


def test_food_name_normalization_is_deterministic_and_unicode_aware():
    assert normalize_food_name("  CRÈME—Brûlée!! ") == "crème brûlée"
    assert normalize_food_name("Chicken   Breast") == "chicken breast"


def test_logged_provenance_never_promotes_generic_community_sources():
    assert provenance_state("family recipe") == ("community_observed", False)
    assert provenance_state("ESTIMATE — typical serving") == ("estimate", False)
    assert provenance_state("OpenFoodFacts barcode: 123") == ("exact_identifier", True)
    assert provenance_state("Known food: Banana") == ("internal_curated", True)


def test_evidence_hash_is_order_independent_and_sensitive_to_values():
    assert evidence_hash({"b": 2, "a": 1}) == evidence_hash({"a": 1, "b": 2})
    assert evidence_hash({"a": 1}) != evidence_hash({"a": 2})


async def test_fatsecret_normalizes_serving_and_attribution(monkeypatch):
    responses = iter([
        {"foods": {"food": {"food_id": "42", "food_name": "Greek Yogurt"}}},
        {"food": {"food_name": "Greek Yogurt", "servings": {"serving": {
            "serving_description": "1 tub", "metric_serving_amount": "170",
            "metric_serving_unit": "g", "calories": "100", "protein": "17",
            "carbohydrate": "6", "fat": "0", "fiber": "0",
        }}}},
    ])

    async def fake_request(_data):
        return next(responses)

    monkeypatch.setattr(food_lookup, "_fatsecret_request", fake_request)
    monkeypatch.setenv("FATSECRET_CACHE_ALLOWED", "false")
    result = await food_lookup.search_fatsecret("greek yogurt")
    assert result["source"] == "FatSecret: 42"
    assert result["macros_per_serving"]["protein"] == 17.0
    assert result["macros_per_100g"]["calories"] == 58.82
    assert result["attribution"]["provider"] == "FatSecret"
    assert result["attribution"]["external_id"] == "42"
    assert result["attribution"]["cache_allowed"] is False
    assert result["attribution"]["serving_grams"] == 170
    assert result["attribution"]["evidence_hash"]
    assert result["attribution"]["attribution_text"] == "Nutrition data from FatSecret"


def test_fatsecret_relevance_rejects_broad_and_single_token_substrings():
    assert food_lookup._fatsecret_relevant("Greek Yogurt", {"food_name": "Greek Yogurt"})
    assert food_lookup._fatsecret_relevant(
        "Fage Greek Yogurt", {"brand_name": "Fage", "food_name": "Greek Yogurt"})
    assert not food_lookup._fatsecret_relevant(
        "chicken", {"food_name": "Chicken Broth Concentrate"})
    assert not food_lookup._fatsecret_relevant(
        "protein bar", {"food_name": "Chocolate Protein Bar"})


async def test_fatsecret_requires_credentials_and_explicit_attribution_gate(monkeypatch):
    calls = []
    class Client:
        async def __aenter__(self): calls.append("opened"); return self
        async def __aexit__(self, *_args): return None
    monkeypatch.setattr(food_lookup, "_client", Client)
    monkeypatch.setenv("FATSECRET_CLIENT_ID", "id")
    monkeypatch.setenv("FATSECRET_CLIENT_SECRET", "secret")
    monkeypatch.delenv("FATSECRET_ATTRIBUTION_ENABLED", raising=False)
    assert await food_lookup._fatsecret_request({"method": "foods.search"}) is None
    assert calls == []


def test_fatsecret_oauth1_signing_is_deterministic_and_rfc3986_encoded():
    form = food_lookup._fatsecret_oauth1_data(
        {"method": "foods.search", "search_expression": "fish & chips", "max_results": 5},
        "consumer key", "secret/value", nonce="fixed-nonce", timestamp=1700000000,
    )
    assert form["oauth_consumer_key"] == "consumer key"
    assert form["oauth_nonce"] == "fixed-nonce"
    assert form["oauth_signature_method"] == "HMAC-SHA1"
    assert form["oauth_timestamp"] == "1700000000"
    assert form["oauth_version"] == "1.0"
    assert form["oauth_signature"] == "wGObnUqO+b3lEmVrhkD5s5uqEIg="


@pytest.mark.parametrize(
    ("consumer_key", "consumer_secret", "client_id", "client_secret", "expected"),
    [
        ("key", "secret", "id", "client-secret", "oauth1"),
        ("key", "secret", "", "", "oauth1"),
        ("", "", "id", "client-secret", "oauth2"),
        ("key", "", "", "", None),
        ("", "secret", "", "", None),
        ("", "", "id", "", None),
        ("", "", "", "client-secret", None),
    ],
)
def test_fatsecret_provider_selection_rejects_partial_pairs(
    monkeypatch, consumer_key, consumer_secret, client_id, client_secret, expected,
):
    monkeypatch.setenv("FATSECRET_ATTRIBUTION_ENABLED", "true")
    monkeypatch.setenv("FATSECRET_CONSUMER_KEY", consumer_key)
    monkeypatch.setenv("FATSECRET_CONSUMER_SECRET", consumer_secret)
    monkeypatch.setenv("FATSECRET_CLIENT_ID", client_id)
    monkeypatch.setenv("FATSECRET_CLIENT_SECRET", client_secret)
    assert food_lookup._fatsecret_provider_mode() == expected


async def test_fatsecret_oauth1_request_is_preferred_without_token_call(monkeypatch):
    calls = []

    class Response:
        def json(self): return {"ok": True}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None

    async def request(_client, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    async def unexpected_token(*_args):
        pytest.fail("OAuth2 token endpoint must not run when OAuth1 is configured")

    monkeypatch.setenv("FATSECRET_ATTRIBUTION_ENABLED", "true")
    monkeypatch.setenv("FATSECRET_CONSUMER_KEY", "consumer")
    monkeypatch.setenv("FATSECRET_CONSUMER_SECRET", "consumer-secret")
    monkeypatch.setenv("FATSECRET_CLIENT_ID", "premier")
    monkeypatch.setenv("FATSECRET_CLIENT_SECRET", "premier-secret")
    monkeypatch.setattr(food_lookup, "_client", Client)
    monkeypatch.setattr(food_lookup, "_request_with_backoff", request)
    monkeypatch.setattr(food_lookup, "_fatsecret_token", unexpected_token)

    assert await food_lookup._fatsecret_request({"method": "foods.search"}) == {"ok": True}
    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", food_lookup.FATSECRET_API_URL)
    form = kwargs["request_kwargs_factory"]()["data"]
    assert form["oauth_consumer_key"] == "consumer"
    assert "Authorization" not in kwargs.get("headers", {})


async def test_fatsecret_oauth1_retry_regenerates_nonce_and_signature(monkeypatch):
    attempts = []
    nonces = iter(("nonce-one", "nonce-two"))

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "provider error", request=httpx.Request("POST", food_lookup.FATSECRET_API_URL),
                    response=httpx.Response(self.status_code),
                )

        def json(self): return {"ok": True}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def request(self, _method, _url, **kwargs):
            attempts.append(kwargs["data"])
            return Response(500 if len(attempts) == 1 else 200)

    async def no_sleep(_delay): return None

    monkeypatch.setenv("FATSECRET_ATTRIBUTION_ENABLED", "true")
    monkeypatch.setenv("FATSECRET_CONSUMER_KEY", "consumer")
    monkeypatch.setenv("FATSECRET_CONSUMER_SECRET", "consumer-secret")
    monkeypatch.setattr(food_lookup, "_client", Client)
    monkeypatch.setattr(food_lookup.secrets, "token_hex", lambda _size: next(nonces))
    monkeypatch.setattr(food_lookup.asyncio, "sleep", no_sleep)

    assert await food_lookup._fatsecret_request({"method": "foods.search"}) == {"ok": True}
    assert [form["oauth_nonce"] for form in attempts] == ["nonce-one", "nonce-two"]
    assert attempts[0]["oauth_signature"] != attempts[1]["oauth_signature"]


async def test_fatsecret_oauth1_non_retryable_4xx_makes_one_attempt(monkeypatch):
    attempts = []

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def request(self, method, url, **kwargs):
            attempts.append(kwargs["data"])
            return httpx.Response(400, request=httpx.Request(method, url))

    monkeypatch.setenv("FATSECRET_ATTRIBUTION_ENABLED", "true")
    monkeypatch.setenv("FATSECRET_CONSUMER_KEY", "consumer")
    monkeypatch.setenv("FATSECRET_CONSUMER_SECRET", "consumer-secret")
    monkeypatch.setattr(food_lookup, "_client", Client)

    assert await food_lookup._fatsecret_request({"method": "foods.search"}) is None
    assert len(attempts) == 1


async def test_fatsecret_oauth2_fallback_uses_bearer_token(monkeypatch):
    calls = []

    class Response:
        def json(self): return {"ok": True}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None

    async def token(_client, client_id, secret):
        assert (client_id, secret) == ("premier", "premier-secret")
        return "test-token"

    async def request(_client, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setenv("FATSECRET_ATTRIBUTION_ENABLED", "true")
    monkeypatch.delenv("FATSECRET_CONSUMER_KEY", raising=False)
    monkeypatch.delenv("FATSECRET_CONSUMER_SECRET", raising=False)
    monkeypatch.setenv("FATSECRET_CLIENT_ID", "premier")
    monkeypatch.setenv("FATSECRET_CLIENT_SECRET", "premier-secret")
    monkeypatch.setattr(food_lookup, "_client", Client)
    monkeypatch.setattr(food_lookup, "_fatsecret_token", token)
    monkeypatch.setattr(food_lookup, "_request_with_backoff", request)

    assert await food_lookup._fatsecret_request({"method": "foods.search"}) == {"ok": True}
    assert calls[0][2]["headers"] == {"Authorization": "Bearer test-token"}


def test_schema_and_catalog_queries_accept_internal_curated_without_calling_it_official():
    migration = (Path(__file__).parents[1] / "migrations" / "001_food_catalog.sql").read_text()
    store_source = (Path(__file__).parents[1] / "src" / "store.py").read_text()
    assert "'internal_curated'" in migration
    assert "'official_curated','internal_curated','exact_identifier'" in store_source


def test_fatsecret_attribution_gate_defaults_false_in_deployment_examples():
    server = Path(__file__).parents[1]
    assert "FATSECRET_ATTRIBUTION_ENABLED=false" in (server / ".env.example").read_text()
    render = (server / "render.yaml").read_text()
    assert "key: FATSECRET_ATTRIBUTION_ENABLED\n        value: \"false\"" in render
    assert "FATSECRET_CONSUMER_KEY=" in (server / ".env.example").read_text()
    assert "key: FATSECRET_CONSUMER_KEY\n        sync: false" in render


async def test_catalog_is_first_and_provider_errors_are_isolated(monkeypatch):
    calls = []

    async def catalog(_query):
        calls.append("catalog")
        return {"name": "Oats", "source": "curated", "macros_per_100g": {
            "calories": 1, "protein": 1, "carbs": 1, "fat": 1, "fiber": 1,
        }}

    async def unexpected(_query):
        pytest.fail("external provider must not run on a catalog hit")

    monkeypatch.setattr(food_lookup, "search_openfoodfacts", unexpected)
    result = await food_lookup.resolve_food("oats", catalog_lookup=catalog)
    assert result["name"] == "Oats"
    assert calls == ["catalog"]


async def test_migration_status_reports_pending_and_checksum_drift(monkeypatch):
    files = [("000.sql", Path("/tmp/000.sql")), ("001.sql", Path("/tmp/001.sql"))]
    monkeypatch.setattr(migrations, "migration_files", lambda: files)
    monkeypatch.setattr(migrations, "checksum", lambda path: "a" if path.name == "000.sql" else "b")

    class Conn:
        async def fetchval(self, _sql):
            return True
        async def fetch(self, _sql):
            return [{"filename": "000.sql", "checksum": "changed"}]

    status = await migrations.migration_status(Conn())
    assert status == {
        "compatible": False, "pending": ["001.sql"],
        "drift": ["000.sql"], "unknown": [],
    }


def test_migration_stream_uses_only_immutable_migration_directory():
    files = migrations.migration_files()
    assert files[0][0] == "000_legacy_schema.sql"
    assert all(path.parent == migrations.MIGRATIONS_DIR for _, path in files)
    assert all(path.name != "schema.sql" for _, path in files)


def test_migration_cli_fails_nonzero_for_missing_or_incompatible_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as missing:
        migrations.main([])
    assert missing.value.code != 0
    with pytest.raises(migrations.MigrationError):
        migrations.main(["--database-url", "https://user:secret@example.com/db"])
    assert "secret" not in capsys.readouterr().out


def test_migration_preflight_prints_only_database_and_host_class(monkeypatch, capsys):
    async def applied(_url): return []
    monkeypatch.setattr(migrations, "apply_migrations", applied)
    assert migrations.main(["--database-url", "postgresql://alice:supersecret@db.internal/macro_clone"]) == 0
    output = capsys.readouterr().out
    assert "database=macro_clone host_class=private_dns" in output
    assert "alice" not in output and "supersecret" not in output and "db.internal" not in output


class AtomicConnection:
    def __init__(self):
        self.operations = {}
        self.business_writes = 0
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            snapshot = (dict(self.operations), self.business_writes)
            try:
                yield
            except Exception:
                self.operations, self.business_writes = snapshot
                raise

    async def fetchrow(self, sql, user_id, key, request_hash=None):
        identity = (user_id, key)
        if sql.startswith("INSERT"):
            if identity in self.operations:
                return None
            self.operations[identity] = {"request_hash": request_hash, "status": "in_progress", "stored_response": None}
            return {"id": "claimed"}
        return self.operations[identity]

    async def execute(self, sql, response, user_id, key):
        self.operations[(user_id, key)].update(status="completed", stored_response=json.loads(response))


class AtomicPool:
    def __init__(self, conn): self.conn = conn
    def acquire(self):
        conn = self.conn
        class Context:
            async def __aenter__(self): return conn
            async def __aexit__(self, *_): return None
        return Context()


async def test_atomic_idempotency_concurrent_replay_conflict_and_failure_rollback():
    conn = AtomicConnection(); store = Store("postgresql://unused")
    async def connect(): return AtomicPool(conn)
    store.connect = connect
    token = bind_user(uuid4())
    try:
        async def write(_conn):
            _conn.business_writes += 1
            await asyncio.sleep(0)
            return {"meal_id":"one"}
        first, second = await asyncio.gather(
            store.run_idempotent("key", "hash", write), store.run_idempotent("key", "hash", write))
        assert first == second == {"meal_id":"one"}
        assert conn.business_writes == 1
        with pytest.raises(IdempotencyConflict):
            await store.run_idempotent("key", "different", write)
        async def fail(_conn):
            _conn.business_writes += 1
            raise RuntimeError("write failed")
        with pytest.raises(RuntimeError):
            await store.run_idempotent("failed-key", "hash", fail)
        assert not any(key[1] == "failed-key" for key in conn.operations)
        assert conn.business_writes == 1
    finally:
        reset_user(token)


async def test_idempotent_meal_wrapper_uses_claim_insert_rollup_and_response_in_one_transaction():
    conn = AtomicConnection(); store = Store("postgresql://unused")
    async def connect(): return AtomicPool(conn)
    store.connect = connect
    async def insert(_conn, **values):
        _conn.business_writes += 1
        return {"id":"meal-one", "day":values["day"]}
    async def refresh(_conn, _user_id, _day):
        _conn.business_writes += 1
    store._insert_meal_conn = insert
    store._refresh_rollups = refresh
    async def response_builder(_conn, _user_id, row):
        return {"logged":{"id":row["id"]}}
    token = bind_user(uuid4())
    try:
        values = {"name":"Banana","meal":"Snack","calories":105,"protein":1,
            "carbs":27,"fat":0,"fiber":3,"day":"2026-09-01",
            "macro_source":"Known food: Banana"}
        first, replay = await asyncio.gather(*[
            store.insert_meal_idempotent("meal-key", "same-hash",
                response_builder=response_builder, **values) for _ in range(2)])
        assert first == replay == {"logged":{"id":"meal-one"}}
        assert conn.business_writes == 2
    finally:
        reset_user(token)


async def test_new_log_reuses_matching_trusted_observation():
    class Conn:
        async def fetchrow(self, sql, *args):
            if "FROM food_aliases" in sql:
                return {"item_id":"trusted-item", "observation_id":"trusted-observation"}
            return None
        async def fetchval(self, *_args):
            pytest.fail("community snapshot must not be created for a trusted match")
    linked = await Store._catalog_snapshot(Conn(), name="Banana", macro_source="Known food: Banana",
        macros={"calories":105,"protein":1,"carbs":27,"fat":0,"fiber":0})
    assert linked == ("trusted-item", "trusted-observation")


async def test_provider_id_relink_requires_all_macros_and_available_basis():
    class Conn:
        def __init__(self): self.created = False
        async def fetchrow(self, sql, *args):
            if "FROM food_identifiers" in sql:
                return {"item_id":"provider-item", "observation_id":"provider-observation",
                    "serving_basis":"per_100g", "calories":89, "protein":1.1,
                    "carbs":23, "fat":0.3, "fiber":2.6}
            return None
        async def fetchval(self, sql, *args):
            self.created = True
            if "food_items" in sql: return "community-item"
            if "food_source_records" in sql: return "community-source"
            return "community-observation"
        async def execute(self, *_args): return "INSERT 0 1"
    macros = {"calories":90,"protein":1.1,"carbs":23,"fat":0.3,"fiber":2.6}
    conn = Conn()
    linked = await Store._catalog_snapshot(conn, name="Banana", macros=macros,
        macro_source="OpenFoodFacts barcode: 4011", serving_basis="per_100g")
    assert linked == ("community-item", "community-observation")
    assert conn.created

    conn = Conn()
    macros["calories"] = 89
    linked = await Store._catalog_snapshot(conn, name="Banana", macros=macros,
        macro_source="OpenFoodFacts barcode: 4011", serving_basis="per_serving")
    assert linked == ("community-item", "community-observation")


def test_component_metadata_has_strict_utf8_serialized_size_cap():
    assert _component_metadata_json({"name": "x"})
    with pytest.raises(ValueError, match="serialized bytes"):
        _component_metadata_json({"name": "é" * MAX_COMPONENT_METADATA_BYTES})


async def test_component_metadata_cap_rejects_before_database_connection():
    store = Store("postgresql://unused")
    async def unexpected_connect():
        pytest.fail("oversized metadata must be rejected before a DB write")
    store.connect = unexpected_connect
    with pytest.raises(ValueError, match="serialized bytes"):
        await store.insert_meal(name="Composite", meal="Lunch", calories=100,
            protein=10, carbs=10, fat=2, fiber=1, day="2026-09-01",
            macro_source="Composite: labels",
            component_metadata={"name":"é" * MAX_COMPONENT_METADATA_BYTES})
