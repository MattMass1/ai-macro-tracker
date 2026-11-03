"""Activation invariants, entirely synthetic; PG uses the existing disposable fixture."""
import copy
import json
from datetime import timedelta
from uuid import uuid4
import pytest
from food_agent.coordinator import FoodAgent
from food_agent.drafts import MemoryDrafts, StoreDrafts, new_draft, draft_to_row
from food_agent.contracts import DraftItemState, ResolvedFood, Modification
from food_agent.ports import RecallResult
from food_agent.find_food import to_resolved_food
from food_agent.recall import HistoryRecaller
from test_food_agent_flow import Clock, Script, Finder, HALF_QP, revise, _portion
from test_food_agent_pg import env, pg, As, _agent, _rows


def rig():
    clock, script, finder = Clock(), Script(), Finder()
    drafts = MemoryDrafts(clock=clock)
    return FoodAgent(drafts, script, finder, clock=clock), drafts, script, finder, clock

async def test_read_outage_then_recovery_same_operation_no_duplicate():
    agent, drafts, script, _, _ = rig()
    script.push(HALF_QP)
    await agent.turn('half burger')
    original = drafts.fetch_entry
    async def unavailable(_): raise ConnectionError('synthetic outage')
    drafts.fetch_entry = unavailable
    assert (await agent.turn('yes')).outcome.effect == 'unknown'
    first_key = drafts.commit_calls[0][:3]
    assert (await agent.turn('a new meal')).outcome.effect == 'unknown'
    assert not [k for k in drafts.ops if k.startswith('food-agent-ack:')]
    drafts.fetch_entry = original
    recovered = await agent.turn('yes')
    assert recovered.receipt and recovered.receipt.verified
    assert len(drafts.entries) == 1 and len(drafts.commit_calls) == 1
    assert drafts.commit_calls[0][:3] == first_key
    assert await drafts.recovery() == []

async def test_recovery_outlives_context_window_and_eight_newer_drafts():
    agent, drafts, script, _, clock = rig()
    script.push(HALF_QP)
    await agent.turn('half burger')
    drafts.fail_after_commit = 1
    await agent.turn('yes')
    original_id = next(iter(drafts.drafts))
    clock.now += timedelta(days=3)
    for _ in range(10):
        row = copy.deepcopy(drafts.drafts[original_id])
        row.update(id=str(uuid4()), state='canceled', pending_question=None, updated_at=clock.now)
        drafts.drafts[row['id']] = row
    assert all(d.id != original_id for d in await drafts.recent(clock.now - timedelta(hours=24)))
    result = await agent.turn('yes')
    assert result.receipt and result.receipt.verified and len(drafts.entries) == 1

async def test_expired_operation_record_is_unknown_not_new_commit():
    agent, drafts, script, _, clock = rig()
    script.push(HALF_QP)
    await agent.turn('half burger')
    drafts.fail_after_commit = 1
    await agent.turn('yes')
    drafts.ops.clear()  # simulate seven-day replay retention, not deletion of a consumed row
    clock.now += timedelta(days=8)
    result = await agent.turn('yes')
    assert result.outcome.effect == 'unknown' and len(drafts.entries) == 1
    assert len(drafts.commit_calls) == 1 and await drafts.recovery()

async def test_cross_session_reshow_then_own_confirmation():
    agent, drafts, script, _, _ = rig()
    script.push(HALF_QP)
    await agent.turn('half burger', session_ref='A')
    result = await agent.turn('yes', session_ref='B')
    assert result.preview and not drafts.entries
    result = await agent.turn('yes', session_ref='B')
    assert result.receipt and result.receipt.verified and len(drafts.entries) == 1

async def test_modification_only_revision_clears_authority():
    agent, drafts, script, _, _ = rig()
    script.push(HALF_QP)
    await agent.turn('half burger')
    script.push({'type': 'revise_item', 'item_ref': 'i1', 'modifications': [{'op':'remove','text':'cheese'}]})
    result = await agent.turn('remove cheese')
    assert result.preview is None and 'source-backed' in result.reply
    await agent.turn('yes')
    assert not drafts.entries

