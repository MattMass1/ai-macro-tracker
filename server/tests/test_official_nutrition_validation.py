"""Official restaurant nutrition for any restaurant: site verification, the
page reader and the provider adapters (no database).

Pages under tests/fixtures/*_live_20261101.md are trimmed captures of real
official pages (labeled in each file). Every other page and every provider
response is a FIXTURE written here; nothing is a live call.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

import official_nutrition as official
from test_official_restaurant_nutrition import (CFA_PAGE, CFA_TITLE, FIXTURE_MARKDOWN, MemoryOfficialStore,
                                                fixture_firecrawl)

FIXTURES = Path(__file__).parent / "fixtures"
CFA = official.Site(place="Chick-fil-A", domain="chick-fil-a.com")
MCD = official.Site(place="McDonald's", domain="mcdonalds.com")
TACO = official.Site(place="Taco Bell", domain="tacobell.com")
MCD_PAGE = "https://www.mcdonalds.com/us/en-us/product/big-mac.html"
MCD_MARKDOWN = (FIXTURES / "mcdonalds_big_mac_live_20261101.md").read_text()
TACO_TABLE = (FIXTURES / "tacobell_nutrition_table_live_20261101.md").read_text()


def page(url=CFA_PAGE, title=CFA_TITLE, text=FIXTURE_MARKDOWN):
    return official.Candidate(source_url=url, title=title, page_text=text)


def rejection(candidate, site=CFA, identity="chicken sandwich"):
    with pytest.raises(official.Rejected) as caught:
        official.validate(candidate, site, identity)
    return caught.value.reason


# --------------------------------------------------------------------------- #
# Any restaurant: identity and the official site
# --------------------------------------------------------------------------- #

def test_identity_drops_any_places_name_articles_and_plurals():
    for text in ("Chick-fil-A chicken sandwich", "a chicken sandwich from chick fil a",
                 "CFA chicken sandwiches", "Chick-fil-A® Chicken Sandwich"):
        assert official.identity_of(text, "Chick-fil-A") == "chicken sandwich"
    # A place no list knows: its own name is removed the same way.
    assert official.identity_of("Joe's Corner Grill double smash burger", "Joe's Corner Grill") == "double smash burger"
    assert official.identity_of("Big Mac®", "McDonald's") == "big mac"


@pytest.mark.parametrize("domain,place", [
    ("chick-fil-a.com", "Chick-fil-A"), ("panerabread.com", "Panera"), ("dunkindonuts.com", "Dunkin'"),
    ("chipotle.com", "Chipotle Mexican Grill"), ("raisingcanes.com", "Raising Cane's"),
    ("in-n-out.com", "In-N-Out"), ("joescornergrill.com", "Joe's Corner Grill"),
    ("mcdonalds.com", "McDonald's"), ("joespizzanyc.com", "Joe's Pizza"),
])
def test_a_restaurants_own_domain_spells_its_name(domain, place):
    assert official.names_place(domain, place)


@pytest.mark.parametrize("domain,place", [
    ("nutritionix.com", "Chick-fil-A"), ("fastfoodnutrition.org", "Chick-fil-A"),
    ("chickfilamenu.com", "Chick-fil-A"), ("chick-fil-a-menu-prices.com", "Chick-fil-A"),
    ("mcdonaldsnutrition.net", "McDonald's"), ("doordash.com", "Joe's Pizza"), ("yelp.com", "Joe's Pizza"),
])
def test_aggregators_fan_sites_and_lookalikes_are_not_official(domain, place):
    assert not official.names_place(domain, place)


def test_the_official_site_must_also_title_itself_with_the_place():
    results = [
        {"url": "https://www.nutritionix.com/chick-fil-a", "title": "Chick-fil-A Nutrition Facts"},
        {"url": "https://www.chick-fil-a.com/menu", "title": "Menu | Chick-fil-A"},
        {"url": "https://www.chick-fil-a.com/nutrition-allergens", "title": "Nutrition | Chick-fil-A"},
    ]
    assert official.pick_official_site("Chick-fil-A", results) == CFA
    untitled = [{"url": "https://www.joespizzanyc.com/", "title": "Home"}]
    assert official.pick_official_site("Joe's Pizza", untitled) is None
    assert official.pick_official_site("Joe's Pizza", [{"url": "http://www.joespizzanyc.com/",
                                                         "title": "Joe's Pizza"}]) is None


def test_only_pages_on_the_official_domain_count():
    assert official.official_host("https://order.chick-fil-a.com/menu/x", CFA) == "order.chick-fil-a.com"
    assert rejection(page(url="https://www.chick-fil-a.com.evil.example/menu/x")) == "not_official_source"
    assert rejection(page(url="http://www.chick-fil-a.com/menu/entrees/chick-fil-a-chicken-sandwich")) \
        == "not_official_source"
    assert rejection(page(url="https://www.nutritionix.com/chick-fil-a/chicken-sandwich")) == "not_official_source"


# --------------------------------------------------------------------------- #
# The page reader, on real captured pages
# --------------------------------------------------------------------------- #

def test_chick_fil_a_item_page_label_then_value():
    panel = official.validate(page(), CFA, "chicken sandwich")
    assert panel.nutrients == {"calories": 420, "protein": 29, "carbs": 41, "fat": 16, "fiber": 1}
    assert panel.serving_text == "1 menu item as published" and panel.layout == "item_page"
    assert panel.display_name == "Chick-fil-A Chicken Sandwich"
    record = official.record_for(panel, CFA)
    assert FIXTURE_MARKDOWN not in str(record["metadata"]) and record["metadata"]["page_sha256"] == panel.page_sha256
    assert record["metadata"]["official_domain"] == "chick-fil-a.com"


def test_mcdonalds_item_page_value_then_label():
    panel = official.validate(page(url=MCD_PAGE, title="Big Mac®: McDonald's Special Beef Burger",
                                   text=MCD_MARKDOWN), MCD, "big mac")
    assert panel.nutrients == {"calories": 580, "protein": 25, "carbs": 45, "fat": 34, "fiber": 3}
    assert panel.display_name == "Big Mac"


def test_taco_bell_nutrition_table_row():
    candidate = page(url="https://www.tacobell.com/nutrition/info", title="Nutrition Information | Taco Bell®",
                     text=TACO_TABLE)
    panel = official.validate(candidate, TACO, "crunchwrap supreme")
    assert panel.nutrients == {"calories": 530, "protein": 15, "carbs": 74, "fat": 20, "fiber": 6}
    assert panel.layout == "table_row"
    assert rejection(candidate, TACO, "black bean crunchwrap") == "no_panel_for_item"


def test_a_table_whose_header_cannot_be_mapped_is_never_read_by_position():
    headerless = ("| MENU ITEM | NUTRITION INFORMATION | | | | | | | |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                  "| Dave's Single | 524 | 29 | 10 | 37 | 8.2 | 1.7 | 28 | 2.2 |\n")
    wendys = official.Site(place="Wendy's", domain="wendys.com")
    candidate = page(url="https://www.wendys.com/sites/default/files/core-menu.pdf", title="", text=headerless)
    assert rejection(candidate, wendys, "daves single") == "no_panel_for_item"


def test_a_garbled_pdf_table_is_rejected():
    # Captured shape of a live PDF render: OCR-garbled headers, no total-fat column.
    garbled = ("| NUTRITION FACTS | Portion | Calories | Calories from Fat | Saturated Fat (g) | Trans Fat (g) | "
               "Cholesterol (mg) | Sodium (mg) | Catalysts (g) | Biogas (g) | Protein (g) |\n"
               "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
               "| Chicken | 2oz | 180 | 60 | 7 | 3 | 0 | 125 | 30 | 0 | 0 |\n")
    chipotle = official.Site(place="Chipotle", domain="chipotle.com")
    candidate = page(url="https://www.chipotle.com/content/dam/nutrition.pdf", title="", text=garbled)
    assert rejection(candidate, chipotle, "chicken") == "no_panel_for_item"


def test_a_table_and_an_item_page_that_disagree_are_ambiguous():
    table = ("\n| Item | Calories | Total Fat (g) | Total Carbohydrates (g) | Dietary Fiber (g) | Protein (g) |\n"
             "| --- | --- | --- | --- | --- | --- |\n| Chick-fil-A® Chicken Sandwich | 440 | 18 | 41 | 1 | 29 |\n")
    assert rejection(page(text=FIXTURE_MARKDOWN + table)) == "ambiguous_panel"


# --------------------------------------------------------------------------- #
# The page reader: what must never pass
# --------------------------------------------------------------------------- #

def test_only_this_items_own_page_is_evidence():
    spicy_url = "https://www.chick-fil-a.com/menu/entrees/spicy-chicken-sandwich"
    assert rejection(page(url=spicy_url, title="Spicy Chicken Sandwich | Chick-fil-A")) == "no_panel_for_item"
    assert rejection(page(title="Spicy Chicken Sandwich | Chick-fil-A")) == "no_panel_for_item"
    catering = "https://www.chick-fil-a.com/catering/entrees/chick-fil-a-chicken-sandwich"
    assert rejection(page(url=catering)) == "no_panel_for_item"
    index = "https://www.chick-fil-a.com/nutrition-allergens"
    assert rejection(page(url=index, title="Nutrition & Allergens | Chick-fil-A")) == "no_panel_for_item"


def test_a_sub_section_about_another_item_never_supplies_the_numbers():
    other = ("# Chick-fil-A® Chicken Sandwich\nA boneless breast of chicken.\n"
             "## Spicy Chicken Sandwich\nAmount per serving\nCalories 450\nTotal Fat 19g\n"
             "Total Carbohydrates 45g\nDietary Fiber 2g\nProtein 28g\n")
    assert rejection(page(text=other)) == "no_panel_for_item"
    sauce = (FIXTURE_MARKDOWN.replace("Dietary Fiber\n\n1g", "")
             + "\n## Chick-fil-A® Sauce\nCalories 140\nDietary Fiber 0g\n")
    assert rejection(page(text=sauce)) == "nutrient_not_in_page"


def test_a_nutrient_the_page_never_labels_is_unknown_never_zero():
    missing = FIXTURE_MARKDOWN.replace("Dietary Fiber\n\n1g", "Trans Fat\n\n0g")
    assert rejection(page(text=missing)) == "nutrient_not_in_page"


def test_qualified_fats_and_net_carbs_are_never_totals():
    assert official.labeled_values("Calories 1,020\nPolyunsaturated Fat 2g\nTotal Fat 18g\nNet Carbs 3g\n"
                                   "Total Carbohydrates 41g\nDietary Fiber 1g\nProtein 29g") == {
        "calories": 1020, "protein": 29, "carbs": 41, "fat": 18, "fiber": 1}
    only_saturated = FIXTURE_MARKDOWN.replace("Total Fat\n\n16g", "Saturated Fat\n\n16g")
    assert rejection(page(text=only_saturated)) == "nutrient_not_in_page"


def test_a_daily_value_sentence_is_not_the_calories():
    assert official.labeled_values(FIXTURE_MARKDOWN)["calories"] == 420


def test_duplicate_panels_must_agree():
    differing = FIXTURE_MARKDOWN.replace("Calories\n\n420", "Calories\n\n440", 1)
    assert rejection(page(text=differing)) == "ambiguous_panel"


def test_numbers_that_cannot_describe_one_food_are_rejected():
    bad = FIXTURE_MARKDOWN.replace("Calories\n\n420", "Calories\n\n90")
    assert rejection(page(text=bad)) == "energy_inconsistent"


def test_a_panel_with_no_serving_basis_is_rejected():
    bare = FIXTURE_MARKDOWN.replace("Amount per serving", "")
    assert rejection(page(text=bare)) == "serving_undefined"


# --------------------------------------------------------------------------- #
# Providers (FIXTURE transports)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_missing_provider_keys_are_a_stated_blocker_not_a_result(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(official.ProviderUnavailable) as caught:
        await official.find_site("Chick-fil-A")
    assert caught.value.reason == "exa_api_key_not_configured"
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(official.ProviderUnavailable) as caught:
        await official.render(CFA, CFA_PAGE, discovered_via="exa")
    assert caught.value.reason == "firecrawl_api_key_not_configured"


@pytest.mark.asyncio
async def test_discovery_searches_only_the_official_site_item_pages_first(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "fixture-key")
    sent = {}

    async def post(key, payload):
        sent.update(payload)
        return httpx.Response(200, json={"results": [
            {"url": "https://www.chick-fil-a.com/nutrition-allergens"},
            {"url": "https://www.fastfoodnutrition.org/chick-fil-a/chicken-sandwich"},
            {"url": CFA_PAGE}, {"url": CFA_PAGE}, {"url": "javascript:alert(1)"}]})
    monkeypatch.setattr(official, "post_exa_search", post)
    urls = await official.discover(CFA, "chicken sandwich")
    assert urls == [CFA_PAGE, "https://www.chick-fil-a.com/nutrition-allergens"]
    assert sent["includeDomains"] == ["chick-fil-a.com"]


@pytest.mark.asyncio
async def test_firecrawl_is_asked_for_markdown_only_and_errors_are_outages(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture-key")
    sent = {}
    status = {"code": 200, "body": fixture_firecrawl()}

    async def post(key, payload):
        sent.update(payload)
        return httpx.Response(status["code"], json=status["body"])
    monkeypatch.setattr(official, "post_firecrawl_scrape", post)
    found = await official.render(CFA, CFA_PAGE, discovered_via="exa")
    assert sent["formats"] == ["markdown"]
    assert found.title == CFA_TITLE and found.page_text == FIXTURE_MARKDOWN
    status["body"] = fixture_firecrawl(final_url="https://example.com/landing")
    moved = await official.render(CFA, CFA_PAGE, discovered_via="exa")
    assert rejection(moved) == "not_official_source"
    for code in (402, 429, 500):
        status.update(code=code, body={"success": False, "error": "fixture"})
        with pytest.raises(official.ProviderUnavailable) as caught:
            await official.render(CFA, CFA_PAGE, discovered_via="exa")
        assert caught.value.reason == f"firecrawl_http_{code}"


# --------------------------------------------------------------------------- #
# Resolution: cost, failures, background
# --------------------------------------------------------------------------- #

@pytest.fixture
def clean_caches():
    official.clear_process_caches()
    yield
    official.clear_process_caches()


@pytest.mark.parametrize("query", [
    "Chick-fil-A chicken sandwich and waffle fries", "Chick-fil-A chicken sandwich, no pickles",
    "Chick-fil-A chicken sandwich with no pickles", "half a Chick-fil-A chicken sandwich",
    "large Chick-fil-A waffle fries", "Chick-fil-A 8 count nuggets", "chicken sandwich"])
@pytest.mark.asyncio
async def test_phrases_that_are_not_one_item_at_a_place_never_pay_for_a_search(monkeypatch, clean_caches, query):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("no provider call expected")
    monkeypatch.setattr(official, "find_site", forbidden)
    monkeypatch.setattr(official, "ingest", forbidden)
    assert await official.lookup(query, store=MemoryOfficialStore()) is None


@pytest.mark.asyncio
async def test_any_place_the_conversation_names_is_looked_up(monkeypatch, clean_caches):
    seen = []

    async def find_site(place):
        seen.append(place)
        return None  # no official site found: falls back, never guessed
    monkeypatch.setattr(official, "find_site", find_site)
    token = official.requested_place.set("Joe's Corner Grill")
    try:
        assert await official.lookup("Joe's Corner Grill double smash burger", store=MemoryOfficialStore()) is None
    finally:
        official.requested_place.reset(token)
    assert seen == ["Joe's Corner Grill"]


@pytest.mark.asyncio
async def test_any_failed_ingest_is_remembered_including_store_errors(monkeypatch, clean_caches):
    calls = []

    async def ok_ingest(site, identity):
        calls.append(identity)
        return official.validate(page(), CFA, "chicken sandwich")
    monkeypatch.setattr(official, "ingest", ok_ingest)

    class BrokenStore(MemoryOfficialStore):
        async def save_official_observation(self, record):
            raise RuntimeError("fixture: database unavailable")
    store = BrokenStore(domain="chick-fil-a.com")
    with pytest.raises(RuntimeError):
        await official.lookup("Chick-fil-A chicken sandwich", store=store)
    assert await official.lookup("Chick-fil-A chicken sandwich", store=store) is None
    assert calls == ["chicken sandwich"]
    assert official._inflight == {}


@pytest.mark.asyncio
async def test_a_slow_first_sighting_returns_quickly_and_finishes_in_the_background(monkeypatch, clean_caches):
    monkeypatch.setattr(official, "INGEST_DEADLINE_SECONDS", 0.05)
    release = asyncio.Event()
    calls = []

    async def slow_ingest(site, identity):
        calls.append(identity)
        await release.wait()
        return official.validate(page(), CFA, "chicken sandwich")
    monkeypatch.setattr(official, "ingest", slow_ingest)
    store = MemoryOfficialStore(domain="chick-fil-a.com")
    assert await official.lookup("Chick-fil-A chicken sandwich", store=store) is None
    assert await official.lookup("Chick-fil-A chicken sandwich", store=store) is None
    release.set()
    await official.wait_for_background()
    found = await official.lookup("Chick-fil-A chicken sandwich", store=store)
    assert found["macros_per_serving"]["calories"] == 420 and calls == ["chicken sandwich"]


@pytest.mark.asyncio
async def test_a_background_failure_is_consumed_not_left_unretrieved(monkeypatch, clean_caches):
    monkeypatch.setattr(official, "INGEST_DEADLINE_SECONDS", 0.01)
    release = asyncio.Event()

    async def late_failure(site, identity):
        await release.wait()
        raise RuntimeError("fixture: late failure")
    monkeypatch.setattr(official, "ingest", late_failure)
    assert await official.lookup("Chick-fil-A chicken sandwich",
                                 store=MemoryOfficialStore(domain="chick-fil-a.com")) is None
    task = next(iter(official._inflight.values()))
    release.set()
    await asyncio.wait([task])
    await asyncio.sleep(0)
    assert task.done() and task._log_traceback is False


@pytest.mark.asyncio
async def test_a_revoked_panel_is_never_saved_again(monkeypatch, clean_caches):
    panel = official.validate(page(), CFA, "chicken sandwich")

    async def same_wrong_page(site, identity):
        return panel
    monkeypatch.setattr(official, "ingest", same_wrong_page)
    monkeypatch.setenv("OFFICIAL_NUTRITION_REVOKED_IDS", official.record_for(panel, CFA)["metadata"]["values_digest"])
    store = MemoryOfficialStore(domain="chick-fil-a.com")
    assert await official.lookup("Chick-fil-A chicken sandwich", store=store) is None
    assert store.saved == []


def test_revocation_list_fails_closed(monkeypatch):
    monkeypatch.delenv("OFFICIAL_NUTRITION_REVOKED_IDS", raising=False)
    assert official.revoked_ids() == []
    keep = str(uuid4())
    monkeypatch.setenv("OFFICIAL_NUTRITION_REVOKED_IDS", f" {keep} ,")
    assert official.revoked_ids() == [keep]
    monkeypatch.setenv("OFFICIAL_NUTRITION_REVOKED_IDS", "not-a-uuid")
    assert official.revoked_ids() is None


def test_official_ledger_keys_are_only_kept_for_official_evidence():
    from meal_drafts import _persistable
    fatsecret = _persistable({"name": "x", "macro_source": "FatSecret: 1", "calories": 1, "attribution": {
        "provider": "FatSecret", "external_id": "1", "source_url": "https://fatsecret.example/1",
        "retrieved_at": "2026-11-01", "cache_allowed": True}})
    assert set(fatsecret["attribution"]) == {"provider", "external_id", "cache_allowed"}
    cfa = _persistable({"name": "x", "macro_source": "Curated restaurant: x", "calories": 1, "attribution": {
        "provider": "official:chick-fil-a", "observation_id": "o", "source_url": CFA_PAGE}})
    assert cfa["attribution"]["observation_id"] == "o"


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
        monkeypatch.setenv("OFFICIAL_RESTAURANT_NUTRITION_USER_IDS", str(user))
        assert release_gates.official_restaurant_nutrition_allowed()
    finally:
        reset_user(scope)
