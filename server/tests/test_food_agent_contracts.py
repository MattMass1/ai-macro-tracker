"""WP2: typed contracts and pure portion math for the food agent v1.

Offline only: no network, no database. These tests pin the model-output
contract (what the model may say), the draft-side types later work packages
build on, and the portion arithmetic behind the three real failures:

1. "ahi tuna bowl again" must be a recall action, never a copied entry.
2. "Half a quarter pounder" then "with cheese": portion 0.5 survives an
   identity revision and is applied exactly once.
3. "6 oz instead of 12" against a 12 fl oz smoothie: multiplier 0.5 on the
   same product.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from food_agent import contracts as c
from food_agent import portion as p


def _turn(*actions: dict) -> c.AgentTurn:
    return c.AgentTurn.model_validate_json(
        json.dumps({"schema_version": c.SCHEMA_VERSION, "actions": list(actions)}))


def _identity(**over) -> dict:
    base = {"name": "Quarter Pounder", "brand": "McDonald's", "variant": None,
            "kind": "restaurant", "preparation": None}
    base.update(over)
    return base


def _portion(**over) -> dict:
    base = {"kind": "serving_fraction", "amount": 0.5, "unit": None, "text": "half of a"}
    base.update(over)
    return base


def _item(**over) -> dict:
    base = {"item_ref": None, "identity": _identity(), "portion": _portion(), "modifications": []}
    base.update(over)
    return base


# ---------------------------------------------------------------- model output


def test_add_items_parses_with_separate_identity_portion_and_mods():
    turn = _turn({"type": "add_items", "items": [_item(modifications=[{"op": "remove", "text": "pickles"}])]})
    action = turn.actions[0]
    assert isinstance(action, c.AddItems)
    item = action.items[0]
    assert item.identity.name == "Quarter Pounder" and item.identity.kind == "restaurant"
    assert item.portion.kind == "serving_fraction" and item.portion.amount == 0.5
    assert item.portion.text == "half of a"
    assert item.modifications[0].op == "remove"


def test_confirm_with_model_supplied_confirmed_flag_is_rejected():
    with pytest.raises(ValidationError):
        _turn({"type": "confirm", "draft_ref": "d1", "shown_revision": 3, "confirmed": True})
    # The bare confirm carries no authority field at all.
    ok = _turn({"type": "confirm", "draft_ref": "d1", "shown_revision": 3})
    assert set(type(ok.actions[0]).model_fields) == {"type", "draft_ref", "shown_revision"}


@pytest.mark.parametrize("field,value", [
    ("calories", 520), ("protein", 30), ("macros_per_serving", {"calories": 520}),
    ("nutrients", {"protein": 30}), ("user_id", "be6333cc-e4c1-48f2-adb1-5e7f14dbf7c2"),
    ("sql", "DELETE FROM nutrition_entries"),
])
def test_nutrients_user_ids_and_sql_are_rejected_in_item_specs(field, value):
    with pytest.raises(ValidationError):
        _turn({"type": "add_items", "items": [{**_item(), field: value}]})


def test_extra_fields_rejected_on_envelope_identity_and_portion():
    with pytest.raises(ValidationError):
        c.AgentTurn.model_validate_json(json.dumps(
            {"schema_version": c.SCHEMA_VERSION, "actions": [{"type": "cancel"}], "user_id": "x"}))
    with pytest.raises(ValidationError):
        _turn({"type": "add_items", "items": [_item(identity=_identity(calories=520))]})
    with pytest.raises(ValidationError):
        _turn({"type": "add_items", "items": [_item(portion=_portion(grams=100))]})


def test_unknown_action_type_and_wrong_schema_version_rejected():
    with pytest.raises(ValidationError):
        _turn({"type": "commit"})
    with pytest.raises(ValidationError):
        c.AgentTurn.model_validate_json(json.dumps({"schema_version": "food-agent/0",
                                                     "actions": [{"type": "cancel"}]}))


def test_action_count_is_bounded():
    with pytest.raises(ValidationError):
        _turn(*[{"type": "ask", "question": f"q{i}?"} for i in range(c.MAX_ACTIONS + 1)])
    with pytest.raises(ValidationError):
        c.AgentTurn.model_validate_json(json.dumps({"schema_version": c.SCHEMA_VERSION, "actions": []}))


def test_revise_item_omitted_portion_means_preserve():
    """Real failure 2: 'quarter pounder with cheese' revises identity only."""
    turn = _turn({"type": "revise_item", "item_ref": "i1",
                  "identity": {"name": "Quarter Pounder with Cheese", "brand": None, "variant": None,
                               "kind": None, "preparation": None},
                  "clear_fields": [], "portion": None, "modifications": None})
    action = turn.actions[0]
    assert isinstance(action, c.ReviseItem)
    assert action.portion is None and action.modifications is None
    original = c.FoodIdentity.model_validate_json(json.dumps(_identity()))
    revised = action.identity.apply_to(original)
    assert revised.name == "Quarter Pounder with Cheese"
    assert revised.brand == "McDonald's" and revised.kind == "restaurant"

    # Omitting the keys entirely is the same as null: preserve.
    terse = _turn({"type": "revise_item", "item_ref": "i1",
                   "identity": {"name": "Quarter Pounder with Cheese"}})
    assert terse.actions[0].portion is None and terse.actions[0].modifications is None


def test_revise_item_clear_fields_and_empty_revision():
    turn = _turn({"type": "revise_item", "item_ref": "i1",
                  "identity": {"kind": "homemade"}, "clear_fields": ["brand"]})
    original = c.FoodIdentity.model_validate_json(json.dumps(_identity()))
    revised = turn.actions[0].identity.apply_to(original, turn.actions[0].clear_fields)
    assert revised.brand is None and revised.kind == "homemade"
    with pytest.raises(ValidationError):
        _turn({"type": "revise_item", "item_ref": "i1"})


def test_add_items_rejects_existing_item_ref():
    with pytest.raises(ValidationError):
        _turn({"type": "add_items", "items": [_item(item_ref="i1")]})


def test_portion_amounts_positive_finite_and_unit_consistent():
    bad = [
        _portion(amount=0), _portion(amount=-1), _portion(amount=None),
        _portion(kind="mass", amount=6, unit="fl_oz"),     # fl_oz is volume
        _portion(kind="volume", amount=6, unit="g"),
        _portion(kind="mass", amount=6, unit=None),
        _portion(kind="unspecified", amount=1, unit=None),
        _portion(kind="serving_fraction", amount=0.5, unit="g"),
    ]
    for portion in bad:
        with pytest.raises(ValidationError):
            c.Portion.model_validate_json(json.dumps(portion))
    for raw in ('{"kind":"mass","amount":1e999,"unit":"g","text":"x"}',
                '{"kind":"mass","amount":NaN,"unit":"g","text":"x"}'):
        with pytest.raises(ValidationError):
            c.Portion.model_validate_json(raw)


def test_recall_ask_not_food_cancel_parse():
    turn = _turn({"type": "recall_meal", "query": "ahi tuna bowl", "when_hint": "last time"})
    assert isinstance(turn.actions[0], c.RecallMeal)
    assert isinstance(_turn({"type": "ask", "question": "Where was it from?"}).actions[0], c.Ask)
    assert isinstance(_turn({"type": "not_food"}).actions[0], c.NotFood)
    assert isinstance(_turn({"type": "cancel"}).actions[0], c.Cancel)


def test_confirm_cannot_ride_with_edits_and_not_food_is_alone():
    with pytest.raises(ValidationError):
        _turn({"type": "add_items", "items": [_item()]},
              {"type": "confirm", "draft_ref": "d1", "shown_revision": 2})
    with pytest.raises(ValidationError):
        _turn({"type": "not_food"}, {"type": "ask", "question": "?"})


# ---------------------------------------------------------------- JSON schema


def _walk(node, path="$"):
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")


def test_agent_turn_json_schema_is_openai_strict_compatible():
    schema = c.agent_turn_json_schema()
    json.dumps(schema)  # serializable
    assert schema["type"] == "object"
    assert "anyOf" not in schema and "oneOf" not in schema
    assert schema["additionalProperties"] is False
    forbidden = {"oneOf", "allOf", "discriminator", "const", "default", "not",
                 "if", "then", "else", "patternProperties"}
    objects = 0
    for path, node in _walk(schema):
        if not isinstance(node, dict):
            continue
        # 'properties' maps field names to schemas; a field may be named e.g. "default".
        if path.endswith(".properties"):
            continue
        assert not (forbidden & set(node)), (path, forbidden & set(node))
        if node.get("type") == "object" or "properties" in node:
            objects += 1
            assert node.get("additionalProperties") is False, path
            assert sorted(node.get("required", [])) == sorted(node["properties"]), path
    assert objects >= 10
    # Every $ref resolves into $defs.
    defs = schema.get("$defs", {})
    for path, node in _walk(schema):
        if isinstance(node, dict) and "$ref" in node:
            assert node["$ref"].startswith("#/$defs/") and node["$ref"].split("/")[-1] in defs, path
    # The action union is a nested anyOf over all eight action types.
    actions = schema["properties"]["actions"]
    assert actions["type"] == "array" and actions["maxItems"] == c.MAX_ACTIONS
    variants = actions["items"]["anyOf"]
    names = set()
    for ref in variants:
        target = defs[ref["$ref"].split("/")[-1]]
        names.update(target["properties"]["type"]["enum"])
    assert names == {"add_items", "revise_item", "remove_item", "recall_meal",
                     "confirm", "cancel", "ask", "not_food"}
    # No nutrient or authority field exists anywhere in the model-facing schema.
    text = json.dumps(schema)
    for word in ('"calories"', '"protein"', '"confirmed"', '"user_id"'):
        assert word not in text


# ---------------------------------------------------------------- portion math


SANDWICH = p.parse_serving_text("1 sandwich (202 g)")
SMOOTHIE = p.parse_serving_text("12 fl oz")
EGG = p.parse_serving_text("1 large egg (50 g)")
PER_100G = c.ServingBasis(text="100 g", basis="per_100g", amount=100.0, unit="g")


def _p(kind, amount=None, unit=None, text="x") -> c.Portion:
    return c.Portion(kind=kind, amount=amount, unit=unit, text=text)


def test_parse_serving_text_shapes():
    assert SANDWICH.unit == "count" and SANDWICH.amount == 1 and SANDWICH.grams == 202
    assert SANDWICH.count_noun == "sandwich" and SANDWICH.basis == "per_serving"
    assert SMOOTHIE.unit == "fl_oz" and SMOOTHIE.amount == 12
    oz = p.parse_serving_text("12 oz")
    assert oz.unit == "oz" and oz.amount == 12
    cup = p.parse_serving_text("1 cup (240 ml)")
    assert cup.unit == "cup" and cup.ml == 240
    assert p.parse_serving_text("1/2 cup").amount == 0.5
    blank = p.parse_serving_text("")
    assert blank.amount is None and blank.unit is None


def test_half_sandwich_halves_the_panel():
    """Real failure 2: half of a 520 kcal Quarter Pounder panel."""
    m = p.multiplier(_p("serving_fraction", 0.5), SANDWICH)
    assert m == Decimal("0.5")
    panel = c.NutrientPanel(calories=520, protein=30, carbs=42, fat=26, fiber=None)
    scaled = p.scale_panel(panel, m)
    assert scaled.calories == 260 and scaled.protein == 15 and scaled.carbs == 21 and scaled.fat == 13
    assert scaled.fiber is None


def test_six_oz_against_twelve_fl_oz_serving_is_half():
    """Real failure 3: bare 'oz' against a fluid-ounce serving is fluid ounces."""
    assert p.multiplier(_p("mass", 6, "oz", "6 oz"), SMOOTHIE) == Decimal("0.5")
    assert p.multiplier(_p("volume", 6, "fl_oz"), SMOOTHIE) == Decimal("0.5")
    # Mirror: the source says bare "12 oz", the user says "6 fl oz".
    assert p.multiplier(_p("volume", 6, "fl_oz"), p.parse_serving_text("12 oz")) == Decimal("0.5")
    assert p.multiplier(_p("mass", 6, "oz"), p.parse_serving_text("12 oz")) == Decimal("0.5")


def test_oz_against_gram_basis_is_mass():
    m = p.multiplier(_p("mass", 4, "oz"), PER_100G)
    assert m == Decimal("1.134")  # 113.398 g / 100 g, 4 dp
    assert p.multiplier(_p("mass", 150, "g"), PER_100G) == Decimal("1.5")
    assert p.multiplier(_p("mass", 101, "g"), SANDWICH) == Decimal("0.5")


def test_count_two_eggs_is_two():
    assert p.multiplier(_p("count", 2), EGG) == Decimal("2")
    assert p.multiplier(_p("count", 6, text="6 cookies"), p.parse_serving_text("3 cookies (28 g)")) == Decimal("2")


def test_review_s4_serving_fraction_needs_a_defined_serving():
    """Review S4 / probe Q1: 'half' of a per-100 g panel is not 50 g; ask instead."""
    assert p.multiplier(_p("serving_fraction", 0.5, text="half a chicken breast"), PER_100G) is None
    assert p.multiplier(_p("serving_fraction", 0.5), p.basis_per_100ml()) is None
    # Against the source's own serving a fraction still always works.
    assert p.multiplier(_p("serving_fraction", 0.5), SANDWICH) == Decimal("0.5")
    assert p.multiplier(_p("serving_fraction", 0.5), SMOOTHIE) == Decimal("0.5")


def test_review_s4_count_against_multi_piece_serving_needs_matching_noun():
    """Review S4 / probe Q1: '1' against a '10 pieces' serving is not silently 0.1."""
    ten = p.parse_serving_text("10 pieces")
    assert p.multiplier(_p("count", 1, text="1"), ten) is None
    assert p.multiplier(_p("count", 1, text="1 bar"), ten) is None
    assert p.multiplier(_p("count", 1, text="one box"), ten) is None
    # The user counted in the serving's own unit: pieces of 10.
    assert p.multiplier(_p("count", 1, text="1 piece"), ten) == Decimal("0.1")
    assert p.multiplier(_p("count", 5, text="5 pieces"), ten) == Decimal("0.5")
    nuggets = p.parse_serving_text("10 nuggets (160 g)")
    assert p.multiplier(_p("count", 4, text="4 chicken nuggets"), nuggets) == Decimal("0.4")
    assert p.multiplier(_p("count", 2, text="2 boxes"), p.parse_serving_text("3 boxes")) == Decimal("0.6667")
    # A one-unit serving keeps the simple rule: n units = n servings.
    assert p.multiplier(_p("count", 2, text="2"), EGG) == Decimal("2")
    assert p.multiplier(_p("count", 2, text="2 bars"), p.parse_serving_text("1 bar (40 g)")) == Decimal("2")
    # A serving with no count unit: the count is servings.
    assert p.multiplier(_p("count", 2, text="2 smoothies"), SMOOTHIE) == Decimal("2")


def _ident(name: str, variant: str | None = None, kind: str = "generic") -> c.FoodIdentity:
    return c.FoodIdentity(name=name, brand=None, variant=variant, kind=kind, preparation=None)


def test_review_r2_count_unit_comes_from_identity_and_known_equivalents():
    """Review re-check R-2: the model often puts only the number in portion.text.
    The unit then comes from the food's name, and nuggets are pieces."""
    two_eggs = p.parse_serving_text("2 large eggs (100 g)")
    assert p.multiplier(_p("count", 2, text="2"), two_eggs, _ident("eggs")) == Decimal("1")
    assert p.multiplier(_p("count", 3, text="3"), two_eggs, _ident("scrambled egg")) == Decimal("1.5")
    slices = p.parse_serving_text("2 slices (16 g)")
    assert p.multiplier(_p("count", 3, text="three"), slices, _ident("bacon")) == Decimal("1.5")
    assert p.multiplier(_p("count", 3, text="three"), slices, _ident("bacon slices")) == Decimal("1.5")
    pieces = p.parse_serving_text("4 pieces (64 g)")
    nuggets = _ident("Chicken McNuggets", kind="restaurant")
    assert p.multiplier(_p("count", 6, text="6 nuggets"), pieces, nuggets) == Decimal("1.5")
    assert p.multiplier(_p("count", 6, text="6 nuggets"), pieces) == Decimal("1.5")
    assert p.multiplier(_p("count", 6, text="6"), pieces, nuggets) == Decimal("1.5")
    # The existing two-argument call keeps working.
    assert p.multiplier(_p("count", 2, text="2 eggs"), two_eggs) == Decimal("1")


