"""Adversarial source/identity invariants from the independent router review."""
import pytest

import food_lookup
import usda_lookup
from food_agent.contracts import FoodIdentity, Portion
from food_agent.find_food import FoodFinder
from test_food_router import _fdc, _fs
import food_agent_bench as bench


async def none(*args, **kwargs):
    return None


@pytest.mark.parametrize('kind', ['generic', 'homemade', 'other'])
async def test_nonbranded_miss_never_suggests_fatsecret(monkeypatch, kind):
    calls = []
    async def suggest(query):
        calls.append(query)
        return []
    monkeypatch.setattr(food_lookup, 'fatsecret_name_options', suggest)
    result = await FoodFinder(none).find(FoodIdentity(name='kumquat', kind=kind),
                                        Portion(kind='count', amount=1, text='1'))
    assert not calls
    assert result.status == 'no_match'


async def test_catalog_cannot_supply_fatsecret_to_generic(monkeypatch):
    async def catalog(query):
        return _fs('x', 'quinoa', 'Brand')
    monkeypatch.setattr(usda_lookup, 'lookup', none)
    monkeypatch.setattr(food_lookup.cofid_lookup, 'lookup', none)
    result = await food_lookup.resolve_food('quinoa', kind='generic', atomic=True,
                                           catalog_lookup=catalog, allow_web=False)
    assert result is None


async def test_production_local_memo_is_lane_scoped(monkeypatch):
    import server
    class Store:
        async def fetch_presets(self):
            return []
        async def lookup_catalog(self, query):
            return None
    monkeypatch.setattr(server, '_client', Store())
    monkeypatch.setattr(food_lookup.cofid_lookup, 'lookup', none)
    async def external(query, **kwargs):
        assert kwargs['kind'] == 'packaged'
        return _fs('x', 'Egg', 'Brand')
    monkeypatch.setattr(food_lookup, 'resolve_food', external)
    token = server._turn_presets.set({})
    try:
        generic = await server.resolve_food('egg', kind='generic', atomic=True)
        packaged = await server.resolve_food('egg', kind='packaged', atomic=True)
    finally:
        server._turn_presets.reset(token)
    assert generic['source'].startswith('Generic:')
    assert packaged['source'] == 'FatSecret: x'


async def test_explicit_other_only_calls_web(monkeypatch):
    calls = []
    async def web(query):
        calls.append(query)
        return {'source': 'WEB ESTIMATE', 'name': query}
    async def forbidden(*args, **kwargs):
        pytest.fail('other lane reached a structured provider')
    monkeypatch.setattr(food_lookup, 'search_fatsecret', forbidden)
    monkeypatch.setattr(food_lookup.cofid_lookup, 'lookup', forbidden)
    monkeypatch.setattr(usda_lookup, 'lookup', forbidden)
    monkeypatch.setattr(food_lookup.web_nutrition_lookup, 'lookup', web)
    await food_lookup.resolve_food('mystery dish', kind='other', atomic=True)
    assert calls == ['mystery dish']


@pytest.mark.parametrize('asked,variant,option', [
    ('vanilla yogurt', None, 'chocolate yogurt'),
    ('ground beef', '93/7', 'ground beef 80/20'),
    ('salmon', None, 'smoked salmon'),
])
async def test_suggestions_cannot_change_identity(asked, variant, option):
    async def resolve(query):
        return _fs('x', option, '') if query == option else None
    async def suggest(query):
        return [option]
    result = await FoodFinder(resolve, suggest=suggest).find(
        FoodIdentity(name=asked, variant=variant, kind='packaged'), Portion(kind='unspecified', text=''))
    assert result.status != 'matched'
    assert result.question is None


@pytest.mark.parametrize('query,description', [
    ('grilled chicken breast', 'Chicken breast, grilled with sauce, skin eaten'),
    ('egg', 'Egg, duck, whole, fresh, raw'),
    ('ground beef 93/7', 'Ground beef 80/20'),
    ('salmon', 'Smoked salmon'),
])
def test_usda_conflicting_single_candidate_is_not_selected(query, description):
    row = _fdc(123, 'Survey (FNDDS)', description, 200, 15, 20, 7, 2)
    assert usda_lookup.select(query, [row]) is None


