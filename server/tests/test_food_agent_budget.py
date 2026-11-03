"""Synthetic rate-card boundaries: no real wire, keys or subscription assumptions."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from decimal import Decimal
import httpx
import pytest
import food_agent_bench_live as live
from food_agent_budget import RateCard, PricingRefused, TERRA


def card():
    return RateCard(version="synthetic-test-only", providers={"fatsecret": "0.01", "exa": "0.02", "cofid": "0"},
                    evidence={"fatsecret": "synthetic fixture", "exa": "synthetic fixture", "cofid": "synthetic fixture"})


def payload():
    return {"model": TERRA, "input": "synthetic input", "max_output_tokens": 2000}

async def test_dollar_reservation_exact_boundary_and_unknown_usage_retained():
    calls = []
    async def wire(token, body):
        calls.append(body)
        return httpx.Response(200, json={}, request=httpx.Request('POST', 'https://fake.invalid'))
    amount, _, _ = card().model_cost(payload())
    budget = live.Budget(max_usd=amount, rate_card=card())
    post = live.counted_model_post(wire, budget)
    await post('synthetic', payload())
    assert budget.reserved_usd == amount
    assert budget.model_calls[0]['total_tokens'] is None
    with pytest.raises(live.CapReached):
        await post('synthetic', payload())
    assert len(calls) == 1

@pytest.mark.parametrize('change', [ {'model': 'unpriced'}, {'tools': [{'type': 'web_search'}]},
    {'max_output_tokens': None}, {'input': [{'role':'user', 'content':[{'type':'input_image','image_url':'https://fake.invalid'}]}]},
    {'input': 'x' * 32000}, {'service_tier':'priority'}, {'previous_response_id':'fake'}])
async def test_unpriced_routes_refused_before_wire(change):
    calls = []
    async def wire(*a):
        calls.append(a)
    budget = live.Budget(rate_card=card())
    with pytest.raises(live.CapReached):
        await live.counted_model_post(wire, budget)('synthetic', {**payload(), **change})
    assert not calls and budget.reserved_usd == 0

async def test_responses_extraction_and_fallback_share_model_cap():
    calls = []
    async def wire(*a):
        calls.append(a)
        return httpx.Response(503, request=httpx.Request('POST', 'https://fake.invalid'))
    budget = live.Budget(max_model_calls=1, rate_card=card())
    import web_nutrition_lookup as web
    with live.install_counters(budget, web_post=wire):
        await web.post_responses('synthetic', payload())
        with pytest.raises(live.CapReached):
            await web.post_responses('synthetic', payload())
    assert len(calls) == budget.model_calls_used == 1
    assert budget.lookups_used == 1 and budget.reserved_usd > 0

async def test_unknown_lookup_price_refuses_before_wire():
    calls = []
    async def wire(*a): calls.append(a)
    with pytest.raises(live.CapReached):
        await live.counted_post(wire, live.Budget(), 'exa')('synthetic', {})
    assert not calls

@pytest.mark.parametrize('kwargs', [{'max_usd': 11}, {'max_model_calls':301}, {'max_lookups':201}, {'max_usd':'NaN'}])
def test_authorized_caps_cannot_be_raised(kwargs):
    with pytest.raises((ValueError, PricingRefused)):
        live.Budget(**kwargs)

async def test_hosted_tools_reserve_full_context_and_tool_fee_before_wire():
    rates = RateCard(version="synthetic", providers={}, evidence={}, hosted_models={
        "synthetic-hosted": {"version":"fixture", "source":"synthetic fixture",
            "max_context_tokens":32000, "input_per_million":10, "output_per_million":20,
            "tool_per_call_usd": .05}})
    body = {"model":"synthetic-hosted","input":"synthetic", "max_output_tokens":1000,
            "tools":[{"type":"web_search"}], "max_tool_calls":1}
    cost, input_bound, output_bound = rates.model_cost(body)
    assert cost == Decimal(".71") and input_bound == 32000 and output_bound == 1000
    calls = []
    async def wire(*args): calls.append(args)
    with pytest.raises(live.CapReached):
        await live.counted_model_post(wire, live.Budget(max_usd=".38",rate_card=rates))("synthetic",body)
    assert not calls

async def test_failed_model_attempt_retains_reservation_and_retry_cannot_bypass():
    rates = card()
    cost, _, _ = rates.model_cost(payload())
    calls = []
    async def wire(*args):
        calls.append(args)
        raise httpx.ReadTimeout("synthetic")
    budget = live.Budget(max_usd=cost,rate_card=rates)
    post = live.counted_model_post(wire,budget)
    with pytest.raises(httpx.ReadTimeout): await post("synthetic",payload())
    with pytest.raises(live.CapReached): await post("synthetic",payload())
    assert len(calls) == 1 and budget.reserved_usd == cost

async def test_configuration_refuses_before_backend_setup(monkeypatch):
    from test_food_agent_bench_live import _spec
    class NoBackend:
        name = 'synthetic'
        async def new_case(self): raise AssertionError('setup before preflight')
    for name in ('OPENAI_API_KEY','OPENAI_ACCESS_TOKEN','FOOD_AGENT_MODEL','COACH_MODEL',
                 'WEB_LOOKUP_PROVIDER','FATSECRET_CLIENT_ID','FATSECRET_CONSUMER_KEY'):
        monkeypatch.delenv(name,raising=False)
    result = await live.run_live_bench(_spec('syn-boiled-eggs-control'),NoBackend(),live.Budget(),repeats=1)
    assert result['summary']['runs']['completed'] == 0
    assert result['summary']['runs']['not_run_configuration'] == 1
    assert result['summary']['model_calls'] == 0 and result['configuration_errors']
