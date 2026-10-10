"""Redundancy removal from the 2026-10-10 agent scan: per-request auth write,
poll-time setup re-sync, duplicate local lookups, per-request HTTP clients,
unbounded web caches, and a ranking gate that cost wasted item fetches."""
import asyncio
import json
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request

import food_lookup
import http_clients
import store as store_module
import web_nutrition_lookup as web
from auth import bind_user, reset_user
from store import Store


class FakePool:
    def __init__(self, user_id):
        self.user_id, self.fetchval_calls, self.execute_calls = user_id, 0, 0

    async def fetchval(self, _sql, _token_hash):
        self.fetchval_calls += 1
        return self.user_id

    async def execute(self, _sql, _token_hash):
        self.execute_calls += 1


async def test_device_resolution_is_cached_and_last_seen_written_sparingly(monkeypatch):
    user_id = uuid4()
    pool = FakePool(user_id)
    s = Store("postgresql://fixture.invalid/x")
    monkeypatch.setattr(s, "connect", lambda: asyncio.sleep(0, result=pool))
    clock = {"now": 1000.0}
    monkeypatch.setattr(store_module._clock, "monotonic", lambda: clock["now"])
    for _ in range(30):  # one minute of 2 s polls (t = 1000 .. 1058)
        assert await s.resolve_device("token-a") == user_id
        clock["now"] += 2
    assert pool.fetchval_calls == 2   # one UPDATE...RETURNING per 30 s TTL window, not 30
    assert pool.execute_calls == 0
    clock["now"] += 400
    assert await s.resolve_device("token-a") == user_id
    assert pool.fetchval_calls == 3  # TTL expired: fresh resolve (which also writes last_seen)
    # A token that stops resolving is forgotten at once and never served from cache.
    pool.user_id = None
    clock["now"] += 100
    assert await s.resolve_device("token-a") is None
    clock["now"] += 1
    assert await s.resolve_device("token-a") is None
    assert pool.fetchval_calls == 5


async def test_snapshot_poll_does_not_reread_a_completed_setup(monkeypatch):
    from agent_canvas import CanvasService
    from test_agent_canvas import MemoryStore

    class AccountStore(MemoryStore):
        reads = 0

        async def get_display_name(self):
            type(self).reads += 1; return "Matt"

        async def get_metrics(self):
            type(self).reads += 1; return {"weight_kg": 90}

        async def fetch_targets(self, _day):
            type(self).reads += 1; return {"calories": 2400}

        async def fetch_workout_plan(self):
            type(self).reads += 1; return {"rotation": ["Push"], "days": {"Push": {"exercises": []}}}

    service = CanvasService(store_factory=lambda: AccountStore(), food_factory=lambda: {},
                            coach_factory=lambda: {}, agent=None)
    scope = bind_user(uuid4())
    try:
        session_id = str(uuid4())
        first = await service.snapshot(session_id, create=True)
        assert AccountStore.reads == 4  # first look syncs once (four reads, gathered)
        for _ in range(10):
            again = await service.snapshot(session_id)
            assert again["revision"] == first["revision"]
        assert AccountStore.reads == 4  # ten polls, no account reads
    finally:
        reset_user(scope)


