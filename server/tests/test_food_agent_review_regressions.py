"""Regression assertions adapted from independent review, synthetic only."""
import httpx
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pytest
import json
from uuid import uuid4
from auth import bind_user, reset_user
from food_agent.contracts import ResolvedFood
from food_agent.ports import FindResult
from test_food_agent_canvas import Rig
from test_food_agent_flow import Script, Finder, Clock, HALF_QP, add, _ident, _portion
from food_agent.coordinator import FoodAgent
from food_agent.drafts import MemoryDrafts
import food_agent_bench_live as live
import food_agent_bench as bench

async def test_preview_must_not_commit_items_hidden_by_canvas_truncation():
    scope = bind_user(uuid4())
    try:
        rig = Rig()
        class LongFinder:
            async def find(self, identity, portion):
                food = ResolvedFood(food_id=identity.name, provider='synthetic', display_name=identity.name,
                    macro_source='Synthetic source', serving_basis={'text':'1 sandwich', 'amount':1.0,'unit':'count'},
                    panel={'calories':100.0,'protein':5.0,'carbs':10.0,'fat':4.0,'fiber':1.0})
                return FindResult('matched', food)
        rig.agent.finder = LongFinder()
        names = [f'Synthetic sandwich {i:02d} ' + 'long descriptive label '*3 for i in range(12)]
        rig.script.push(add(*[(_ident(n), _portion('serving_fraction',1.0,text='one serving')) for n in names]))
        shown = await rig.say('Log twelve distinct synthetic sandwiches')
        assert len(shown['reply']) <= 1000
        assert 'split' in shown['reply'].lower()
        assert names[-1] not in shown['reply']
        result = await rig.say('yes')
        print('TRUNCATION', len(shown['reply']), 'saved_items', sum(len(e['component_metadata']['items']) for e in rig.drafts.entries.values()))
        assert not rig.drafts.entries, 'Unshown items must not be committed by this confirmation'
    finally:
        reset_user(scope)

async def test_modification_must_not_silently_keep_unmodified_panel():
    clock, script, finder = Clock(), Script(), Finder()
    drafts = MemoryDrafts(clock=clock)
    agent = FoodAgent(drafts, script, finder, clock=clock)
    action = add((_ident('Quarter Pounder', "McDonald's", 'restaurant'), _portion('serving_fraction',1.0,text='one')))
    action['items'][0]['modifications'] = [{'op':'extra','text':'beef patty'}]
    script.push(action)
    preview = await agent.turn('Quarter Pounder with an extra beef patty')
    result = await agent.turn('yes')
    print('MODIFICATION', preview.reply, result.outcome.effect)
    assert not drafts.entries, 'Unsupported extra patty must ask/disclose rather than log unchanged standard panel'

async def test_preview_from_another_session_cannot_be_confirmed_unseen():
    clock, script, finder = Clock(), Script(), Finder()
    drafts = MemoryDrafts(clock=clock)
    agent = FoodAgent(drafts, script, finder, clock=clock)
    script.push(HF := HALF_QP)
    await agent.turn('Half a quarter pounder', session_ref='synthetic-session-A')
    result = await agent.turn('yes', session_ref='synthetic-session-B')
    print('CROSS_SESSION', result.outcome.effect, len(drafts.entries))
    assert not drafts.entries, 'Session B never displayed this meal preview'

def test_live_preflight_requires_credentials_and_explicit_route(tmp_path):
    errors = live.preflight(pg_admin_url='postgresql://localhost/reviewer_disposable',
                            out=tmp_path/'unused.json', env={})
    print('EMPTY_ENV_PREFLIGHT', errors)
    assert errors, 'Absent credentials/route must refuse before DB setup or quality scoring'

