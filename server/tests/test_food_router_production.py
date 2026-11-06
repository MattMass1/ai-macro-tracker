"""Production-path source routing and spoken portions: review regressions."""
import asyncio

import pytest

import food_lookup
import usda_lookup
from test_food_router import _fdc, _fs  # supplies synthetic app configuration
import server
from food_agent.contracts import FoodIdentity, Portion
from food_agent.find_food import FoodFinder, ROUTE_KIND
from food_agent.portion import multiplier


async def none(*args, **kwargs):
    return None


class Store:
    async def fetch_presets(self):
        return []

    async def lookup_catalog(self, query):
        return None


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(server, '_client', Store())
    monkeypatch.setattr(food_lookup, '_fatsecret_provider_mode', lambda: 'oauth1')
    monkeypatch.setattr(food_lookup, 'search_fatsecret', none)
    monkeypatch.setattr(food_lookup.cofid_lookup, 'lookup', none)
    monkeypatch.setattr(usda_lookup, 'lookup', none)
    monkeypatch.setattr(food_lookup.web_nutrition_lookup, 'lookup', none)

    async def no_options(query):
        return []
    monkeypatch.setattr(food_lookup, 'fatsecret_name_options', no_options)
    server._resolved_food_cache.clear()


@pytest.mark.parametrize('kind', ['packaged', 'restaurant'])
async def test_branded_miss_cannot_fall_back_to_generic(kind):
    result = await FoodFinder().find(FoodIdentity(name='grilled chicken breast', kind=kind),
                                    Portion(kind='mass', amount=100, unit='g', text='100 g'))
    assert result.status == 'no_match', result


@pytest.mark.parametrize('text,kind,amount', [
    ('1 bagel', 'count', 1), ('one bagel', 'count', 1),
    ('half a bagel', 'serving_fraction', 0.5), ('two bagels', 'count', 2),
    ('1/2 bagel', 'count', 0.5), ('three bagels', 'count', 3),
    ('quarter of a bagel', 'serving_fraction', 0.25),
    ('thirteen bagels', 'count', 13), ('twenty one bagels', 'count', 21),
    ('one and a half bagels', 'count', 1.5),
])
async def test_spoken_amount_keeps_regular_measure(monkeypatch, text, kind, amount):
    row = _fdc(123, 'Survey (FNDDS)', 'Bagel, plain', 200, 15, 20, 7, 2,
               [('1 bagel thin', 45), ('1 regular bagel', 95)])

    async def usda(query):
        selected = usda_lookup.select(query, [row])
        return usda_lookup.to_result(selected) if selected else None
    monkeypatch.setattr(usda_lookup, 'lookup', usda)
    identity = FoodIdentity(name='bagel', kind='generic')
    portion = Portion(kind=kind, amount=amount, text=text)
    result = await FoodFinder().find(identity, portion)
    assert result.status == 'matched', result
    assert result.food is not None and result.food.serving_basis.grams == 95
    assert float(multiplier(portion, result.food.serving_basis, identity)) == amount


@pytest.mark.parametrize('kind', ['generic', 'homemade', 'other'])
async def test_nonbranded_catalog_and_suggestions_never_use_fatsecret(kind, monkeypatch):
    class Catalog(Store):
        async def lookup_catalog(self, query):
            return _fs('x', 'quinoa', '')
    monkeypatch.setattr(server, '_client', Catalog())

    async def forbidden(*args, **kwargs):
        pytest.fail('FatSecret reached from wrong lane')
    monkeypatch.setattr(food_lookup, 'search_fatsecret', forbidden)
    monkeypatch.setattr(food_lookup, 'fatsecret_name_options', forbidden)
    result = await FoodFinder().find(FoodIdentity(name='quinoa', kind=kind),
                                    Portion(kind='mass', amount=100, unit='g', text='100 g'))
    assert result.status == 'no_match'


@pytest.mark.parametrize('kind', ['restaurant', 'packaged'])
async def test_generic_catalog_cannot_enter_branded_lane(kind, monkeypatch):
    class Catalog(Store):
        async def lookup_catalog(self, query):
            return food_lookup.resolve_generic_whole_food('egg')
    monkeypatch.setattr(server, '_client', Catalog())
    result = await FoodFinder().find(FoodIdentity(name='egg', kind=kind),
                                    Portion(kind='count', amount=1, text='1'))
    assert result.status == 'no_match'


async def test_lanes_remain_task_local_and_reset_after_options():
    async def resolve(query):
        expected = 'packaged' if query == 'yogurt' else 'generic'
        assert ROUTE_KIND.get() == expected
        await asyncio.sleep(0)
        assert ROUTE_KIND.get() == expected
        return None

    async def suggest(query):
        assert ROUTE_KIND.get() == 'packaged'
        return []
    finder = FoodFinder(resolve, suggest=suggest)
    await asyncio.gather(*[
        finder.find(FoodIdentity(name=name, kind=kind), Portion(kind='unspecified', text=''))
        for name, kind in [('yogurt', 'packaged'), ('kumquat', 'generic')]
    ])
    assert ROUTE_KIND.get() is None


@pytest.mark.parametrize('kind', ['generic', 'homemade', 'other'])
async def test_composite_whole_phrase_retry_keeps_the_lane(kind, monkeypatch):
    # Review R3: the retry after unresolved parts must carry `kind`, never
    # re-enter the omitted-kind legacy cascade (FatSecret for a whole food).
    async def forbidden(query, **kwargs):
        pytest.fail(f'FatSecret reached from the {kind} lane: {query!r}')
    monkeypatch.setattr(food_lookup, 'search_fatsecret', forbidden)
    result = await food_lookup.resolve_food('kumquat and persimmon', kind=kind, allow_web=False)
    assert result is None


@pytest.mark.parametrize('kind', ['packaged', 'restaurant', None])
async def test_composite_whole_phrase_retry_reaches_branded_or_legacy_sources(kind, monkeypatch):
    calls = []

    async def fatsecret(query, **kwargs):
        calls.append(query)
        return None
    monkeypatch.setattr(food_lookup, 'search_fatsecret', fatsecret)
    await food_lookup.resolve_food('kumquat and persimmon', kind=kind, allow_web=False)
    assert 'kumquat and persimmon' in calls


@pytest.mark.parametrize('query,description', [
    ('egg', 'Egg, goose, raw'), ('salmon', 'Salmon, canned'),
    ('chicken breast', 'Chicken breast, skin eaten'),
    ('chicken breast raw', 'Chicken breast grilled'),
    ('ground beef 93/7', 'Ground beef 85/15'),
    ('vanilla yogurt', 'Chocolate yogurt'),
    ('salmon', 'Salmon in sauce'),
])
def test_additional_usda_conflicts_fail_closed(query, description):
    row = _fdc(123, 'SR Legacy', description, 200, 15, 20, 7, 2)
    assert usda_lookup.select(query, [row]) is None
