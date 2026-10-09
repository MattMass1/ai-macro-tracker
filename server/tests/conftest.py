import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture(autouse=True)
def _isolate_lookup_caches():
    """Process-local lookup memory (provider search hits, web positive/negative
    results) must never leak between tests that reuse the same food names."""
    import food_lookup
    import web_nutrition_lookup

    food_lookup.clear_search_cache()
    web_nutrition_lookup.clear_cache()
    yield
    food_lookup.clear_search_cache()
    web_nutrition_lookup.clear_cache()
