"""Curated restaurant rows: retained as future Layer 1 cache seeds only.

Spec 2026-10-09 removed this module from the resolution chain (its chain-alias
matching cross-contaminated brands); `food_lookup` never consults it. The
hand-verified table and its deterministic matcher stay testable on their own.
"""
from restaurant_menu import restaurant_lookup


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