async def test_recalled_modifications_are_not_silently_discarded():
    agent, drafts, script, finder, _ = rig()
    script.push(HALF_QP)
    await agent.turn('half burger')
    item = (await drafts.get(next(iter(drafts.drafts)))).items[0]
    modified = item.model_copy(update={'modifications':[Modification(op='extra',text='patty')]})
    class Recaller:
        async def recall(self, *args): return RecallResult('found', (modified,))
    agent.recaller = Recaller()
    script.push({'type':'recall_meal','query':'burger'})
    result = await agent.turn('same extra patty burger')
    assert not result.preview and 'source-backed' in result.reply
    await agent.turn('yes')
    assert not drafts.entries


def source_row(**extra):
    return {'name':'Synthetic bar','source':'FatSecret:42','serving_size':'1 bar',
            'macros_per_serving':{'calories':100,'protein':10,'carbs':5,'fat':4,'fiber':1},
            'attribution':{'provider':'FatSecret','external_id':'42','cache_allowed':False,
                           'attribution_text':'Nutrition data from FatSecret'}, **extra}

@pytest.mark.parametrize('nested', [False, True])
def test_nested_policy_and_retention_preserved_without_draft_panels(nested):
    found = source_row()
    if nested:
        found['attribution'] = {'provider':'Composite','cache_allowed':True}
        found['component_metadata'] = [{'attribution':{'provider':'FatSecret','cache_allowed':False,
                'retention_policy':'ephemeral','attribution_text':'keep this'}, 'panel':{'calories':999}}]
    food = to_resolved_food(found)
    assert not food.cache_allowed
    item = DraftItemState(item_ref='i1',identity={'name':'Synthetic bar','kind':'packaged'},
                          portion={'kind':'count','amount':1.,'text':'one'},resolved=food,multiplier=1.)
    row = draft_to_row(new_draft(log_date=Clock()().date(),session_ref=None,items=(item,)))
    stored = row['items'][0]['resolution']['state']['resolved']
    assert stored['panel'] == {} and stored['cache_allowed'] is False
    assert '999' not in json.dumps(stored)
    assert ('retention_policy' in json.dumps(stored)) == nested
    assert 'attribution_text' in json.dumps(stored)

async def test_no_cache_refresh_change_requires_new_preview():
    agent, drafts, script, finder, _ = rig()
    original = finder.find
    count = 0
    async def restricted(identity, portion):
        nonlocal count
        count += 1
        result = await original(identity, portion)
        from dataclasses import replace
        food = result.food.model_copy(update={'cache_allowed':False})
        if count > 1:
            food = food.model_copy(update={'panel':food.panel.model_copy(update={'calories':600.})})
        return replace(result,food=food)
    finder.find = restricted
    script.push(HALF_QP)
    first = await agent.turn('half burger')
    result = await agent.turn('yes')
    assert count == 2 and result.preview and not drafts.entries
    assert result.preview.content_hash != first.preview.content_hash
    final = await agent.turn('yes')
    assert final.receipt and final.receipt.nutrients.calories == 300
    assert len(drafts.entries) == 1

async def test_missing_session_cannot_consume_named_session_preview():
    agent, drafts, script, _, _ = rig()
    script.push(HALF_QP)
    await agent.turn('half burger', session_ref='A')
    shown = await agent.turn('yes', session_ref=None)
    assert shown.preview and not drafts.entries
    result = await agent.turn('yes', session_ref=None)
    assert result.receipt and len(drafts.entries) == 1

