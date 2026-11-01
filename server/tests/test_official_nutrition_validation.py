"""Official restaurant nutrition: validation and provider adapters (no database).

Every provider response here is a FIXTURE built in the test; nothing is a
live search or extraction.
"""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

import official_nutrition as official
from test_official_restaurant_nutrition import CFA_PAGE, FIXTURE_MARKDOWN, FIXTURE_PANEL, fixture_firecrawl

CFA = official.REGISTRY["Chick-fil-A"]


def candidate(**changes):
    panel = {**FIXTURE_PANEL, **changes.pop("panel", {})}
    kwargs = {"source_url": CFA_PAGE, "page_text": FIXTURE_MARKDOWN,
              "discovered_via": "exa", "extracted_via": "firecrawl", **changes}
    return official.Candidate.from_extraction(panel, **kwargs)


def rejection(cand, identity="chicken sandwich"):
    with pytest.raises(official.Rejected) as caught:
        official.validate(cand, CFA, identity)
    return caught.value.reason


def test_identity_ignores_place_words_articles_and_plurals():
    for text in ("Chick-fil-A chicken sandwich", "a chicken sandwich from chick fil a",
                 "CFA chicken sandwiches", "Chick-fil-A® Chicken Sandwich"):
        assert official.identity_of(text, CFA) == "chicken sandwich"
    assert official.identity_of("Chick-fil-A spicy chicken sandwich", CFA) == "spicy chicken sandwich"


def test_valid_official_panel_is_accepted_with_a_page_hash_not_the_page():
    panel = official.validate(candidate(), CFA, "chicken sandwich")
    assert panel.nutrients == {"calories": 420, "protein": 29, "carbs": 41, "fat": 18, "fiber": 1}
    record = official.record_for(panel, CFA)
    assert record["display_name"] == "Chick-fil-A Chicken Sandwich"
    assert "markdown" not in str(record["metadata"]) and FIXTURE_MARKDOWN not in str(record["metadata"])
    assert record["metadata"]["page_sha256"] == panel.page_sha256


@pytest.mark.parametrize("url", ["https://www.chick-fil-a.com.evil.example/menu/x",
                                 "http://www.chick-fil-a.com/menu/x",
                                 "https://www.nutritionix.com/chick-fil-a/sandwich"])
def test_a_page_off_the_official_domain_is_never_evidence(url):
    assert rejection(candidate(source_url=url)) == "not_official_source"


def test_a_different_menu_item_is_not_this_item():
    spicy = candidate(panel={"item_name": "Spicy Chicken Sandwich"})
    assert rejection(spicy) == "identity_mismatch"


def test_a_missing_nutrient_is_rejected_never_stored_as_zero():
    assert rejection(candidate(panel={"protein": None})) == "incomplete_panel"
    assert rejection(candidate(panel={"fiber": "n/a"})) == "incomplete_panel"


def test_numbers_that_cannot_describe_one_food_are_rejected():
    assert rejection(candidate(panel={"calories": 90})) == "energy_inconsistent"


def test_values_the_page_does_not_show_are_rejected():
    page = FIXTURE_MARKDOWN.replace("Protein 29g", "Protein 31g")
    assert rejection(candidate(page_text=page)) == "values_not_in_page"
    assert rejection(candidate(page_text="")) == "item_not_in_page"


def test_an_undefined_serving_is_rejected():
    assert rejection(candidate(panel={"serving_size": ""})) == "serving_undefined"


