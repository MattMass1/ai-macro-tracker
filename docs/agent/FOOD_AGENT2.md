# food_agent2

The food-logging agent and retrieval layer in `server/src/food_agent2/`. It replaces
the orchestration in `food_agent/` and the legacy `meal_drafts` chat path once it
passes the live bench. It is off by default. The iPhone app, the schema and the API
contract do not change.

## Switching it on

| Flag | Meaning |
|---|---|
| `FOOD_AGENT2_ENABLED` | Exact `true` opens it. Anything else is closed. |
| `FOOD_AGENT2_USER_IDS` | Optional comma-separated UUID canary list. Malformed means nobody. |

With the flag off, `_food_agent_engine` behaves as before and `CanvasService.turn` is
unchanged. If both this flag and `FOOD_AGENT_ENABLED` are open, this one wins.
Rollback is unsetting the flag. There is no migration, and drafts it wrote stay in
the migration 004 tables under the same marker the earlier agent uses.

## How a turn works

1. **Interpret.** One strict JSON model call. The model returns identity, portion,
   changes and intent. It has no way to ask a question.
2. **Update the draft.** Items are stored as numbers across turns.
3. **Resolve.** The ladder in `retrieval.py`, run per item in parallel with a per-turn
   memo and cap: exact match, then (whole foods) the food without its preparation words,
   a typical dish from pinned rows and the closest USDA row, or (named products) the closest
   FatSecret hit under the same brand, then a labeled web estimate, then not counted.
4. **Preview or ask.** Code chooses. There are only two questions, how much and raw or
   cooked on a weighed food, and each is asked once per draft.
5. **Commit.** Only on the user's own plain yes to the preview shown last in that session,
   with revision and content hash matching. The row is read back and the receipt comes
   from the row. An unverified outcome is reconciled by the same key on the next turn.

Numbers the user types are checked against their own message before use, and a
number that does not appear there is dropped.

## Chain menu wording

`server/src/food_words.py` puts what people say and what a menu says in one wording
before any comparison: `10"`, `10in` and `10 inch` are the same; "personal" and
"individual" are the chain's small; "whole pie" and "pizza pie" are "pizza" (the amount
is the portion's job). The brand never erases the food word ("pizza" in "Domino's
Pizza"). A stated size or diameter that differs is a different food. A named product
that the full phrase does not find is searched again in simpler words ("Domino's cheese
pizza", then "Domino's pizza") and ranked against what was said. Among a product's
servings, the one naming the user's unit is used ("whole pie" takes "1 pizza", no
amount on a pizza takes the pizza); a pizza sold only by the slice asks how many slices.

### Menu picker (the agentic step)

Word rules cannot know every chain's menu. When no candidate is a clear word match
(score below 0.9), `food_agent2/menu_pick.py` makes one strict JSON model call with the
user's words and the same-brand candidates code found, and the model returns the id of
the item the user meant, using what it knows about the chain (a "personal" pizza is the
chain's smallest, no topping named is cheese). It can only return an id it was offered;
code has already removed candidates that are a different food (species, flavor, variant,
a stated size). If none fits, it may give two searches in the chain's own words, and code
searches once more and asks again. A failed call, or `FOOD_AGENT2_MENU_PICK=false`, falls
back to the word ranking.

### Everyday defaults

Foods are read the way a person means them. The kind everybody assumes goes unsaid, as
databases leave it unsaid: "chicken eggs" are searched as "eggs", "cow's milk" as "milk"
(`food_words.default_kind`). Another animal is another food: a row naming an animal the
user did not say or mean is refused (`match.conflict`; "eggs" mean chicken, "bacon" means
pork, a named animal wins). "Cooked" is a preparation word, so "3 cooked eggs" uses the
curated chicken egg. When more than one database row could be the food, the menu picker
chooses with everyday defaults (plain, as usually eaten), among rows code has not refused.
The interpreter names whole foods in plain words and treats "it's a chicken egg" as a
revision of the item shown.

## Serving units

`server/src/serving_units.py` decides in code what one of a food is counted in: bread
and toast in slices, milk in cups, peanut butter, butter, oil, honey and mayo in
tablespoons, a banana, apple or orange as one medium. It applies when the user names
that unit or no unit ("a slice", "2", nothing), never to a weight ("2 oz") or another
unit ("a loaf"). The weight of one unit is the matched USDA row's own plain or medium
measure, otherwise the pinned typical weight in the table, and the food's attribution
records which (`serving_unit_source`). The label and logged name carry the unit
("Sourdough bread (1 slice)"), and a "how much?" question offers the unit's chips
instead of 4 / 6 / 8 oz. Both food agents use it through `usda_lookup.for_portion`
and `find_food.with_serving_unit`.

## Testing

```sh
cd server
../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_food_agent2_*.py

# scripted lanes (code paths only, not product quality)
../.venv/bin/python scripts/food_agent_bench.py --agent 2 --lane A --bench <labels> --scripts <scripts> --out <file>

# live lane C (costs money; needs provider keys)
../.venv/bin/python scripts/food_agent_bench.py --agent 2 --lane C --bench <labels> --scripts <scripts> \
  --pg-admin-url <throwaway> --rate-card <card> --repeats 2 --out <outside the worktree>
```

The scripted lanes use scripts written for the earlier contract. They carry no
`supplied` numbers or `settle` actions, so user-number cases can only pass on lane C.

## Known limits

- **Strict bench success cannot pass yet.** `success` requires `expected_nutrients`, which no
  label has. Read `case_success_behavior` until the labels gain independent gold nutrition.
- **Unknown nutrients.** While `CALORIE_ONLY_WRITES_ENABLED` is closed, a food with an unknown
  nutrient (often fiber on a web estimate) cannot be saved and is named as not counted. A user's
  own numbers fill gaps from the matched source, scaled to their calories.
- **Voice** uses the same agent with no separate behavior yet. Workouts are not part of this layer.
