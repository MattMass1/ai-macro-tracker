"""A rejected weight basis must not be replaced by an incompatible estimate."""
import os
from copy import deepcopy

import pytest

os.environ.setdefault('APP_SHARED_TOKEN', 'offline-weight-test')
os.environ.setdefault('DATABASE_URL', 'postgresql://fixture.invalid/weight-test')

from test_meal_flow_v2 import engine, TUNA_RAW, TUNA_COOKED


@pytest.mark.parametrize('typical', ['on', 'off'])
@pytest.mark.parametrize('requested,panel', [('raw', TUNA_COOKED), ('cooked', TUNA_RAW)])
async def test_mismatched_weight_is_not_logged_by_any_fallback(engine, monkeypatch, typical, requested, panel):
    monkeypatch.setenv('TYPICAL_SERVING_ESTIMATES', typical)
    engine['foods'][f'{requested} grilled chicken'] = dict(panel)
    result = await engine['engine'].handle({'add': [{
        'description': 'grilled chicken', 'quantity': 100, 'unit': 'g', 'preparation': requested}]})
    assert result['status'] == 'needs_detail', result
    assert engine['store'].entries == {}
    draft = engine['store'].drafts[result['draft_ref']]
    assert draft['items'][0]['preparation'] == requested
    assert draft['items'][0]['resolution'] is None


async def test_missing_provider_does_not_allow_cooked_typical_estimate_for_raw_weight(engine):
    result = await engine['engine'].handle({'add': [{
        'description': 'grilled chicken', 'quantity': 100, 'unit': 'g', 'preparation': 'raw'}]})
    assert result['status'] == 'needs_detail', result
    assert engine['store'].entries == {}


async def test_cooked_description_cannot_use_raw_panel_without_explicit_preparation(engine):
    engine['foods']['grilled chicken'] = dict(TUNA_RAW)
    result = await engine['engine'].handle({'add': [{
        'description': 'grilled chicken', 'quantity': 100, 'unit': 'g'}]})
    assert result['status'] == 'needs_detail', result
    assert not engine['store'].entries


async def test_followup_cannot_change_explicit_weight_basis_to_available_panel(engine):
    engine['foods']['raw grilled chicken'] = dict(TUNA_COOKED)
    engine['foods']['cooked grilled chicken'] = dict(TUNA_COOKED)
    first = await engine['engine'].handle({'add': [{
        'description': 'grilled chicken', 'quantity': 100, 'unit': 'g', 'preparation': 'raw'}]})
    await engine['engine'].handle({'draft_ref': first['draft_ref']})
    assert not engine['store'].entries
    assert engine['store'].drafts[first['draft_ref']]['items'][0]['preparation'] == 'raw'


async def test_explicit_correction_resolves_the_same_draft_once(engine):
    engine['foods']['raw grilled chicken'] = dict(TUNA_COOKED)
    engine['foods']['cooked grilled chicken'] = dict(TUNA_COOKED)
    first = await engine['engine'].handle({'add': [{
        'description': 'grilled chicken', 'quantity': 100, 'unit': 'g', 'preparation': 'raw'}]})
    assert not engine['store'].entries
    corrected = await engine['engine'].handle({'draft_ref': first['draft_ref'],
        'edit': [{'item_ref': 'item1', 'preparation': 'cooked'}]})
    assert corrected['status'] == 'committed'
    assert corrected['draft_ref'] == first['draft_ref']
    assert len(engine['store'].entries) == 1
    assert len(engine['store'].commits) == 1


async def test_refresh_refuses_a_changed_weight_basis(engine):
    item = {'description': 'chicken breast', 'quantity': 100, 'unit': 'g', 'preparation': 'raw',
            'resolution': {'fingerprint': 'old-evidence'}}
    engine['foods']['raw chicken breast'] = dict(TUNA_COOKED)
    assert await engine['engine']._refresh(item) is None
    assert not engine['store'].entries


# --- review R1: typical recipes declare their basis from pinned rows, not labels ---

@pytest.mark.parametrize('description,source', [
    ('steak', None), ('ground beef', None), ('eggs', None), ('chicken', None),
    ('omelette', 'homemade'), ('salad with chicken', 'homemade'), ('breakfast burrito with bacon', 'homemade'),
])
async def test_raw_weight_never_uses_a_cooked_typical_recipe(engine, description, source):
    spec = dict(description=description, quantity=100, unit='g', preparation='raw')
    if source:
        spec['source'] = source
    result = await engine['engine'].handle({'add': [spec]})  # no provider answers: recipe is the only source
    assert result['status'] == 'needs_detail', result
    assert not engine['store'].entries
    assert 'weight basis' in result['question']


@pytest.mark.parametrize('description', ['steak', 'ground beef', 'eggs'])
async def test_cooked_weight_still_uses_the_cooked_typical_recipe(engine, description):
    result = await engine['engine'].handle({'add': [
        dict(description=description, quantity=100, unit='g', preparation='cooked')]})
    assert result['status'] == 'committed', result
    assert len(engine['store'].entries) == 1


async def test_as_served_recipe_has_no_weight_basis_to_conflict(engine):
    result = await engine['engine'].handle({'add': [
        dict(description='cheese', quantity=30, unit='g', preparation='raw')]})
    assert result['status'] == 'committed', result