async def test_responses_model_cannot_bypass_zero_model_budget():
    budget = live.Budget(max_model_calls=0, max_lookups=20)
    wire = []
    async def fake_post(token, payload):
        wire.append(payload)
        return httpx.Response(200, json={'model':'synthetic','usage':{'input_tokens':100,'output_tokens':10}},
                              request=httpx.Request('POST','https://fake.invalid/responses'))
    import web_nutrition_lookup as web
    with live.install_counters(budget, web_post=fake_post):
        try:
            await web.post_responses('synthetic-token', {'model':'synthetic','max_output_tokens':500})
        except live.CapReached:
            pass
    print('RESPONSES_BUDGET', len(wire), budget.model_calls_used, budget.lookups_used)
    assert not wire, 'OpenAI Responses is a model call even when used for food lookup'

async def test_missing_credentials_are_not_completed_live_quality_runs(monkeypatch):
    from test_food_agent_bench_live import _spec, _providers, FakeModel
    monkeypatch.delenv('OPENAI_ACCESS_TOKEN', raising=False)
    spec = _spec('syn-boiled-eggs-control')
    model = FakeModel(spec)
    result = await live.run_live_bench(spec, bench.MemoryBackend(), live.Budget(), repeats=1, **_providers(model))
    print('MISSING_CREDENTIALS', result['summary']['runs'], result['summary']['model_calls'], result['runs'][0]['status'])
    assert result['summary']['runs']['completed'] == 0

async def test_reconciliation_read_error_must_not_acknowledge_as_deleted():
    clock, script, finder = Clock(), Script(), Finder()
    drafts = MemoryDrafts(clock=clock)
    agent = FoodAgent(drafts, script, finder, clock=clock)
    script.push(HALF_QP)
    await agent.turn('Half a quarter pounder')
    async def disconnected(entry_id):
        raise ConnectionError('synthetic read outage')
    drafts.fetch_entry = disconnected
    initial = await agent.turn('yes')
    followup = await agent.turn('yes')
    acks = [key for key in drafts.ops if key.startswith('food-agent-ack:')]
    print('READBACK_OUTAGE', initial.outcome.effect, followup.reply, 'rows', len(drafts.entries), 'acks', len(acks))
    assert not acks, 'A failed read is not proof of deletion; keep reconciliation pending'

async def test_no_cache_provider_panel_must_not_persist_in_draft():
    from test_food_agent_canvas import HALF_QP as canvas_half
    scope = bind_user(uuid4())
    try:
        rig = Rig()
        rig.script.push(canvas_half)
        await rig.say('Half a quarter pounder')
        (row,) = rig.drafts.drafts.values()
        stored = row['items'][0]['resolution']['state']['resolved']
        print('PROVIDER_PANEL', stored['provider'], stored['panel'], 'cache_policy_present', 'cache_allowed' in stored)
        assert not stored.get('panel'), 'Drafts must not cache a provider panel without caching permission'
    finally:
        reset_user(scope)

def test_malformed_model_error_is_not_successful_clarification():
    case = {'id':'synthetic-malformed','expected_action':'clarify','gold_items':[], 'old_app':{}}
    run = bench.CaseRun(case_id=case['id'], turns=[bench.TurnRecord(idx=0,user='synthetic correction',
        reply="Sorry, I couldn't read that. Can you say it again?", is_question=True, status='error')])
    score = bench.score_case(case, {}, run)
    print('MALFORMED_CLARIFY', score['success'], score['reasons'])
    assert not score['success'], 'Interpreter error is not correct clarification'

def test_scorer_rejects_wrong_nutrition_not_just_self_consistency():
    from test_food_agent_bench_live import _qp_case, _qp_run
    case, script = _qp_case()
    run = _qp_run('Quarter Pounder with Cheese',1.0)
    entry = run.entries[0]
    entry.update(calories=1.0, protein=1.0)
    run.turns[1].reply = 'Logged it: 1 g protein, 1 kcal.'
    score = live.score_live(case, script, run, {'FatSecret:4242':"McDonald's"})
    print('SCORER_WRONG_PANEL', score['success'], score['reasons'])
    assert not score['success'], 'Self-consistent wrong nutrition must not be quality success'