async def test_usda_curated_tag_does_not_bypass_identity():
    row = _fdc(123, 'Survey (FNDDS)', 'Chicken breast, grilled with sauce, skin eaten', 200, 15, 20, 7, 2)
    async def resolve(query):
        return usda_lookup.to_result(row)
    result = await FoodFinder(resolve).find(
        FoodIdentity(name='chicken breast', preparation='grilled', kind='generic'),
        Portion(kind='mass', amount=100, unit='g', text='100 g'))
    assert result.status != 'matched'


@pytest.mark.parametrize('measures', [
    [('1 bagel thin', 45), ('1 regular bagel', 95)],
    [('1 regular bagel', 95), ('1 bagel thin', 45)],
])
async def test_bagel_default_never_depends_on_measure_order(measures):
    row = _fdc(123, 'Survey (FNDDS)', 'Bagel, plain', 200, 15, 20, 7, 2, measures)
    async def resolve(query):
        return usda_lookup.to_result(row)
    result = await FoodFinder(resolve).find(FoodIdentity(name='bagel', kind='generic'),
                                          Portion(kind='count', amount=1, text='1 bagel'))
    assert result.food is not None and result.food.serving_basis.grams == 95
    assert result.status == 'matched'


@pytest.mark.parametrize('gold,shown', [
    ({'name': 'chicken breast', 'preparation': 'raw'}, 'Chicken breast, cooked'),
    ({'name': 'ground beef', 'variant': '93/7'}, 'Ground beef 80/20'),
    ({'name': 'salmon'}, 'Smoked salmon'),
    ({'name': 'rice'}, 'Cooked rice'),
])
def test_scorer_cannot_erase_distinguishing_attributes(gold, shown):
    assert not bench.identity_matches(gold, shown)


@pytest.mark.parametrize('measures', [
    [('1 bagel thin', 45)],
    [('1 bagel', 80), ('1 bagel', 95)],
])
async def test_unsupported_usda_count_asks_amount_not_guessing(measures):
    row = _fdc(123, 'Survey (FNDDS)', 'Bagel, plain', 200, 15, 20, 7, 2, measures)
    async def resolve(query):
        return usda_lookup.to_result(row)
    result = await FoodFinder(resolve).find(FoodIdentity(name='bagel', kind='generic'),
                                          Portion(kind='count', amount=1, text='1 bagel'))
    assert result.status == 'needs_detail'
    assert result.food.serving_basis.basis == 'per_100g'


async def test_explicit_measure_is_selected_and_mass_is_not_double_scaled():
    row = _fdc(123, 'Survey (FNDDS)', 'Bagel, plain', 200, 15, 20, 7, 2,
               [('1 bagel thin', 45), ('1 regular bagel', 95)])
    async def resolve(query):
        return usda_lookup.to_result(row)
    finder = FoodFinder(resolve)
    ident = FoodIdentity(name='bagel', kind='generic')
    thin = await finder.find(ident, Portion(kind='count', amount=1, text='1 bagel thin'))
    assert thin.status == 'matched' and thin.food.serving_basis.grams == 45
    mass = await finder.find(ident, Portion(kind='mass', amount=50, unit='g', text='50 g'))
    assert mass.status == 'matched' and mass.food.serving_basis.basis == 'per_100g'
    assert mass.food.panel.calories == 200


def test_scorer_alias_cannot_override_explicit_ratio():
    gold = {'name': 'ground beef', 'variant': '93/7', 'accepted_names': ['ground beef 80/20']}
    assert not bench.identity_matches(gold, 'ground beef 80/20')


def test_scorer_unspecified_serving_is_not_one_almond():
    saved = {'multiplier': 1, 'serving_basis': {'unit': 'count', 'amount': 1,
             'count_noun': 'serving', 'text': '1 serving (28 g)', 'grams': 28}}
    gold = {'portion': {'kind': 'count', 'amount': 1, 'unit': 'almond'}}
    assert bench.consumed_matches(gold, saved) is None
