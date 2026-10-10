"""Layer 2 via Exa search + a no-tools OpenAI extraction. Synthetic fixtures only.

Owner decision 2026-10-09: Exa replaces the single hosted web_search step for
speed; the OpenAI web_search path stays as the fallback for an Exa outage. The
result contract, citation verification (the cited URL must be one Exa actually
returned) and one-tap confirm are unchanged."""
import json

import httpx
import pytest

import web_nutrition_lookup as web

QUINOA_URL = "https://example.test/quinoa"


def exa_response(results, status=200):
    return httpx.Response(status, request=httpx.Request("POST", web.EXA_SEARCH_URL),
                          headers={"content-type": "application/json"},
                          content=json.dumps({"results": results}).encode())


def exa_result(url=QUINOA_URL, *, title="Synthetic nutrition table", highlights=None, text=""):
    return {"id": url, "url": url, "title": title, "text": text,
            "highlights": highlights if highlights is not None else [
                "Per 100 g cooked: 120 kcal, protein 4.4 g, carbs 21.3 g, fat 1.9 g, fiber 2.8 g."]}


def extraction(raw):
    """A Responses reply from the extraction call: one message, no web_search_call."""
    return httpx.Response(200, request=httpx.Request("POST", web.RESPONSES_URL),
                          headers={"content-type": "application/json"},
                          content=json.dumps({"model": "fixture-model", "output": [
                              {"type": "message", "content": [{"type": "output_text",
                                                               "text": json.dumps(raw)}]}]}).encode())


def evidence(source_url=QUINOA_URL, **overrides):
    raw = {"food_name": "cooked red quinoa", "preparation": "cooked",
           "basis_amount": 100, "basis_unit": "g", "serving_grams": 100,
           "calories": 120, "protein": 4.4, "carbs": 21.3, "fat": 1.9, "fiber": 2.8,
           "portion_assumption": "100 g cooked portion", "source_url": source_url,
           "source_title": "Synthetic nutrition table",
           "evidence_excerpt": "Per 100 g cooked: 120 kcal, protein 4.4 g, carbs 21.3 g, fat 1.9 g, fiber 2.8 g."}
    raw.update(overrides)
    return raw


@pytest.fixture(autouse=True)
def exa_env(monkeypatch):
    web.clear_cache()
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "synthetic-openai")
    monkeypatch.setenv("COACH_MODEL", "fixture-model")
    monkeypatch.setenv("EXA_API_KEY", "synthetic-exa")
    monkeypatch.delenv("WEB_LOOKUP_PROVIDER", raising=False)
    monkeypatch.delenv("WEB_LOOKUP_EXTRACT_MODEL", raising=False)


def test_provider_selection_defaults_to_exa_only_when_a_key_exists(monkeypatch):
    assert web.provider_name() == "exa"
    monkeypatch.setenv("WEB_LOOKUP_PROVIDER", "openai")
    assert web.provider_name() == "openai"
    monkeypatch.delenv("WEB_LOOKUP_PROVIDER")
    monkeypatch.delenv("EXA_API_KEY")
    assert web.provider_name() == "openai"


async def test_exa_search_then_extraction_returns_the_same_contract():
    seen = {}

    async def post_exa(key, payload):
        seen["exa"] = (key, payload)
        return exa_response([exa_result(), exa_result("https://other.test/rice", title="Rice")])

    async def post_openai(token, payload):
        seen["openai"] = (token, payload)
        return extraction(evidence())

    found = await web.lookup("200 g cooked red quinoa", post=post_openai, post_exa=post_exa)
    key, exa_payload = seen["exa"]
    assert key == "synthetic-exa"
    assert "200 g cooked red quinoa" in exa_payload["query"]
    assert exa_payload["numResults"] == web.EXA_RESULTS
    assert "usda.gov" in exa_payload["excludeDomains"]
    assert "highlights" in exa_payload["contents"]
    token, extract_payload = seen["openai"]
    assert token == "synthetic-openai" and extract_payload["model"] == "fixture-model"
    assert "tools" not in extract_payload  # extraction never searches
    assert QUINOA_URL in extract_payload["input"] and "https://other.test/rice" in extract_payload["input"]
    assert extract_payload["text"]["format"]["name"] == "nutrition_evidence"
    assert found["name"] == "200 g cooked red quinoa"
    assert found["macros_per_100g"]["fiber"] == 2.8
    assert found["estimate_provenance"] == "server_web_estimate"
    assert found["source"].startswith("WEB ESTIMATE (unverified): example.test")
    assert found["attribution"]["provider"] == "Exa search + OpenAI extraction"
    assert found["attribution"]["candidate_url"] == QUINOA_URL
    assert found["attribution"]["verification_state"] == "unverified_web_estimate"
    assert found["attribution"]["cache_allowed"] is False


