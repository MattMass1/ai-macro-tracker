"""Official restaurant nutrition: validation and provider adapters (no database).

Every provider response here is a FIXTURE built in the test; nothing is a
live search or extraction.
"""
from __future__ import annotations

import asyncio
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
    assert rejection(candidate(page_text="")) == "not_item_page"


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


# ---------------------------------------------------------------------------
# Review round 1 (B1, B2, S2-S5, nits). Pages are FIXTURES written here.
# ---------------------------------------------------------------------------

TABLE_PAGE = (
    "# Nutrition & Allergens\n"
    "| Item | Calories | Fat | Carbohydrates | Fiber | Protein |\n"
    "| Chick-fil-A® Chicken Sandwich | 420 | 18g | 41g | 1g | 29g |\n"
    "| Spicy Chicken Sandwich | 450 | 19g | 45g | 2g | 28g |\n"
)


def test_a_multi_item_page_is_never_evidence_for_one_item():
    # B1: the Spicy row's numbers under the plain sandwich's name.
    spicy_numbers = candidate(page_text=TABLE_PAGE, panel={"calories": 450, "fat": 19, "carbs": 45,
                                                         "fiber": 2, "protein": 28})
    assert rejection(spicy_numbers) == "not_item_page"
    assert rejection(candidate(page_text=TABLE_PAGE)) == "not_item_page"


def test_values_are_read_from_the_items_own_labels_not_anywhere_on_the_page():
    # A related-items block lower on the item page must not supply the numbers.
    page = FIXTURE_MARKDOWN + "\n# You may also like\nSpicy Chicken Sandwich\nCalories 450\nProtein 28g\n"
    accepted = official.validate(candidate(page_text=page), CFA, "chicken sandwich")
    assert accepted.nutrients["calories"] == 420
    model_read_the_wrong_block = candidate(page_text=page, panel={"calories": 450, "protein": 28})
    assert rejection(model_read_the_wrong_block) == "values_not_in_page"


def test_a_nutrient_the_page_never_labels_is_unknown_even_if_the_model_says_zero():
    # B2: no Fiber line, a "Trans Fat 0g" line, and the model answering fiber 0.
    page = FIXTURE_MARKDOWN.replace("Fiber 1g\n", "Trans Fat 0g\n")
    assert rejection(candidate(page_text=page, panel={"fiber": 0})) == "nutrient_not_in_page"


def test_trans_and_saturated_fat_are_not_total_fat():
    page = FIXTURE_MARKDOWN.replace("Fat 18g", "Saturated Fat 3.5g\nTrans Fat 0g\nTotal Fat 18g")
    assert official.validate(candidate(page_text=page), CFA, "chicken sandwich").nutrients["fat"] == 18
    no_total = FIXTURE_MARKDOWN.replace("Fat 18g", "Saturated Fat 18g")
    assert rejection(candidate(page_text=no_total)) == "nutrient_not_in_page"


def test_serving_grams_are_kept_only_when_the_page_shows_them():
    unverified = official.validate(candidate(panel={"serving_grams": 183}), CFA, "chicken sandwich")
    assert unverified.serving_grams is None
    page = FIXTURE_MARKDOWN.replace("Serving size 1 sandwich", "Serving size 1 sandwich (183 g)")
    shown = official.validate(candidate(page_text=page, panel={"serving_grams": 183}), CFA, "chicken sandwich")
    assert shown.serving_grams == 183


def test_only_the_official_hosts_count_not_every_subdomain():
    assert rejection(candidate(source_url="https://careers.chick-fil-a.com/menu/x")) == "not_official_source"
    assert official.official_host("https://chick-fil-a.com/menu/x", CFA) == "chick-fil-a.com"


@pytest.mark.parametrize("query", [
    "Chick-fil-A chicken sandwich and waffle fries", "Chick-fil-A chicken sandwich, no pickles",
    "Chick-fil-A chicken sandwich with no pickles", "half a Chick-fil-A chicken sandwich",
    "large Chick-fil-A waffle fries", "Chick-fil-A chicken sandwich without cheese"])