def test_typical_recipes_declare_a_row_basis():
    import dish_estimates
    cooked = dish_estimates.estimate({'description': 'steak', 'quantity': 100, 'unit': 'g'})
    assert cooked['basis'] == 'cooked'
    assert all('basis' in part for part in cooked['built_from'])
    omelette = dish_estimates.estimate({'description': 'omelette with cheese', 'quantity': 100, 'unit': 'g'})
    assert omelette['basis'] == 'cooked'
    assert dish_estimates.estimate({'description': 'cheese', 'quantity': 30, 'unit': 'g'})['basis'] == 'as_served'


# --- review R2: an amendment never drops food the entry already counts ---

async def _logged_chicken_and_banana(engine):
    engine['foods']['raw chicken breast'] = dict(TUNA_RAW)
    first = await engine['engine'].handle({'add': [
        dict(description='chicken breast', quantity=100, unit='g', preparation='raw'),
        dict(description='banana', quantity=1)]}, turn_ref='t1')
    assert first['status'] == 'committed'
    return first


async def test_settling_a_weight_conflict_in_an_amendment_keeps_the_entry(engine):
    first = await _logged_chicken_and_banana(engine)
    before = deepcopy(engine['store'].entries)
    engine['foods']['cooked chicken breast'] = dict(TUNA_RAW)  # only a raw panel exists
    amended = await engine['engine'].handle({'amend_ref': first['draft_ref'],
        'edit': [{'item_ref': 'item1', 'preparation': 'cooked'}]}, turn_ref='t2')
    assert amended['status'] == 'needs_detail'
    assert engine['store'].entries == before
    settled = await engine['engine'].settle(amended['draft_ref'], turn_ref='t3')
    assert settled is not None and settled['status'] == 'failed', settled
    assert 'nothing changed' in settled['confirmation']
    assert engine['store'].entries == before
    assert len(engine['store'].commits) == 1
    retry = await engine['engine'].handle({'draft_ref': amended['draft_ref']}, turn_ref='t4')
    assert retry['status'] != 'amended'
    assert engine['store'].entries == before


async def test_log_now_on_an_amendment_cannot_drop_a_counted_food(engine):
    first = await _logged_chicken_and_banana(engine)
    before = deepcopy(engine['store'].entries)
    engine['foods']['cooked chicken breast'] = dict(TUNA_RAW)
    amended = await engine['engine'].handle({'amend_ref': first['draft_ref'],
        'edit': [{'item_ref': 'item1', 'preparation': 'cooked'}]}, turn_ref='t2')
    forced = await engine['engine'].handle({'draft_ref': amended['draft_ref'], 'log_now': True}, turn_ref='t3')
    assert forced['status'] == 'failed', forced
    assert engine['store'].entries == before


async def test_source_miss_on_an_amendment_cannot_drop_a_counted_food(engine):
    first = await _logged_chicken_and_banana(engine)
    before = deepcopy(engine['store'].entries)
    amended = await engine['engine'].handle({'amend_ref': first['draft_ref'],
        'edit': [{'item_ref': 'item1', 'description': 'unknown fish'}]}, turn_ref='t2')
    assert amended['status'] == 'failed', amended
    assert engine['store'].entries == before


async def test_user_skip_or_remove_still_drops_food_from_an_amended_entry(engine):
    first = await _logged_chicken_and_banana(engine)
    skipped = await engine['engine'].handle({'amend_ref': first['draft_ref'], 'skip': ['item1']}, turn_ref='t2')
    assert skipped['status'] == 'amended', skipped
    entry = next(iter(engine['store'].entries.values()))
    assert entry['name'] == '1 banana' and entry['entry_revision'] == 2
    removed = await engine['engine'].handle({'amend_ref': skipped['draft_ref'], 'remove': ['item1']}, turn_ref='t3')
    assert removed['status'] == 'amended', removed


async def test_corrected_amendment_updates_the_entry_once(engine):
    first = await _logged_chicken_and_banana(engine)
    engine['foods']['cooked chicken breast'] = dict(TUNA_COOKED)
    amended = await engine['engine'].handle({'amend_ref': first['draft_ref'],
        'edit': [{'item_ref': 'item1', 'preparation': 'cooked'}]}, turn_ref='t2')
    assert amended['status'] == 'amended', amended
    entry = next(iter(engine['store'].entries.values()))
    assert entry['entry_revision'] == 2 and 'cooked' in entry['name']
    assert len(engine['store'].commits) == 2


@pytest.mark.parametrize('basis', ['raw', 'uncooked', 'cooked', 'grilled'])
async def test_matching_weight_panel_still_logs(engine, basis):
    requested = 'raw' if basis in {'raw', 'uncooked'} else 'cooked'
    panel = dict(TUNA_RAW if requested == 'raw' else TUNA_COOKED, basis=basis)
    engine['foods'][f'{requested} chicken breast'] = panel
    result = await engine['engine'].handle({'add': [{
        'description': 'chicken breast', 'quantity': 100, 'unit': 'g', 'preparation': requested}]})
    assert result['status'] == 'committed', result
    assert len(engine['store'].entries) == 1
    assert next(iter(engine['store'].entries.values()))['calories'] == panel['calories']