@pytest.mark.asyncio
async def test_missing_provider_keys_are_a_stated_blocker_not_a_result(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(official.ProviderUnavailable) as caught:
        await official.discover(CFA, "chicken sandwich")
    assert caught.value.reason == "exa_api_key_not_configured"
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(official.ProviderUnavailable) as caught:
        await official.extract(CFA, CFA_PAGE, "chicken sandwich", discovered_via="exa")
    assert caught.value.reason == "firecrawl_api_key_not_configured"


@pytest.mark.asyncio
async def test_discovery_keeps_only_official_urls(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "fixture-key")
    sent = {}

    async def post(key, payload):
        sent.update(payload)
        return httpx.Response(200, json={"results": [
            {"url": "https://www.fastfoodnutrition.org/chick-fil-a/chicken-sandwich"},
            {"url": CFA_PAGE}, {"url": CFA_PAGE}, {"url": "javascript:alert(1)"}]})
    monkeypatch.setattr(official, "post_exa_search", post)
    assert await official.discover(CFA, "chicken sandwich") == [CFA_PAGE]
    assert sent["includeDomains"] == ["chick-fil-a.com"] and sent["numResults"] == 3


@pytest.mark.asyncio
async def test_firecrawl_request_shape_and_errors(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture-key")
    sent = {}
    status = {"code": 200, "body": fixture_firecrawl()}

    async def post(key, payload):
        sent.update(payload)
        return httpx.Response(status["code"], json=status["body"])
    monkeypatch.setattr(official, "post_firecrawl_scrape", post)
    found = await official.extract(CFA, CFA_PAGE, "chicken sandwich", discovered_via="exa")
    assert found.nutrients["calories"] == 420 and found.page_text == FIXTURE_MARKDOWN
    assert sent["url"] == CFA_PAGE and sent["formats"][0] == "markdown"
    assert sent["formats"][1]["type"] == "json" and "schema" in sent["formats"][1]
    # A redirect off the official site is caught by validation, not trusted.
    status["body"] = fixture_firecrawl(final_url="https://example.com/landing")
    moved = await official.extract(CFA, CFA_PAGE, "chicken sandwich", discovered_via="exa")
    with pytest.raises(official.Rejected):
        official.validate(moved, CFA, "chicken sandwich")
    for code in (402, 429, 500):
        status.update(code=code, body={"success": False, "error": "fixture"})
        with pytest.raises(official.ProviderUnavailable) as caught:
            await official.extract(CFA, CFA_PAGE, "chicken sandwich", discovered_via="exa")
        assert caught.value.reason == f"firecrawl_http_{code}"


class MemoryOfficialStore:
    def __init__(self):
        self.saved = []

    async def fetch_official_observation(self, provider, identity):
        if not self.saved:
            return None
        record, ids = self.saved[-1]
        return {**ids, "display_name": record["display_name"], "product_identity": record["product_identity"],
                "serving_text": record["serving_text"], "serving_grams": record["serving_grams"],
                **record["nutrients"], "source_url": record["source_url"],
                "retrieved_at": record["retrieved_at"], "cache_allowed": record["cache_allowed"]}

    async def save_official_observation(self, record):
        ids = {"food_item_id": "item", "observation_id": f"obs{len(self.saved)}", "source_record_id": "src"}
        self.saved.append((record, ids))
        return ids


@pytest.mark.asyncio
async def test_an_outage_is_remembered_so_each_meal_does_not_pay_again(monkeypatch):
    official.clear_process_caches()
    calls = []

    async def failing_discover(source, identity):
        calls.append(identity)
        raise official.ProviderUnavailable("exa_http_503")
    monkeypatch.setattr(official, "discover", failing_discover)
    store = MemoryOfficialStore()
    assert await official.lookup("Chick-fil-A chicken sandwich", store=store) is None
    assert await official.lookup("chick fil a chicken sandwich", store=store) is None
    assert calls == ["chicken sandwich"]
    official.clear_process_caches()


@pytest.mark.asyncio
async def test_unregistered_places_and_counts_never_reach_a_provider(monkeypatch):
    official.clear_process_caches()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("no provider call expected")
    monkeypatch.setattr(official, "discover", forbidden)
    store = MemoryOfficialStore()
    assert await official.lookup("Taco Bell crunchy taco", store=store) is None  # not in the registry
    assert await official.lookup("chicken sandwich", store=store) is None  # no place at all
    assert await official.lookup("Chick-fil-A 8 count nuggets", store=store) is None  # count-led identity


def test_gate_fails_closed(monkeypatch):
    import release_gates
    from auth import bind_user, reset_user
    user = uuid4()
    scope = bind_user(user)
    try:
        monkeypatch.delenv("OFFICIAL_RESTAURANT_NUTRITION_ENABLED", raising=False)
        assert not release_gates.official_restaurant_nutrition_allowed()
        monkeypatch.setenv("OFFICIAL_RESTAURANT_NUTRITION_ENABLED", "yes")
        assert not release_gates.official_restaurant_nutrition_allowed()
        monkeypatch.setenv("OFFICIAL_RESTAURANT_NUTRITION_ENABLED", "true")
        assert release_gates.official_restaurant_nutrition_allowed()
        monkeypatch.setenv("OFFICIAL_RESTAURANT_NUTRITION_USER_IDS", "not-a-uuid")
        assert not release_gates.official_restaurant_nutrition_allowed()
        monkeypatch.setenv("OFFICIAL_RESTAURANT_NUTRITION_USER_IDS", str(uuid4()))
        assert not release_gates.official_restaurant_nutrition_allowed()
        monkeypatch.setenv("OFFICIAL_RESTAURANT_NUTRITION_USER_IDS", str(user))
        assert release_gates.official_restaurant_nutrition_allowed()
    finally:
        reset_user(scope)