def test_review_r2_still_asks_when_the_unit_really_differs_or_is_unknown():
    ten = p.parse_serving_text("10 pieces")
    nuggets = _ident("Chicken McNuggets", kind="restaurant")
    assert p.multiplier(_p("count", 1, text="1 box"), ten, nuggets) is None
    assert p.multiplier(_p("count", 1, text="1 order"), ten, nuggets) is None
    assert p.multiplier(_p("count", 1, text="1 bar"), ten, _ident("granola bites")) is None
    assert p.multiplier(_p("count", 1, text="1"), ten) is None
    # "1" McNugget from the name alone is more likely one box: ask.
    assert p.multiplier(_p("count", 1, text="1"), ten, nuggets) is None
    # A name that states the box size ("10 piece") is the box, not the unit.
    assert p.multiplier(_p("count", 2, text="2"), ten, _ident("Chicken McNuggets", variant="10 piece")) is None
    assert p.multiplier(_p("count", 2, text="2"), ten, _ident("10 piece Chicken McNuggets")) is None
    # Nothing links the food to slices.
    assert p.multiplier(_p("count", 3, text="three"), p.parse_serving_text("2 slices"), _ident("apple")) is None


def test_mass_vs_volume_without_density_is_none():
    assert p.multiplier(_p("mass", 100, "g"), p.parse_serving_text("12 fl oz")) is None
    assert p.multiplier(_p("volume", 1, "cup"), p.parse_serving_text("12 oz")) is None
    assert p.multiplier(_p("volume", 1, "cup"), PER_100G) is None
    # Volume against a mass basis that also states ml uses the ml.
    assert p.multiplier(_p("volume", 120, "ml"), p.parse_serving_text("1 cup (240 ml)")) == Decimal("0.5")


