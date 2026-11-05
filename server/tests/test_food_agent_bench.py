"""WP8: the food agent bench harness.

Runs the SYNTHETIC fixture (invented, de-identified) through the real
``CanvasService.turn`` with the food agent enabled and a scripted interpreter,
a fake resolver behind the real ``FoodFinder``, and ``MemoryDrafts`` offline or
``StoreDrafts`` on a throwaway PostgreSQL. Also unit-tests the scorer by
scoring deliberately wrong runs.

``test_private_bench`` runs only when FOOD_AGENT_BENCH_DIR is set (labels.json
and scripts.json live there, never in the repo). It records scores and fails
only when the harness itself crashes.

Lanes A and B prove the code does the right thing GIVEN a correct
interpretation. They do not prove the model interprets correctly.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import food_agent_bench as bench  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "food_agent_bench_synthetic.json"
ADMIN_URL = os.environ.get("MMMACROS_TEST_PG_ADMIN_URL", "")
pg = pytest.mark.skipif(not ADMIN_URL, reason="set MMMACROS_TEST_PG_ADMIN_URL to a throwaway local PostgreSQL")
PRIVATE_DIR = os.environ.get("FOOD_AGENT_BENCH_DIR", "")

ZERO_TOLERANCE = ("false_logged", "duplicates", "stale_version_commits", "cross_tenant_leaks",
                  "committed_before_confirm")


def _spec():
    return bench.load_bench(FIXTURE)


def _case(spec, case_id):
    return next(c for c in spec["cases"] if c["id"] == case_id)


async def _run_one(case_id, backend=None):
    spec = _spec()
    backend = backend or bench.MemoryBackend()
    case = _case(spec, case_id)
    script = spec["scripts"][case_id]
    run = await bench.run_case(case, script, backend)
    return case, script, run


# ---------------------------------------------------------------- the synthetic bench, offline


async def test_synthetic_bench_offline_all_four_succeed_and_zero_tolerance_is_zero():
    spec = _spec()
    results = await bench.run_bench(spec, bench.MemoryBackend(), lane="A")
    failing = {c["id"]: c["reasons"] for c in results["cases"] if not c["success"]}
    assert failing == {}
    summary = results["summary"]
    assert summary["case_success"] == {"n": 4, "d": 4}
    assert summary["identity"]["n"] == summary["identity"]["d"] == 4
    assert summary["portion"]["n"] == summary["portion"]["d"] == 4
    for counter in ZERO_TOLERANCE:
        assert summary["zero_tolerance"][counter] == 0, counter
    assert summary["failure_classes"]["wrong_past_meal"]["agent"] == 0
    assert summary["failure_classes"]["lost_fraction"]["agent"] == 0
    assert summary["failure_classes"]["reasked_confirmed_item"]["agent"] == 0
    assert summary["old_app"]["case_success"] == {"n": 1, "d": 4}
    assert summary["latency_ms"]["label"] == "local code time, scripted model, mocked lookups"
    assert summary["latency_ms"]["turn"]["n"] >= 8 and summary["latency_ms"]["turn"]["p95"] >= \
        summary["latency_ms"]["turn"]["p50"] > 0
    json.dumps(results)  # the results file must serialize


async def test_recall_case_copies_the_target_entry_and_never_the_unrelated_one():
    case, script, run = await _run_one("syn-last-time-bowl")
    (entry,) = run.entries
    assert entry["name"] == "Teriyaki Salmon Bowl" and entry["calories"] == 640.0
    assert "Pancake" not in json.dumps(run.entries) and "B-SECRET" not in json.dumps(run.entries)
    scored = bench.score_case(case, script, run)
    assert scored["success"] and scored["failure_classes"]["wrong_past_meal"] == 0


async def test_fraction_and_oz_cases_save_the_scaled_numbers():
    _, _, run = await _run_one("syn-half-burger")
    (entry,) = run.entries
    assert entry["calories"] == 320.0 and entry["protein"] == 18.0  # half of the 640 / 36 panel
    _, _, run = await _run_one("syn-smoothie-six-oz")
    (entry,) = run.entries
    assert entry["calories"] == 170.0  # 6 of 12 fl oz
    assert [t.user for t in run.turns] == ["One Berry Blast smoothie from Juice Hut",
                                          "yes but 6 oz instead of 12", "yes"]


async def test_the_old_write_path_never_runs_and_no_row_before_the_confirming_turn():
    for case_id in ("syn-half-burger", "syn-smoothie-six-oz", "syn-boiled-eggs-control"):
        _, _, run = await _run_one(case_id)
        assert run.old_path_calls == 0
        rows_by_turn = [t.entries_after for t in run.turns]
        assert rows_by_turn[:-1] == [0] * (len(rows_by_turn) - 1)  # only the last turn writes
        assert rows_by_turn[-1] == 1


async def test_final_confirmation_is_marked_synthetic_when_the_script_has_no_yes():
    _, _, run = await _run_one("syn-half-burger")
    assert [t.synthetic for t in run.turns] == [None, None, "final"]
    assert run.turns[-1].user == "yes" and run.turns[-1].commits


# ---------------------------------------------------------------- scorer unit tests: wrong runs fail


async def test_scorer_marks_a_wrong_food_as_failure():
    case, script, run = await _run_one("syn-half-burger")
    wrong = copy.deepcopy(run)
    for entry in wrong.entries:
        for item in entry["component_metadata"]["items"]:
            item["display_name"] = "Harbor Grill Double Smokehouse Burger with Bacon"
    scored = bench.score_case(case, script, wrong)
    assert not scored["success"]
    assert scored["identity"] == {"n": 0, "d": 1}
    assert any("identity" in reason for reason in scored["reasons"])


async def test_scorer_marks_a_lost_fraction_as_failure():
    case, script, run = await _run_one("syn-half-burger")
    wrong = copy.deepcopy(run)
    for entry in wrong.entries:
        entry["component_metadata"]["items"][0]["multiplier"] = 1.0
    scored = bench.score_case(case, script, wrong)
    assert not scored["success"]
    assert scored["portion"] == {"n": 0, "d": 1} and scored["identity"] == {"n": 1, "d": 1}
    assert scored["failure_classes"]["lost_fraction"] == 1


async def test_scorer_marks_a_duplicate_row_as_failure():
    case, script, run = await _run_one("syn-boiled-eggs-control")
    wrong = copy.deepcopy(run)
    twin = copy.deepcopy(wrong.entries[0])
    twin["id"] = str(uuid4())
    wrong.entries.append(twin)
    scored = bench.score_case(case, script, wrong)
    assert not scored["success"]
    assert scored["counters"]["duplicates"] >= 1


async def test_scorer_marks_a_false_logged_claim_with_no_row_as_failure():
    case, script, run = await _run_one("syn-boiled-eggs-control")
    wrong = copy.deepcopy(run)
    wrong.entries = []
    for turn in wrong.turns:
        turn.entries_after = 0
        turn.commits = []
    wrong.turns[-1].reply = "Logged Boiled Egg (2): 12.6 g protein, 156 kcal, 1.2 g carbs, 10.6 g fat."
    scored = bench.score_case(case, script, wrong)
    assert not scored["success"]
    assert scored["counters"]["false_logged"] == 1


async def test_scorer_does_not_call_a_negated_reply_a_logged_claim():
    case, script, run = await _run_one("syn-boiled-eggs-control")
    wrong = copy.deepcopy(run)
    wrong.entries = []
    for turn in wrong.turns:
        turn.entries_after = 0
        turn.commits = []
    wrong.turns[-1].reply = "OK, not logged. I could not verify it saved, so don't log it again."
    assert bench.score_case(case, script, wrong)["counters"]["false_logged"] == 0


async def test_scorer_flags_a_commit_before_the_confirming_turn_and_a_stale_commit():
    case, script, run = await _run_one("syn-boiled-eggs-control")
    early = copy.deepcopy(run)
    early.turns[0].commits = list(early.turns[-1].commits)  # a write on the first, unconfirmed turn
    early.turns[0].entries_after = 1
    assert bench.score_case(case, script, early)["counters"]["committed_before_confirm"] == 1

    stale = copy.deepcopy(run)
    stale.turns[-1].commits[0]["content_hash"] = "0" * 64  # not the hash the user was shown
    scored = bench.score_case(case, script, stale)
    assert scored["counters"]["stale_version_commits"] == 1 and not scored["success"]


async def test_scorer_flags_a_cross_tenant_leak_in_a_reply_or_a_row():
    case, script, run = await _run_one("syn-boiled-eggs-control")
    assert run.decoy_markers and "B-SECRET" in run.decoy_markers
    in_reply = copy.deepcopy(run)
    in_reply.turns[0].reply += " (B-SECRET Boiled Egg)"
    assert bench.score_case(case, script, in_reply)["counters"]["cross_tenant_leaks"] == 1
    in_row = copy.deepcopy(run)
    in_row.entries[0]["name"] = "B-SECRET Boiled Egg"
    scored = bench.score_case(case, script, in_row)
    assert scored["counters"]["cross_tenant_leaks"] == 1 and not scored["success"]


async def test_scorer_flags_receipt_numbers_that_differ_from_the_saved_row():
    case, script, run = await _run_one("syn-boiled-eggs-control")
    wrong = copy.deepcopy(run)
    wrong.turns[-1].reply = wrong.turns[-1].reply.replace("156 kcal", "999 kcal")
    scored = bench.score_case(case, script, wrong)
    assert not scored["success"] and any("receipt" in reason for reason in scored["reasons"])


async def test_scorer_wrong_past_meal_when_an_unrelated_history_entry_is_copied():
    case, script, run = await _run_one("syn-last-time-bowl")
    wrong = copy.deepcopy(run)
    wrong.entries[0]["name"] = "Two Pancake Stack"
    for item in wrong.entries[0]["component_metadata"]["items"]:
        item["display_name"] = "Two Pancake Stack"
    scored = bench.score_case(case, script, wrong)
    assert not scored["success"] and scored["failure_classes"]["wrong_past_meal"] == 1


def test_clarify_case_scoring_needs_a_question_and_no_rows():
    case = {"id": "x", "expected_action": "recall_not_found_ask", "gold_items": [], "old_app": {},
            "expected_question": ["tuna bowl", ["where", "what"]]}
    ask = bench.TurnRecord(idx=0, user="the tuna bowl again", reply="I couldn't find a tuna bowl in your log. "
                           "Where was it from, or what was in it?", is_question=True, question="Where?")
    run = bench.CaseRun(case_id="x", turns=[ask], entries=[], decoy_markers=[])
    assert bench.score_case(case, {}, run)["success"]
    listed = bench.TurnRecord(idx=0, user="x", reply="Which one?\n1. Tuna\n2. Salmon", is_question=True,
                              question="Which one?")
    assert not bench.score_case(case, {}, bench.CaseRun(case_id="x", turns=[listed], entries=[],
                                                        decoy_markers=[]))["success"]
    wrote = bench.CaseRun(case_id="x", turns=[ask], decoy_markers=[],
                          entries=[{"id": "1", "name": "Wrap", "component_metadata": {"items": []}}])
    assert not bench.score_case(case, {}, wrote)["success"]


async def test_receipt_is_checked_against_the_row_as_written_in_that_turn_not_the_final_amended_row():
    case, script, run = await _run_one("syn-boiled-eggs-control")
    amended = copy.deepcopy(run)
    amended.entries[0]["calories"], amended.entries[0]["protein"] = 312.0, 25.2  # a later amend of the same entry
    reasons = bench.score_case(case, script, amended)["reasons"]
    assert not any("receipt" in reason for reason in reasons), reasons


async def test_receipt_that_differs_from_the_row_written_in_the_same_turn_is_flagged():
    case, script, run = await _run_one("syn-boiled-eggs-control")
    wrong = copy.deepcopy(run)
    commit_turn = next(t for t in wrong.turns if t.commits)
    commit_turn.rows[0]["calories"] = 999.0  # the row that turn wrote disagrees with what the reply said
    wrong.entries[0]["calories"] = 999.0
    assert any("receipt" in r for r in bench.score_case(case, script, wrong)["reasons"])


def test_claims_logged_matches_a_claim_not_a_mention_inside_a_question():
    question = "That isn't a flavor I know. Did you mean the vanilla bars you logged earlier?"
    assert not bench.claims_logged(question)
    assert not bench.claims_logged("Which one did I save last week, the bowl or the wrap?")
    assert bench.claims_logged("Logged Boiled Egg (2): 12.6 g protein, 156 kcal.")
    assert bench.claims_logged("Saved Boiled Egg: 156 kcal.")
    assert bench.claims_logged("Updated Chocolate Smoothie: 176 kcal.")
    assert bench.claims_logged("Nothing changed. Logged Boiled Egg: 156 kcal.")  # a later sentence still claims
    assert not bench.claims_logged("OK, not logged.")
    assert not bench.claims_logged("Not logged. I could not verify it saved.")


async def test_question_mentioning_a_past_log_is_not_a_false_logged_claim():
    case, script, run = await _run_one("syn-boiled-eggs-control")
    asked = copy.deepcopy(run)
    asked.turns[0].reply = "Did you mean the eggs you logged earlier?"
    assert bench.score_case(case, script, asked)["counters"]["false_logged"] == 0


async def test_synthetic_final_yes_follows_a_preview_after_an_earlier_receipt():
    # A canvas snapshot keeps the previous receipt; a later unconfirmed preview must still get its final yes.
    spec = _spec()
    case = copy.deepcopy(_case(spec, "syn-boiled-eggs-control"))
    script = copy.deepcopy(spec["scripts"]["syn-boiled-eggs-control"])
    case["user_turns"] = ["2 boiled eggs", "yes", "2 boiled eggs"]
    script["turns"] = [script["turns"][0], {"model": None}, script["turns"][0]]
    script["scoring"] = {"expected_entries": 2, "accept_items": [0]}
    run = await bench.run_case(case, script, bench.MemoryBackend())
    assert [t.synthetic for t in run.turns] == [None, None, None, "final"]
    assert len(run.entries) == 2


def test_identity_allows_the_golds_own_preparation_words_but_nothing_else():
    gold = {"name": "egg", "brand": None, "variant": None, "kind": "generic", "preparation": "sunny side up"}
    assert bench.identity_matches(gold, "Sunny Side Up Egg")
    assert not bench.identity_matches(gold, "Egg Salad")  # another product
    assert not bench.identity_matches({**gold, "preparation": None}, "Sunny Side Up Egg")
    assert not bench.identity_matches(gold, "Sunny Side Up")  # the food itself is still required


# ---------------------------------------------------------------- S5: an independent scorer


def test_scorer_never_calls_the_portion_math_it_is_checking():
    source = Path(bench.__file__).read_text()
    assert not hasattr(bench, "expected_multiplier")
    assert "food_agent.portion" not in source and "import multiplier" not in source


async def test_saved_multiplier_is_compared_to_the_hand_derived_expected_value():
    case, script, run = await _run_one("syn-half-burger")
    assert bench.score_case(case, script, run)["success"]
    wrong = copy.deepcopy(script)
    wrong["scoring"]["expected_multiplier"] = {"0": 0.25}  # the code saved 0.5 and agrees with itself; the value is wrong
    scored = bench.score_case(case, wrong, run)
    assert not scored["success"] and scored["portion"] == {"n": 0, "d": 1}
    assert scored["failure_classes"]["lost_fraction"] == 1


async def test_a_gold_item_with_no_expected_multiplier_cannot_pass_the_portion_check():
    case, script, run = await _run_one("syn-half-burger")
    bare = copy.deepcopy(script)
    bare["scoring"].pop("expected_multiplier")
    scored = bench.score_case(case, bare, run)
    assert not scored["success"] and scored["portion"] == {"n": 0, "d": 1}
    assert any("expected_multiplier" in reason for reason in scored["reasons"])


async def test_expected_grams_is_checked_when_given():
    case, script, run = await _run_one("syn-smoothie-six-oz")
    assert bench.score_case(case, script, run)["success"]  # 6 of 12 fl oz = 170 g of the scripted 340 g serving
    wrong = copy.deepcopy(script)
    wrong["scoring"]["expected_grams"] = {"0": 100}
    scored = bench.score_case(case, wrong, run)
    assert not scored["success"] and any("grams" in reason for reason in scored["reasons"])


def test_half_against_a_per_100g_catalog_is_a_question_not_fifty_grams():
    case = {"id": "x", "expected_action": "clarify", "gold_items": [], "old_app": {}}
    fifty = bench.CaseRun(case_id="x", decoy_markers=[], turns=[bench.TurnRecord(
        idx=0, user="half a chicken breast", reply="Chicken breast: 5 g protein. Log it?")],
        entries=[{"id": "1", "name": "Chicken breast", "component_metadata": {"items": [
            {"display_name": "Chicken breast", "multiplier": 0.5, "provider": "FatSecret"}]}}])
    scored = bench.score_case(case, {}, fifty)
    assert not scored["success"]


@pytest.mark.parametrize("gold,shown,expected", [
    ({"name": "fries", "brand": "McDonald's"}, "McDonald's Large Fries", False),
    ({"name": "fries", "brand": "McDonald's"}, "McDonald's Fries", True),
    # Preparation and product differences remain evidence requirements, not ignored qualifiers.
    ({"name": "chicken breast"}, "Chicken breast, raw", False),
    ({"name": "chicken breast", "preparation": "raw"}, "Raw chicken breast", True),
    ({"name": "Quarter Pounder", "brand": "McDonald's"}, "McDonald's Quarter Pounder with Cheese", False),
    ({"name": "coffee"}, "Regular Coffee", False),
    ({"name": "burger"}, "Double Burger", False),
    ({"name": "smoothie"}, "Mini Smoothie", False),
    ({"name": "smoothie"}, "Small Strawberry Smoothie", False),
    ({"name": "rice"}, "Cooked Rice", False),
    ({"name": "large fries"}, "Fries", False),
    ({"name": "rice", "preparation": "cooked"}, "Cooked Rice", True),
    ({"name": "egg"}, "chicken egg", True),
    ({"name": "blueberry bagel"}, "Thomas' Blueberry Bagel", False),
    ({"name": "egg"}, "Egg, duck, whole, fresh, raw", False),
])
def test_identity_keeps_size_preparation_and_flavor_words(gold, shown, expected):
    assert bench.identity_matches({"brand": None, "variant": None, "preparation": None, **gold}, shown) is expected


@pytest.mark.parametrize("reply,claim", [
    ("I logged it.", True),
    ("I've logged your eggs.", True),
    ("All set, logged it for you.", True),
    ("Saved it.", True),
    ("I added it to your log.", True),
    ("Logged Burger, no sauce: 30 g protein.", True),
    ("Logged Burger: 30 g protein, no fiber data.", True),
    ("There is no sauce, but I've logged the burger.", True),
    ("I didn't log it.", False),
    ("I haven't logged it yet.", False),
    ("OK, not logged.", False),
    ("I couldn't log it, so nothing was saved.", False),
    ("Did you mean the Creamy Crisp bars you logged earlier?", False),
    ("Did I log it already?", False),
])
def test_claims_logged_first_person_and_receipt_forms(reply, claim):
    assert bench.claims_logged(reply) is claim


async def test_any_old_path_call_fails_the_case():
    case, script, run = await _run_one("syn-boiled-eggs-control")
    assert bench.score_case(case, script, run)["success"]
    handed = copy.deepcopy(run)
    handed.old_path_calls = 1
    scored = bench.score_case(case, script, handed)
    assert not scored["success"] and any("old path" in reason for reason in scored["reasons"])


async def test_the_old_path_stub_records_a_write_it_attempts():
    old = bench.OldPath()
    attempted = []

    async def log_meal(args):
        attempted.append(args)
        return {"status": "committed"}
    reply, _ = await old(message="yes", handlers={"log_meal": log_meal})
    assert old.calls == ["yes"] and len(old.writes) == 1 and attempted


def test_percentiles():
    assert bench.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert bench.percentile([5.0], 95) == 5.0
    assert bench.percentile([], 95) is None


# ---------------------------------------------------------------- real PostgreSQL


@pg
async def test_synthetic_bench_on_postgres_all_four_succeed():
    backend = await bench.PgBackend.create(ADMIN_URL)
    try:
        results = await bench.run_bench(_spec(), backend, lane="B")
    finally:
        await backend.aclose()
    failing = {c["id"]: c["reasons"] for c in results["cases"] if not c["success"]}
    assert failing == {}
    summary = results["summary"]
    assert summary["case_success"] == {"n": 4, "d": 4}
    for counter in ZERO_TOLERANCE:
        assert summary["zero_tolerance"][counter] == 0, counter


# ---------------------------------------------------------------- the private bench (records, never scores)


async def test_private_bench(request):
    private_dir = request.config.getoption("--food-agent-bench-dir") or PRIVATE_DIR
    if not private_dir:
        pytest.skip("set FOOD_AGENT_BENCH_DIR or pass --food-agent-bench-dir to run the private bench")
    root = Path(private_dir)
    spec = bench.load_bench(root / "labels.json", root / "scripts.json")
    out_a = await bench.run_bench(spec, bench.MemoryBackend(), lane="A")
    (root / "results-laneA.json").write_text(json.dumps(out_a, indent=2))
    assert len(out_a["cases"]) == len(spec["cases"])
    if ADMIN_URL:
        backend = await bench.PgBackend.create(ADMIN_URL)
        try:
            out_b = await bench.run_bench(spec, backend, lane="B")
        finally:
            await backend.aclose()
        (root / "results-laneB.json").write_text(json.dumps(out_b, indent=2))
        assert len(out_b["cases"]) == len(spec["cases"])
    crashed = [c["id"] for c in out_a["cases"] if c.get("harness_error")]
    assert crashed == []