async def test_restricted_history_keeps_original_consumed_quantity_for_refresh():
    agent, drafts, script, _, clock = rig()
    script.push(HALF_QP)
    await agent.turn('half burger')
    await agent.turn('yes')
    entry = copy.deepcopy(next(iter(drafts.entries.values())))
    entry['day'] = entry['date']
    entry['component_metadata']['items'][0]['cache_allowed'] = False
    entry['component_metadata']['items'][0]['attribution'] = {'provider':'FatSecret','cache_allowed':False}
    class History:
        async def search_own_entries(self,*args,**kwargs): return [entry]
    found = await HistoryRecaller(History(),clock=clock).recall('quarter pounder',None)
    assert found.status == 'found'
    assert found.items[0].resolved is None and found.items[0].portion.amount == .5

async def test_source_attribution_is_delivered_in_canvas_preview():
    from test_food_agent_canvas import Rig, HALF_QP as action
    from auth import bind_user, reset_user
    scope = bind_user(uuid4())
    try:
        canvas = Rig()
        canvas.script.push(action)
        result = await canvas.say('half quarter pounder')
        assert 'Nutrition data from FatSecret' in result['reply']
    finally:
        reset_user(scope)

async def test_history_adapter_preserves_unsupported_modifications():
    agent, drafts, script, _, clock = rig()
    script.push(HALF_QP)
    await agent.turn('half burger')
    await agent.turn('yes')
    entry = copy.deepcopy(next(iter(drafts.entries.values())))
    entry['day'] = entry['date']
    entry['component_metadata']['items'][0]['modifications'] = [{'op':'extra','text':'patty'}]
    class History:
        async def search_own_entries(self, *args, **kwargs): return [entry]
    found = await HistoryRecaller(History(), clock=clock).recall('quarter pounder', None)
    assert found.status == 'found'
    assert found.items[0].modifications[0].text == 'patty'

@pg
async def test_pg_recovery_outside_window_owner_scoped_and_durable_ack(env):
    agent, script, _ = _agent(env['store'])
    with As(env['users'][0]):
        script.push(HALF_QP)
        await agent.turn('half burger',session_ref='A')
        original = agent.drafts.fetch_entry
        async def outage(_): raise ConnectionError('synthetic outage')
        agent.drafts.fetch_entry = outage
        assert (await agent.turn('yes',session_ref='A')).outcome.effect == 'unknown'
        await _rows(env['url'], "UPDATE meal_drafts SET updated_at=now()-interval '3 days' WHERE user_id=$1", env['users'][0])
        assert len(await agent.drafts.recovery()) == 1
    with As(env['users'][1]):
        assert await agent.drafts.recovery() == []
    with As(env['users'][0]):
        agent.drafts.fetch_entry = original
        recovered = await agent.turn('yes',session_ref='A')
        assert recovered.receipt and recovered.receipt.verified
        assert await agent.drafts.recovery() == []
        await _rows(env['url'], 'DELETE FROM request_operations WHERE user_id=$1', env['users'][0])
        assert await agent.drafts.recovery() == []
    assert len(await _rows(env['url'], 'SELECT id FROM nutrition_entries')) == 1

@pg
async def test_pg_no_cache_json_and_unchanged_refresh_commits_once(env):
    agent, script, finder = _agent(env['store'])
    original = finder.find
    async def restricted(*args):
        from dataclasses import replace
        result = await original(*args)
        return replace(result, food=result.food.model_copy(update={'cache_allowed':False}))
    finder.find = restricted
    with As(env['users'][0]):
        script.push(HALF_QP)
        await agent.turn('half burger',session_ref='A')
        (row,) = await _rows(env['url'], 'SELECT resolution FROM meal_draft_items WHERE user_id=$1', env['users'][0])
        resolution = json.loads(row['resolution'])
        assert resolution['state']['resolved']['panel'] == {}
        result = await agent.turn('yes',session_ref='A')
        assert result.receipt and result.receipt.verified
        assert result.receipt.nutrients.calories == 210
    assert len(await _rows(env['url'], 'SELECT id FROM nutrition_entries')) == 1