async def test_snapshot_route_returns_304_for_an_unchanged_revision(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    monkeypatch.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import server as srv

    class FixedService:
        async def snapshot(self, _session_id, *, create=False):
            return {"instanceId": "inst-1", "revision": 7, "surfaces": []}

    monkeypatch.setattr(srv, "canvas_service", lambda: FixedService())

    def request(headers):
        scope = {"type": "http", "http_version": "1.1", "method": "GET",
                 "path": "/api/agent-canvas/s1", "scheme": "http", "server": ("test", 80),
                 "client": ("127.0.0.1", 1), "query_string": b"",
                 "path_params": {"session_id": "s1"},
                 "headers": [(b"x-app-token", srv.CONFIG.app_shared_token.encode()),
                             *[(k.encode(), v.encode()) for k, v in headers.items()]]}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        return Request(scope, receive)

    full = await srv.api_canvas_snapshot(request({}))
    assert full.status_code == 200 and full.headers["etag"] == '"inst-1:7"'
    assert json.loads(full.body)["revision"] == 7
    unchanged = await srv.api_canvas_snapshot(request({"if-none-match": '"inst-1:7"'}))
    assert unchanged.status_code == 304 and unchanged.headers["etag"] == '"inst-1:7"'
    assert not unchanged.body
    # Seen in production: Render's proxy weakens the validator to W/"...".
    weak = await srv.api_canvas_snapshot(request({"if-none-match": 'W/"inst-1:7"'}))
    assert weak.status_code == 304
    stale = await srv.api_canvas_snapshot(request({"if-none-match": '"inst-1:6"'}))
    assert stale.status_code == 200


async def test_outbound_http_clients_are_shared_per_loop_and_timeout():
    a, b, c = http_clients.shared_client(90.0), http_clients.shared_client(90.0), http_clients.shared_client(8.0)
    assert a is b and a is not c and not a.is_closed


async def test_one_food_tool_call_queries_the_catalog_once_per_distinct_text(monkeypatch):
    from test_live_coach import FakeVoiceStore, _import_server
    srv = _import_server(monkeypatch)

    class CountingStore(FakeVoiceStore):
        catalog_calls: list[str] = []

        async def lookup_catalog(self, query):
            type(self).catalog_calls.append(food_lookup.normalize_food_name(query)); return None

    fake = CountingStore()
    monkeypatch.setattr(srv, "_client", fake)
    srv._resolved_food_cache.clear()
    monkeypatch.setattr(srv.food_lookup, "_fatsecret_provider_mode", lambda: None)
    token = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("memo", {
            "description": "2 eggs and 2 slices of bacon", "meal_type": "Breakfast"})
    finally:
        reset_user(token)
    assert result["status"] == "committed"
    calls = CountingStore.catalog_calls
    assert calls and len(calls) == len(set(calls)), calls  # no text asked twice


def test_web_caches_are_bounded():
    web.clear_cache()
    for index in range(web.CACHE_MAX_ENTRIES + 50):
        web._bounded_put(web._NEGATIVE_CACHE, f"k{index}", (0.0, "no_results"))
    assert len(web._NEGATIVE_CACHE) == web.CACHE_MAX_ENTRIES
    assert "k0" not in web._NEGATIVE_CACHE and f"k{web.CACHE_MAX_ENTRIES + 49}" in web._NEGATIVE_CACHE


def test_ranking_gate_ignores_description_words_the_resolver_will_not_accept():
    # The hit's description mentions the query words, its name does not: fetching
    # it would be wasted because validated() re-checks name + brand only.
    hit = {"food_id": "1", "food_name": "Chicken Broth Concentrate",
           "food_description": "chicken sandwich flavor base"}
    assert food_lookup._rank_hits("chicken sandwich", [hit]) == []


async def test_did_you_mean_reuses_the_search_keyed_on_the_cleaned_identity(monkeypatch):
    monkeypatch.setattr(food_lookup, "_fatsecret_provider_mode", lambda: "oauth1")
    searches = []

    async def request(data):
        searches.append(data["search_expression"])
        return {"foods": {"food": [
            {"food_id": "1", "food_name": "Chicken Breast Grilled", "brand_name": "Fixture Farms"},
            {"food_id": "2", "food_name": "Chicken Breast Fried", "brand_name": "Fixture Farms"},
        ]}}

    monkeypatch.setattr(food_lookup, "_fatsecret_request", request)
    assert await food_lookup.resolve_food("about 12 oz fixture farms chicken breast") is None
    assert searches[0] == "fixture farms chicken breast"  # the hedge and the portion were stripped
    searched = list(searches)
    options = await food_lookup.fatsecret_name_options("about 12 oz fixture farms chicken breast")
    assert len(options) == 2
    assert searches == searched  # the options reused the resolver's search; no new provider call


def test_resolved_food_cache_forgets_a_tenant_when_a_preset_is_saved(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    monkeypatch.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import server as srv
    srv._resolved_food_cache.clear()
    srv._resolved_food_cache[("t1", "oats", False, False, True, False)] = (0.0, {"name": "oats"})
    srv._resolved_food_cache[("t2", "oats", False, False, True, False)] = (0.0, {"name": "oats"})
    srv._forget_resolved_food("t1")
    assert list(srv._resolved_food_cache) == [("t2", "oats", False, False, True, False)]