@pytest.mark.asyncio
async def test_a_phrase_that_is_not_one_published_item_name_never_pays_for_a_search(monkeypatch, query):
    # S4: these can only ever end as identity_mismatch, so they are not searched.
    official.clear_process_caches()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("no provider call expected")
    monkeypatch.setattr(official, "discover", forbidden)
    assert await official.lookup(query, store=MemoryOfficialStore()) is None


@pytest.mark.asyncio
async def test_any_failed_ingest_is_remembered_including_store_errors(monkeypatch):
    # S2: a save failure must not make every later meal pay again.
    official.clear_process_caches()
    calls = []

    async def ok_ingest(source, identity):
        calls.append(identity)
        return official.validate(candidate(), CFA, "chicken sandwich")
    monkeypatch.setattr(official, "ingest", ok_ingest)

    class BrokenStore(MemoryOfficialStore):
        async def save_official_observation(self, record):
            raise RuntimeError("fixture: database unavailable")
    store = BrokenStore()
    with pytest.raises(RuntimeError):
        await official.lookup("Chick-fil-A chicken sandwich", store=store)
    assert await official.lookup("Chick-fil-A chicken sandwich", store=store) is None
    assert calls == ["chicken sandwich"]
    assert official._inflight == {}  # nit: no per-identity state outlives its ingest
    official.clear_process_caches()


@pytest.mark.asyncio
async def test_a_slow_first_sighting_returns_quickly_and_finishes_in_the_background(monkeypatch):
    # S3: the turn never waits past the deadline; the record is saved for next time.
    official.clear_process_caches()
    monkeypatch.setattr(official, "INGEST_DEADLINE_SECONDS", 0.05)
    release = asyncio.Event()
    calls = []

    async def slow_ingest(source, identity):
        calls.append(identity)
        await release.wait()
        return official.validate(candidate(), CFA, "chicken sandwich")
    monkeypatch.setattr(official, "ingest", slow_ingest)
    store = MemoryOfficialStore()
    assert await official.lookup("Chick-fil-A chicken sandwich", store=store) is None
    assert await official.lookup("Chick-fil-A chicken sandwich", store=store) is None  # still in flight
    release.set()
    await official.wait_for_background()
    found = await official.lookup("Chick-fil-A chicken sandwich", store=store)
    assert found["macros_per_serving"]["calories"] == 420 and calls == ["chicken sandwich"]
    official.clear_process_caches()


def test_official_ledger_keys_are_only_kept_for_official_evidence():
    # S5: gate off (any other provider), the persisted attribution is unchanged.
    from meal_drafts import _persistable
    fatsecret = _persistable({"name": "x", "macro_source": "FatSecret: 1", "calories": 1, "attribution": {
        "provider": "FatSecret", "external_id": "1", "source_url": "https://fatsecret.example/1",
        "retrieved_at": "2026-11-01", "cache_allowed": True}})
    assert set(fatsecret["attribution"]) == {"provider", "external_id", "cache_allowed"}
    cfa = _persistable({"name": "x", "macro_source": "Curated restaurant: x", "calories": 1, "attribution": {
        "provider": "official:chick-fil-a", "observation_id": "o", "source_url": CFA_PAGE}})
    assert cfa["attribution"]["observation_id"] == "o" and cfa["attribution"]["source_url"] == CFA_PAGE


def test_revocation_list_fails_closed(monkeypatch):
    monkeypatch.delenv("OFFICIAL_NUTRITION_REVOKED_IDS", raising=False)
    assert official.revoked_ids() == []
    keep = str(uuid4())
    monkeypatch.setenv("OFFICIAL_NUTRITION_REVOKED_IDS", f" {keep} ,")
    assert official.revoked_ids() == [keep]
    monkeypatch.setenv("OFFICIAL_NUTRITION_REVOKED_IDS", "not-a-uuid")
    assert official.revoked_ids() is None  # malformed: serve no official record at all
