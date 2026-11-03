import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
# Test imports must never discover a developer's real credentials. Tests of the
# dotenv parser explicitly reset this flag against their own temporary fixtures.
import config
config._ENV_LOADED = True


@pytest.fixture(autouse=True)
def _deny_real_provider_wire(monkeypatch):
    import httpx
    reached = []
    async def blocked_async(self, request):
        reached.append(request.url.host)
        raise AssertionError("real provider transport forbidden in tests")
    def blocked_sync(self, request):
        reached.append(request.url.host)
        raise AssertionError("real provider transport forbidden in tests")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked_sync)
    yield
    assert not reached, "a test attempted real provider transport"


def pytest_addoption(parser):
    parser.addoption("--food-agent-bench-dir", action="store", default="",
                     help="directory holding the private bench labels.json and scripts.json "
                          "(alternative to FOOD_AGENT_BENCH_DIR)")


@pytest.fixture(autouse=True)
def _isolate_lookup_caches(monkeypatch):
    """Process-local lookup memory (provider search hits, web positive/negative
    results) must never leak between tests that reuse the same food names.

    `config` loads the developer's `server/.env` at import, so a local
    EXA_API_KEY would silently switch Layer 2 to Exa for every test that only
    stubs the OpenAI call -- and make paid network calls. Tests opt in to Exa
    explicitly (test_exa_nutrition_lookup sets the key itself)."""
    import food_lookup
    import web_nutrition_lookup

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("WEB_LOOKUP_PROVIDER", raising=False)
    food_lookup.clear_search_cache()
    web_nutrition_lookup.clear_cache()
    yield
    food_lookup.clear_search_cache()
    web_nutrition_lookup.clear_cache()
