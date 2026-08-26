"""Deterministic official restaurant menu lookup tests."""
import pytest

import food_lookup
from restaurant_menu import RESTAURANT_MENU, restaurant_lookup


@pytest.mark.parametrize(("query", "name"), [
    ("chick fil a egg white grill", "Egg White Grill"),
    ("chipotle double chicken bowl", "Double Chicken Burrito Bowl"),
    ("raising cane's 3 finger combo", "3 Finger Combo Without Drink"),
    ("mcdonald's 10 piece mcnuggets", "Chicken McNuggets 10 Piece"),
])
async def test_curated_restaurant_queries_match_exact_table(query, name):
    expected = next(item for items in RESTAURANT_MENU.values()
                    for item in items if item["name"] == name)
    result = await food_lookup.resolve_food(query)
    assert result is not None
    assert result["name"] == name
    assert result["macros_per_serving"] == {
        key: float(expected[key])
        for key in ("calories", "protein", "carbs", "fat", "fiber")
    }
    assert result["source"].startswith("Restaurant menu: https://")


def test_multi_item_order_aggregates_each_curated_match():
    result = restaurant_lookup(
        "chick fil a grilled club + 8ct grilled nuggets + honey mustard packet"
    )
    assert result is not None
    assert [item["name"] for item in result["matched_items"]] == [
        "Grilled Chicken Club Sandwich", "Grilled Nuggets 8 Count",
        "Honey Mustard Sauce",
    ]
    assert result["macros_per_serving"] == {
        "calories": 700.0, "protein": 63.0, "carbs": 57.0,
        "fat": 25.0, "fiber": 3.0,
    }


def test_chain_name_is_required_for_generic_items():
    assert restaurant_lookup("medium fries") is None