def test_unspecified_portion_needs_a_defined_single_serving():
    assert p.multiplier(_p("unspecified"), SANDWICH) == Decimal("1")
    assert p.multiplier(_p("unspecified"), PER_100G) is None
    assert p.multiplier(_p("count", 2), PER_100G) is None


def test_none_nutrient_stays_none_after_scaling():
    panel = c.NutrientPanel(calories=None, protein=None, carbs=10, fat=None, fiber=None)
    scaled = p.scale_panel(panel, Decimal("2"))
    assert scaled.calories is None and scaled.protein is None and scaled.fat is None
    assert scaled.carbs == 20
    assert p.scale_panel(panel, Decimal("0.3333")).carbs == 3.3


def test_scale_panel_rejects_non_positive_multiplier():
    panel = c.NutrientPanel(calories=100, protein=10, carbs=None, fat=None, fiber=None)
    for bad in (Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            p.scale_panel(panel, bad)


# ---------------------------------------------------------------- draft side


def _draft_item(portion_amount=0.5, **resolved_over) -> c.DraftItemState:
    resolved = {"food_id": "fatsecret:123", "provider": "fatsecret", "external_id": "123",
                "display_name": "McDonald's Quarter Pounder with Cheese", "macro_source": "FatSecret: 123",
                "serving_basis": {"text": "1 sandwich (202 g)", "basis": "per_serving", "amount": 1.0,
                                  "unit": "count", "count_noun": "sandwich", "grams": 202.0, "ml": None},
                "panel": {"calories": 520, "protein": 30, "carbs": 42, "fat": 26, "fiber": None}}
    resolved.update(resolved_over)
    return c.DraftItemState.model_validate_json(json.dumps({
        "item_ref": "i1", "identity": _identity(name="Quarter Pounder with Cheese"),
        "portion": _portion(amount=portion_amount), "modifications": [{"op": "remove", "text": "pickles"}],
        "resolved": resolved, "multiplier": portion_amount}))


def test_draft_item_scaled_panel_applies_multiplier_once():
    item = _draft_item()
    scaled = item.scaled_panel()
    assert scaled.calories == 260 and scaled.protein == 15 and scaled.fiber is None
    # The stored per-serving panel is never mutated.
    assert item.resolved.panel.calories == 520


def test_content_hash_stable_under_key_order_and_sensitive_to_portion():
    item = _draft_item()
    reordered = c.DraftItemState.model_validate_json(json.dumps(
        dict(reversed(list(json.loads(item.model_dump_json()).items())))))
    assert c.content_hash([item]) == c.content_hash([reordered])
    assert len(c.content_hash([item])) == 64
    assert c.content_hash([item]) != c.content_hash([_draft_item(portion_amount=1.0)])
    assert c.content_hash([item]) != c.content_hash([_draft_item(food_id="fatsecret:999")])
    # Plain dicts hash the same as models, regardless of key order.
    as_dict = json.loads(item.model_dump_json())
    assert c.content_hash([as_dict]) == c.content_hash([item])
    assert c.content_hash([dict(sorted(as_dict.items(), reverse=True))]) == c.content_hash([item])


def test_pending_confirm_round_trip_fits_pending_question():
    pending = c.PendingConfirm(draft_id="0b5f1f8e-3c1e-4d55-9a8a-2f4b1c9d7e10", revision=7,
                               content_hash=c.content_hash([_draft_item()]))
    text = pending.to_pending_question()
    assert len(text) <= c.PENDING_QUESTION_MAX
    assert c.PendingConfirm.from_pending_question(text) == pending
    assert c.PendingConfirm.from_pending_question("How many ounces?") is None
    assert c.PendingConfirm.from_pending_question(None) is None
    with pytest.raises(ValidationError):
        c.PendingConfirm(draft_id="x", revision=0, content_hash="abc")


def test_preview_and_receipt_keep_unknown_nutrients_unknown():
    item = _draft_item()
    preview = c.Preview(draft_id="0b5f1f8e-3c1e-4d55-9a8a-2f4b1c9d7e10", revision=3,
                        content_hash=c.content_hash([item]),
                        lines=[c.PreviewLine(item_ref="i1", display_name=item.resolved.display_name,
                                             portion_text="half", nutrients=item.scaled_panel())],
                        totals=c.sum_panels([item.scaled_panel()]),
                        created_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert preview.totals.calories == 260 and preview.totals.fiber is None
    assert c.Preview.model_validate_json(preview.model_dump_json()) == preview

    row = {"id": "e1", "entry_revision": 1, "name": "Quarter Pounder with Cheese (half)",
           "calories": Decimal("260"), "protein": Decimal("15.0"), "carbs": 21.0, "fat": 13, "fiber": None,
           "macro_source": "FatSecret: 123"}
    receipt = c.Receipt.from_saved_row(row, verified=True)
    assert receipt.nutrients.protein == 15 and receipt.nutrients.fiber is None
    assert receipt.entry_id == "e1" and receipt.verified is True
    # A stored 0 that the commit recorded as unknown is shown as unknown.
    zero_row = {**row, "fiber": 0}
    assert c.Receipt.from_saved_row(zero_row, verified=True, unknown_nutrients=["fiber"]).nutrients.fiber is None


def test_sum_panels_unknown_poisons_the_total():
    total = c.sum_panels([c.NutrientPanel(calories=100, protein=10, carbs=None, fat=1, fiber=None),
                          c.NutrientPanel(calories=50, protein=5, carbs=4, fat=None, fiber=None)])
    assert total.calories == 150 and total.protein == 15
    assert total.carbs is None and total.fat is None and total.fiber is None


def test_tool_outcome_vocabulary():
    outcome = c.ToolOutcome(status="unknown", effect="unknown", safe_message="Could not verify it saved.")
    assert outcome.status == "unknown"
    with pytest.raises(ValidationError):
        c.ToolOutcome(status="ok", effect="maybe", safe_message="x")
    with pytest.raises(ValidationError):
        c.ToolOutcome(status="ok", effect="applied_verified", safe_message="x", entry_id="e1")


def test_contracts_are_frozen():
    item = _draft_item()
    with pytest.raises(ValidationError):
        item.multiplier = 2.0  # type: ignore[misc]
