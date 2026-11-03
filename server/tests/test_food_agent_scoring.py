"""Independent synthetic gold oracles; never import production portion math."""
import copy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import food_agent_bench as bench
import food_agent_bench_live as live
from test_food_agent_bench_live import _qp_case, _qp_run


def test_live_gold_count_uses_actual_two_item_serving_not_scripted_multiplier():
    case, script = _qp_case()
    run = _qp_run('Quarter Pounder with Cheese', .5)
    item = run.entries[0]['component_metadata']['items'][0]
    item['serving_basis'] = {'text':'2 sandwiches', 'unit':'count', 'amount':2., 'count_noun':'sandwich'}
    score = live.score_live(case,script,run,{'FatSecret:4242':"McDonald's"})
    assert score['success'] and score['portion'] == {'n':1,'d':1}


def test_live_gold_mass_uses_consumed_grams_not_scripted_multiplier():
    gold = {'portion':{'kind':'mass','amount':200.,'unit':'g'}}
    first = {'multiplier':2., 'serving_basis':{'grams':100.}}
    second = {'multiplier':.5, 'serving_basis':{'grams':400.}}
    assert bench.consumed_matches(gold,first) is True
    assert bench.consumed_matches(gold,second) is True
    assert bench.consumed_matches(gold,{**second,'multiplier':2.}) is False


def test_live_volume_does_not_use_the_scripted_source_density():
    gold = {'portion':{'kind':'volume','amount':6.,'unit':'fl oz'}}
    assert bench.consumed_matches(gold,{'multiplier':.5,'serving_basis':{'unit':'fl_oz','amount':12.,'grams':900.}})


def test_clarification_requires_meaning_not_just_question_mark():
    case = {'id':'synthetic-ask','expected_action':'clarify','gold_items':[],
            'expected_question':['burger', ['brand','restaurant']], 'old_app':{}}
    def score(reply, status='needs_detail'):
        return bench.score_case(case,{},bench.CaseRun(case_id='synthetic-ask',turns=[
            bench.TurnRecord(idx=0,user='burger',reply=reply,is_question=True,status=status)]))
    assert score('Which restaurant made the burger?')['success']
    assert not score('How many eggs?')['success']
    assert not score('Which restaurant made the burger?', 'error')['success']


def test_latest_delivered_turn_not_any_previous_preview_authorizes_score():
    case, script = _qp_case()
    run = _qp_run('Quarter Pounder with Cheese',1.)
    run.turns.insert(1,bench.TurnRecord(idx=1,user='wait',reply='Which brand?',is_question=True))
    run.turns[2].idx = 2
    score = live.score_live(case,script,run,{'FatSecret:4242':"McDonald's"})
    assert not score['success'] and score['counters']['stale_version_commits'] == 1


def test_gold_missing_nutrition_is_not_nutrition_success():
    case, script = _qp_case()
    del case['expected_nutrients']
    score = live.score_live(case,script,_qp_run('Quarter Pounder with Cheese',1.),{'FatSecret:4242':"McDonald's"})
    assert not score['success'] and any('not verified' in r for r in score['reasons'])