async def test_extraction_must_cite_a_url_exa_actually_returned():
    async def post_exa(_key, _payload):
        return exa_response([exa_result()])

    async def post_openai(_token, _payload):
        return extraction(evidence(source_url="https://invented.test/quinoa"))

    with pytest.raises(web.NutritionLookupError) as error:
        await web.lookup("cooked red quinoa", post=post_openai, post_exa=post_exa)
    assert error.value.reason == "unverified_evidence"


async def test_extraction_with_no_exact_evidence_is_a_fast_no_result():
    openai_calls = []

    async def post_exa(_key, _payload):
        return exa_response([exa_result(highlights=["Quinoa is a seed native to the Andes."])])

    async def post_openai(_token, payload):
        openai_calls.append(payload)
        return extraction(evidence(source_url=""))

    with pytest.raises(web.NutritionLookupError) as error:
        await web.lookup("cooked red quinoa", post=post_openai, post_exa=post_exa)
    assert error.value.reason == "no_results"
    assert len(openai_calls) == 1 and "tools" not in openai_calls[0]  # no web_search fallback


async def test_exa_without_usable_results_never_calls_the_model():
    async def post_exa(_key, _payload):
        return exa_response([{"url": "https://example.test/empty", "title": "Empty", "text": "", "highlights": []},
                             {"url": "https://fdc.nal.usda.gov/x", "title": "USDA", "highlights": ["120 kcal"]}])

    async def post_openai(_token, _payload):
        raise AssertionError("no sources, so nothing to extract")

    with pytest.raises(web.NutritionLookupError) as error:
        await web.lookup("cooked red quinoa", post=post_openai, post_exa=post_exa)
    assert error.value.reason == "no_results"


@pytest.mark.parametrize(("status", "reason"), [(503, "exa_unavailable"), (429, "exa_rate_limited"),
                                                 (401, "exa_unauthorized")])
async def test_exa_outage_falls_back_to_openai_web_search(status, reason):
    from test_web_nutrition_lookup import fixture, response
    payloads = []

    async def post_exa(_key, _payload):
        return exa_response([], status=status)

    async def post_openai(_token, payload):
        payloads.append(payload)
        return response(fixture())

    found = await web.lookup("cooked red quinoa", post=post_openai, post_exa=post_exa)
    assert found["attribution"]["provider"] == "OpenAI web_search"
    assert payloads[0]["tools"][0]["type"] == "web_search"
    assert reason in web.INFRASTRUCTURE_REASONS


async def test_exa_timeout_falls_back_and_is_an_infrastructure_reason():
    from test_web_nutrition_lookup import fixture, response

    async def post_exa(_key, _payload):
        raise httpx.ReadTimeout("slow")

    async def post_openai(_token, _payload):
        return response(fixture())

    found = await web.lookup("cooked red quinoa", post=post_openai, post_exa=post_exa)
    assert found["attribution"]["provider"] == "OpenAI web_search"
    assert "exa_timeout" in web.INFRASTRUCTURE_REASONS


async def test_extraction_model_is_overridable_and_results_are_cached_per_provider(monkeypatch):
    monkeypatch.setenv("WEB_LOOKUP_EXTRACT_MODEL", "fixture-mini")
    models, exa_calls = [], []

    async def post_exa(_key, _payload):
        exa_calls.append(1); return exa_response([exa_result()])

    async def post_openai(_token, payload):
        models.append(payload["model"]); return extraction(evidence())

    first = await web.lookup("cooked red quinoa", post=post_openai, post_exa=post_exa)
    second = await web.lookup("cooked red quinoa", post=post_openai, post_exa=post_exa)
    assert models == ["fixture-mini"] and exa_calls == [1]
    assert first == second
    assert web.recent_failure("cooked red quinoa") is None
