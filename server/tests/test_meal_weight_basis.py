"""A rejected weight basis must not be replaced by an incompatible estimate."""
import os

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
