"""WP5: ``recall_meal`` from the user's OWN saved history (real failure 1).

Old app: "I had an ahi tuna bowl again, the one I had last time" logged two
Burger King Royal Crispy Wraps (the newest unrelated entry) and said it was
logged. The recaller must match food words, never fall back to "most recent",
be honest when nothing matches, and never see another user's entries.

Offline tests use a fake store with the same search method as ``store.Store``.
Real-Postgres tests run only with MMMACROS_TEST_PG_ADMIN_URL (disposable server).
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from food_agent.contracts import DraftItemState

# ---------------------------------------------------------------- fake store


def _row(name, day, *, protein=40.0, calories=550.0, carbs=60.0, fat=15.0, fiber=5.0,
         macro_source="Poke Spot menu", metadata=None, created_hour=12, entry_id=None):
    return {"id": entry_id or str(uuid4()), "name": name, "meal": "Lunch", "calories": calories,
            "protein": protein, "carbs": carbs, "fat": fat, "fiber": fiber, "day": day.isoformat(),
            "created_at": datetime(day.year, day.month, day.day, created_hour, tzinfo=timezone.utc).isoformat(),
            "macro_source": macro_source, "entry_revision": 1, "component_metadata": metadata}


TUNA = dict(name="Ahi Tuna Bowl", day=date(2026, 10, 15))
BK = dict(name="Burger King Royal Crispy Wrap (2)", day=date(2026, 10, 19), protein=30.0, calories=620.0,
          macro_source="Burger King nutrition")


class FakeStore:
    """Mirrors ``Store.search_own_entries``: owner-scoped rows whose name or
    component_metadata text contains ANY term, in [since, until], newest first."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.calls: list[dict] = []

    async def search_own_entries(self, terms, *, since, until=None, limit=20):
        self.calls.append({"terms": list(terms), "since": since, "until": until, "limit": limit})
        out = []
        for row in self.rows:
            day = date.fromisoformat(row["day"])
            if day < since or (until is not None and day > until):
                continue
            text = (row["name"] + " " + json.dumps(row["component_metadata"] or {})).casefold()
            if any(term.casefold() in text for term in terms):
                out.append(row)
        out.sort(key=lambda r: (r["day"], r["created_at"]), reverse=True)
        return out[:limit]


def _clock(y=2026, m=10, d=19, h=17):
    now = datetime(y, m, d, h, 0, tzinfo=timezone.utc)  # 13:00 New York, Monday Oct 19 2026
    return lambda: now


def _recaller(rows, clock=None):
    from food_agent.recall import HistoryRecaller
    store = FakeStore(rows)
    return HistoryRecaller(store, clock=clock or _clock()), store


def _no_numbered_list(text: str) -> None:
    assert not re.search(r"(^|\n|\s)\d+[.)]\s", text), text


# ---------------------------------------------------------------- real failure 1


async def test_unrelated_recent_entries_and_no_tuna_is_not_found():
    recaller, _ = _recaller([_row(**BK), _row("Grande Iced Latte", date(2026, 10, 18), protein=8.0)])
    result = await recaller.recall("ahi tuna bowl", "last time")
    assert result.status == "not_found"
    assert result.items == () and result.source_entry_id is None
    text = f"{result.note or ''} {result.question or ''}"
    assert "ahi tuna bowl" in text.casefold() and "couldn't find" in text.casefold()
    assert result.question and result.question.rstrip().endswith("?")
    assert "burger" not in text.casefold() and "wrap" not in text.casefold()


async def test_filler_words_never_widen_the_match():
    recaller, _ = _recaller([_row(**BK)])
    for query in ("the one I had last time", "same as last time", "again"):
        result = await recaller.recall(query, None)
        assert result.status != "found" and result.items == ()


async def test_last_thursday_finds_the_tuna_not_the_newer_wrap():
    tuna, wrap = _row(**TUNA), _row(**BK)
    recaller, store = _recaller([wrap, tuna])
    result = await recaller.recall("ahi tuna bowl", "last Thursday")
    assert result.status == "found"
    assert result.source_entry_id == tuna["id"]
    (item,) = result.items
    assert isinstance(item, DraftItemState)
    assert item.resolved is not None and item.resolved.display_name == "Ahi Tuna Bowl"
    assert item.resolved.food_id == f"history:{tuna['id']}" and item.resolved.provider == "history"
    assert item.resolved.panel.protein == 40.0 and item.resolved.panel.calories == 550.0
    assert item.portion.kind == "serving_fraction" and item.portion.amount == 1.0
    assert item.multiplier == 1.0 and item.scaled_panel().calories == 550.0
    assert item.resolved.macro_source == "Poke Spot menu"
    assert "Oct 15" in (result.note or "") and "Thursday" in (result.note or "")
    assert len(store.calls) == 1 and store.calls[0]["until"] == date(2026, 10, 19)  # one owner-scoped query
    assert wrap["id"] not in json.dumps([i.model_dump(mode="json") for i in result.items])


async def test_the_whole_real_failure_sentence_as_a_query():
    tuna, wrap = _row(**TUNA), _row(**BK)
    recaller, _ = _recaller([wrap, tuna])
    result = await recaller.recall("I had an ahi tuna bowl again, the one I had last time", None)
    assert result.status == "found" and result.source_entry_id == tuna["id"]


async def test_most_recent_matching_date_wins_within_the_window():
    old = _row("Ahi Tuna Bowl", date(2026, 9, 30), protein=35.0)
    new = _row("Ahi Tuna Bowl", date(2026, 10, 15), protein=40.0)
    recaller, store = _recaller([old, new])
    result = await recaller.recall("ahi tuna bowl", None)
    assert result.status == "found" and result.source_entry_id == new["id"]
    assert store.calls[0]["since"] <= date(2026, 10, 19) - timedelta(days=120)


async def test_when_hint_respected_over_a_newer_same_food():
    old = _row("Ahi Tuna Bowl", date(2026, 10, 15), protein=40.0)
    new = _row("Ahi Tuna Bowl", date(2026, 10, 17), protein=44.0)
    recaller, _ = _recaller([old, new])
    result = await recaller.recall("ahi tuna bowl", "on Thursday")
    assert result.status == "found" and result.source_entry_id == old["id"]
    yesterday = await recaller.recall("ahi tuna bowl", "Saturday")
    assert yesterday.source_entry_id == new["id"]


@pytest.mark.parametrize("hint,expected", [
    ("yesterday", date(2026, 10, 18)), ("today", date(2026, 10, 19)), ("last Thursday", date(2026, 10, 15)),
    ("on Monday", date(2026, 10, 19)), ("last monday", date(2026, 10, 12)), ("2026-10-15", date(2026, 10, 15)),
    ("Oct 15", date(2026, 10, 15)), ("October 15th", date(2026, 10, 15)), ("10/15", date(2026, 10, 15)),
    ("3 days ago", date(2026, 10, 16)),
])
def test_when_hint_resolves_to_user_days(hint, expected):
    from food_agent.recall import resolve_when
    windows = resolve_when(hint, date(2026, 10, 19))
    assert windows[0] == (expected, expected)


def test_unknown_or_vague_hint_does_not_narrow():
    from food_agent.recall import resolve_when
    assert resolve_when("last time", date(2026, 10, 19)) == []
    assert resolve_when(None, date(2026, 10, 19)) == []
    assert resolve_when("at the beach", date(2026, 10, 19)) == []


async def test_day_rolls_over_at_4am_local():
    # 02:30 New York on Tue Oct 20 is still the Monday Oct 19 logging day.
    clock = _clock(2026, 10, 20, 6)  # 06:00 UTC = 02:00 EDT
    tuna = _row("Ahi Tuna Bowl", date(2026, 10, 18))
    recaller, _ = _recaller([tuna], clock=clock)
    assert (await recaller.recall("ahi tuna bowl", "yesterday")).source_entry_id == tuna["id"]


async def test_hint_day_without_the_food_says_which_day_it_used():
    tuna = _row("Ahi Tuna Bowl", date(2026, 10, 14))  # a Wednesday
    recaller, _ = _recaller([tuna, _row(**BK)])
    result = await recaller.recall("ahi tuna bowl", "last Thursday")
    assert result.status == "found" and result.source_entry_id == tuna["id"]
    assert "Oct 14" in result.note and "Oct 15" in result.note


# ---------------------------------------------------------------- matching tolerance


@pytest.mark.parametrize("query", ["ahi tuna bowls", "Ahi-Tuna Bowl!", "AHI TUNA BOWL", "tuna bowl"])
async def test_plural_case_and_punctuation_are_found(query):
    tuna = _row(**TUNA)
    recaller, _ = _recaller([_row(**BK), tuna])
    result = await recaller.recall(query, None)
    assert result.status == "found" and result.source_entry_id == tuna["id"], query


@pytest.mark.parametrize("query", ["ahi tuan bowl", "ahi tuna bwol"])
async def test_a_typo_only_match_asks_did_you_mean(query):
    tuna = _row(**TUNA)
    recaller, _ = _recaller([_row(**BK), tuna])
    result = await recaller.recall(query, None)
    assert result.status == "needs_detail" and result.items == (), query
    assert result.question.startswith("Did you mean the Ahi Tuna Bowl") and result.question.count("?") == 1


# Review S3 (probes P9, P9b): a typo must never turn into a different real food.
@pytest.mark.parametrize("query,entry", [("my mocha from last time", "Mochi ice cream"),
                                         ("the jerky I had", "Jerk chicken thighs"),
                                         ("toast again", "Roast beef"),
                                         ("ahi tuna bowel", "Ahi Tuna Bowl")])
def test_typo_tolerance_never_bridges_different_foods(query, entry):
    from food_agent.recall import food_words, match, tokens_of
    assert match(food_words(query), tokens_of(entry)) is None


@pytest.mark.parametrize("query,entry", [("mocha", "Mochi ice cream"), ("jerky", "Jerk chicken thighs"),
                                         ("toast", "Roast beef")])
async def test_unrelated_near_miss_entries_are_never_found(query, entry):
    row = _row(entry, date(2026, 10, 30))
    recaller, _ = _recaller([row], clock=_clock(2026, 11, 2))
    result = await recaller.recall(query, "last time")
    assert result.status != "found" and result.items == () and result.source_entry_id is None


def test_fuzzy_needs_same_first_letter_and_longer_words():
    from food_agent.recall import match
    assert match(["chiken"], ["chicken"]) == "fuzzy"   # 6+ letters: one edit, same first letter
    assert match(["hicken"], ["chicken"]) is None      # first letter differs
    assert match(["bwol"], ["bowl"]) == "fuzzy"        # short word: adjacent swap only
    assert match(["mocha"], ["mochi"]) is None         # 5 letters: no substitution
    assert match(["mocha"], ["mohca"]) == "fuzzy"


@pytest.mark.parametrize("query", ["salmon bowl", "ahi tuna roll", "tuna sandwich", "bowl of chili",
                                   "crispy chicken wrap"])
async def test_different_foods_never_match(query):
    recaller, _ = _recaller([_row(**TUNA), _row(**BK)])
    result = await recaller.recall(query, None)
    assert result.status != "found" and result.items == (), query


async def test_burger_king_wrap_never_matches_tuna_even_alone():
    recaller, _ = _recaller([_row(**BK)])
    for hint in (None, "last Thursday", "today", "yesterday"):
        assert (await recaller.recall("ahi tuna bowl", hint)).status == "not_found"


async def test_plural_entry_name_matches_singular_query():
    wraps = _row(**BK)
    recaller, _ = _recaller([wraps])
    result = await recaller.recall("royal crispy wraps from burger king", None)
    assert result.status == "found" and result.source_entry_id == wraps["id"]


# ---------------------------------------------------------------- several foods


async def test_two_distinct_tuna_foods_ask_one_plain_question():
    poke = _row("Poke Ahi Tuna Bowl", date(2026, 10, 15))
    rice = _row("Ahi Tuna Bowl with Rice", date(2026, 10, 10))
    recaller, _ = _recaller([poke, rice, _row(**BK)])
    result = await recaller.recall("ahi tuna bowl", None)
    assert result.status == "needs_detail" and result.items == ()
    question = result.question or ""
    assert question.count("?") == 1 and question.rstrip().endswith("?")
    assert "Poke Ahi Tuna Bowl" in question and "Ahi Tuna Bowl with Rice" in question and " or " in question
    _no_numbered_list(question)
    assert "\n" not in question and "Burger" not in question


async def test_many_distinct_foods_name_at_most_three():
    names = ["Spicy Ahi Tuna Bowl", "Poke Ahi Tuna Bowl", "Ahi Tuna Bowl with Rice", "Ahi Tuna Bowl Large",
             "Ahi Tuna Bowl Keto"]
    rows = [_row(n, date(2026, 10, 15 - i)) for i, n in enumerate(names)]
    recaller, _ = _recaller(rows)
    result = await recaller.recall("ahi tuna bowl", None)
    assert result.status == "needs_detail"
    named = [n for n in names if n in result.question]
    assert 2 <= len(named) <= 3 and named[0] == "Spicy Ahi Tuna Bowl"
    _no_numbered_list(result.question)


async def test_same_food_on_several_dates_is_one_food():
    rows = [_row("Ahi Tuna Bowl", date(2026, 10, d), protein=40.0 + d) for d in (5, 9, 15)]
    rows.append(_row("ahi tuna bowls", date(2026, 10, 1)))
    recaller, _ = _recaller(rows)
    result = await recaller.recall("ahi tuna bowl", None)
    assert result.status == "found" and result.source_entry_id == rows[2]["id"]


async def test_exact_name_is_preferred_over_longer_names():
    plain = _row("Ahi Tuna Bowl", date(2026, 10, 12))
    poke = _row("Poke Ahi Tuna Bowl", date(2026, 10, 15))
    recaller, _ = _recaller([plain, poke])
    result = await recaller.recall("ahi tuna bowl", None)
    assert result.status == "found" and result.source_entry_id == plain["id"]


# ---------------------------------------------------------------- unknown nutrients and metadata


async def test_null_protein_stays_none():
    tuna = _row("Ahi Tuna Bowl", date(2026, 10, 15), protein=None, fiber=None)
    recaller, _ = _recaller([tuna])
    result = await recaller.recall("ahi tuna bowl", None)
    (item,) = result.items
    assert item.resolved.panel.protein is None and item.resolved.panel.fiber is None
    assert item.scaled_panel().protein is None and item.resolved.panel.calories == 550.0


async def test_metadata_unknown_nutrients_override_stored_zero():
    meta = {"origin": "food_agent", "schema": "food-agent/1", "unknown_nutrients": ["fiber"], "items": [{
        "item_ref": "i1", "identity": {"name": "Ahi Tuna Bowl", "brand": "Poke Spot", "variant": None,
                                       "kind": "restaurant", "preparation": None},
        "portion": {"kind": "unspecified", "amount": None, "unit": None, "text": ""}, "modifications": [],
        "multiplier": 1.0, "food_id": "fs-ahi", "provider": "fatsecret", "cache_allowed": True, "external_id": "fs-ahi",
        "display_name": "Poke Spot Ahi Tuna Bowl", "macro_source": "FatSecret: Poke Spot Ahi Tuna Bowl",
        "serving_basis": {"text": "1 bowl", "basis": "per_serving", "amount": 1.0, "unit": "count",
                          "count_noun": "bowl", "grams": 400.0, "ml": None},
        "nutrients": {"protein": 40.0, "calories": 550.0, "carbs": 60.0, "fat": 15.0, "fiber": None},
        "unknown_nutrients": ["fiber"]}]}
    tuna = _row("Poke Spot Ahi Tuna Bowl", date(2026, 10, 15), fiber=0.0, metadata=meta,
                macro_source="FatSecret: Poke Spot Ahi Tuna Bowl")
    recaller, _ = _recaller([tuna])
    result = await recaller.recall("poke spot tuna bowl", None)
    (item,) = result.items
    assert item.identity.brand == "Poke Spot" and item.identity.kind == "restaurant"
    assert item.resolved.panel.fiber is None and item.resolved.panel.protein == 40.0
    assert item.resolved.display_name == "Poke Spot Ahi Tuna Bowl"
    assert item.resolved.food_id.startswith(f"history:{tuna['id']}")


async def test_component_identity_names_are_searched():
    meta = {"origin": "food_agent", "schema": "food-agent/1", "unknown_nutrients": [], "items": [
        {"item_ref": "i1", "identity": {"name": "Ahi Tuna Bowl", "brand": None, "variant": None,
                                        "kind": "restaurant", "preparation": None},
         "portion": {"kind": "unspecified", "amount": None, "unit": None, "text": ""}, "modifications": [],
         "multiplier": 1.0, "food_id": "fs-ahi", "provider": "fatsecret", "cache_allowed": True, "external_id": None,
         "display_name": "Ahi Bowl", "macro_source": "FatSecret: Ahi Bowl",
         "serving_basis": {"text": "1 bowl", "basis": "per_serving"},
         "nutrients": {"protein": 38.0, "calories": 500.0, "carbs": 55.0, "fat": 12.0, "fiber": 4.0},
         "unknown_nutrients": []},
        {"item_ref": "i2", "identity": {"name": "Iced Green Tea", "brand": None, "variant": None,
                                        "kind": "generic", "preparation": None},
         "portion": {"kind": "unspecified", "amount": None, "unit": None, "text": ""}, "modifications": [],
         "multiplier": 1.0, "food_id": "fs-tea", "provider": "fatsecret", "cache_allowed": True, "external_id": None,
         "display_name": "Iced Green Tea", "macro_source": "FatSecret: Iced Green Tea",
         "serving_basis": {"text": "16 fl oz", "basis": "per_serving"},
         "nutrients": {"protein": 0.0, "calories": 5.0, "carbs": 1.0, "fat": 0.0, "fiber": 0.0},
         "unknown_nutrients": []}]}
    row = _row("Ahi Bowl + Iced Green Tea", date(2026, 10, 15), protein=38.0, calories=505.0, metadata=meta,
               macro_source="Composite: FatSecret: Ahi Bowl; FatSecret: Iced Green Tea")
    recaller, _ = _recaller([row])
    result = await recaller.recall("ahi tuna bowl", None)
    assert result.status == "found"
    (item,) = result.items  # only the bowl, not the tea
    assert item.identity.name == "Ahi Tuna Bowl" and item.resolved.panel.calories == 500.0
    assert item.resolved.macro_source == "FatSecret: Ahi Bowl"


async def test_placeholder_macro_source_is_replaced_with_history_provenance():
    tuna = _row("Ahi Tuna Bowl", date(2026, 10, 15), macro_source="estimate")
    recaller, _ = _recaller([tuna])
    (item,) = (await recaller.recall("ahi tuna bowl", None)).items
    import domain
    assert domain.validate_macro_source(item.resolved.macro_source)
    assert "Ahi Tuna Bowl" in item.resolved.macro_source


async def test_store_search_terms_are_food_words_only():
    recaller, store = _recaller([_row(**TUNA)])
    await recaller.recall("the ahi tuna bowl I had again last time", None)
    terms = " ".join(store.calls[0]["terms"])
    for filler in ("again", "last", "time", "had", "the"):
        assert filler not in store.calls[0]["terms"]
    assert "ahi" in terms


async def test_empty_food_words_ask_instead_of_guessing():
    recaller, store = _recaller([_row(**BK)])
    result = await recaller.recall("the one I had last time", "yesterday")
    assert result.status != "found" and result.question and result.question.endswith("?")
    assert store.calls == []


# ---------------------------------------------------------------- coordinator, offline


async def test_coordinator_previews_recalled_tuna_and_commits_it_once():
    from food_agent.coordinator import FoodAgent
    from food_agent.drafts import MemoryDrafts
    from test_food_agent_flow import Finder, Script
    clock = _clock()
    tuna, wrap = _row(**TUNA), _row(**BK)
    recaller, _ = _recaller([wrap, tuna], clock=clock)
    drafts = MemoryDrafts(clock=clock)
    script = Script()
    agent = FoodAgent(drafts, script, Finder(), recaller, clock)
    script.push({"type": "recall_meal", "query": "ahi tuna bowl", "when_hint": "last time"})
    shown = await agent.turn("I had an ahi tuna bowl again, the one I had last time", session_ref="c")
    assert shown.preview is not None and shown.receipt is None and drafts.entries == {}
    assert "Ahi Tuna Bowl" in shown.reply and "Burger" not in shown.reply
    done = await agent.turn("yes", session_ref="c")
    assert done.receipt is not None and done.receipt.name == "Ahi Tuna Bowl"
    (entry,) = drafts.entries.values()
    assert entry["name"] == "Ahi Tuna Bowl" and float(entry["protein"]) == 40.0
    assert entry["component_metadata"]["items"][0]["food_id"] == f"history:{tuna['id']}"


COORDINATOR_DROPS_QUESTION = (
    "coordinator.py FoodAgent._edit returns 'Removed. Nothing left to log.' when a recall_meal-only turn "
    "leaves no items, dropping the recaller's not-found question (outside WP5's files; reported)")


async def _offline_not_found_turn():
    from food_agent.coordinator import FoodAgent
    from food_agent.drafts import MemoryDrafts
    from test_food_agent_flow import Finder, Script
    clock = _clock()
    recaller, _ = _recaller([_row(**BK)], clock=clock)
    drafts = MemoryDrafts(clock=clock)
    script = Script()
    agent = FoodAgent(drafts, script, Finder(), recaller, clock)
    script.push({"type": "recall_meal", "query": "ahi tuna bowl", "when_hint": "last time"})
    result = await agent.turn("I had an ahi tuna bowl again, the one I had last time", session_ref="c")
    return agent, drafts, result


async def test_coordinator_not_found_logs_nothing_and_never_names_the_wrap():
    agent, drafts, result = await _offline_not_found_turn()
    assert result.preview is None and result.receipt is None
    assert "burger" not in result.reply.casefold() and "wrap" not in result.reply.casefold()
    assert "logged" not in result.reply.casefold()
    await agent.turn("yes", session_ref="c")
    assert drafts.entries == {}


async def test_coordinator_not_found_reply_is_honest():
    _, _, result = await _offline_not_found_turn()
    assert "couldn't find" in result.reply.casefold() and "ahi tuna bowl" in result.reply.casefold()


# ---------------------------------------------------------------- real Postgres

ADMIN_URL = os.environ.get("MMMACROS_TEST_PG_ADMIN_URL", "")
pg = pytest.mark.skipif(not ADMIN_URL, reason="set MMMACROS_TEST_PG_ADMIN_URL to a throwaway local PostgreSQL")


@pytest.fixture
async def pg_env(monkeypatch):
    if not ADMIN_URL:
        pytest.skip("no PostgreSQL")
    import asyncpg
    import migrations
    from store import Store
    for flag in ("CALORIE_ONLY_WRITES_ENABLED", "CALORIE_ONLY_WRITES_USER_IDS"):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    monkeypatch.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    from test_food_agent_pg import _db_url
    name = "mmm_food_recall_" + uuid4().hex[:12]
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    url = _db_url(name)
    await migrations.apply_migrations(url)
    conn = await asyncpg.connect(url)
    for held in ("003_calorie_only_unknown_macros.sql", "004_meal_drafts.sql"):
        await conn.execute((migrations.MIGRATIONS_DIR / held).read_text())
    users = [uuid4(), uuid4()]
    for user in users:
        await conn.execute("INSERT INTO users(id, display_name) VALUES($1, 'Synthetic test user')", user)
    await conn.close()

    async def connect(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(url, min_size=1, max_size=6)
        return self.pool
    monkeypatch.setattr(Store, "connect", connect)
    store = Store(url)
    yield {"store": store, "users": users, "url": url}
    await store.aclose()
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()


def _today() -> date:
    import domain
    return domain.effective_date(datetime.now(timezone.utc))


async def _seed(store, user, name, day, *, protein=40.0, calories=550.0, macro_source="Poke Spot menu"):
    from test_food_agent_pg import As
    with As(user):
        row = await store.insert_meal(name=name, meal="Lunch", calories=calories, protein=protein, carbs=60.0,
                                      fat=15.0, fiber=5.0, day=day, macro_source=macro_source)
    return str(row["id"])


async def _count(url, sql="SELECT 1 FROM nutrition_entries", *args):
    from test_food_agent_pg import _rows
    return len(await _rows(url, sql, *args))


@pg
async def test_pg_search_is_owner_scoped(pg_env):
    from food_agent.recall import HistoryRecaller
    from test_food_agent_pg import As
    store, (alice, bob) = pg_env["store"], pg_env["users"]
    day = _today() - timedelta(days=3)
    alice_id = await _seed(store, alice, "Ahi Tuna Bowl", day, protein=40.0)
    bob_id = await _seed(store, bob, "Ahi Tuna Bowl", day + timedelta(days=1), protein=99.0)
    with As(alice):
        rows = await store.search_own_entries(["ahi"], since=day - timedelta(days=5))
        assert [r["id"] for r in rows] == [alice_id]
        found = await HistoryRecaller(store).recall("ahi tuna bowl", None)
        assert found.status == "found" and found.source_entry_id == alice_id
        assert bob_id not in json.dumps([i.model_dump(mode="json") for i in found.items])
        assert found.items[0].resolved.panel.protein == 40.0
    carol = uuid4()  # a user with no rows at all
    with As(carol):
        assert await store.search_own_entries(["ahi"], since=day - timedelta(days=5)) == []
        assert (await HistoryRecaller(store).recall("ahi tuna bowl", None)).status == "not_found"
    await _seed(store, bob, "Grande Iced Latte", day, protein=8.0)
    with As(bob):
        found = await HistoryRecaller(store).recall("ahi tuna bowl", None)
        assert found.source_entry_id == bob_id and alice_id not in json.dumps(
            [i.model_dump(mode="json") for i in found.items])


@pg
async def test_pg_search_escapes_like_wildcards_and_respects_until(pg_env):
    from test_food_agent_pg import As
    store, (alice, _) = pg_env["store"], pg_env["users"]
    day = _today() - timedelta(days=2)
    await _seed(store, alice, "Ahi Tuna Bowl", day)
    with As(alice):
        assert await store.search_own_entries(["%"], since=day - timedelta(days=1)) == []
        assert await store.search_own_entries(["_hi"], since=day - timedelta(days=1)) == []
        assert await store.search_own_entries([], since=day - timedelta(days=1)) == []
        assert await store.search_own_entries(["ahi"], since=day + timedelta(days=1)) == []
        assert await store.search_own_entries(["ahi"], since=day - timedelta(days=9),
                                              until=day - timedelta(days=1)) == []
        (row,) = await store.search_own_entries(["TUNA"], since=day, until=day)
        assert row["name"] == "Ahi Tuna Bowl" and row["protein"] == 40.0


def _pg_agent(store):
    from food_agent.coordinator import FoodAgent
    from food_agent.drafts import StoreDrafts
    from food_agent.recall import HistoryRecaller
    from test_food_agent_flow import Finder, Script
    script = Script()
    return FoodAgent(StoreDrafts(store), script, Finder(), HistoryRecaller(store),
                     lambda: datetime.now(timezone.utc)), script


@pg
async def test_pg_real_failure_1_dialogue_recalls_own_tuna_bowl(pg_env):
    from test_food_agent_pg import As, _rows
    store, (alice, bob) = pg_env["store"], pg_env["users"]
    today = _today()
    tuna_id = await _seed(store, alice, "Ahi Tuna Bowl", today - timedelta(days=4), protein=40.0)
    await _seed(store, alice, "Burger King Royal Crispy Wrap (2)", today, protein=30.0, calories=620.0,
                macro_source="Burger King nutrition")
    await _seed(store, bob, "Ahi Tuna Bowl", today - timedelta(days=1), protein=99.0)
    before = await _count(pg_env["url"])
    agent, script = _pg_agent(store)
    with As(alice):
        script.push({"type": "recall_meal", "query": "ahi tuna bowl", "when_hint": "last time"})
        shown = await agent.turn("I had an ahi tuna bowl again, the one I had last time", session_ref="pg")
        assert shown.preview is not None and shown.receipt is None
        assert shown.preview.lines[0].display_name == "Ahi Tuna Bowl"
        assert shown.preview.lines[0].nutrients.protein == 40.0  # Alice's, not Bob's 99
        assert "burger" not in shown.reply.casefold()
        assert await _count(pg_env["url"]) == before
        done = await agent.turn("yes", session_ref="pg")
    assert done.receipt is not None and done.receipt.verified and done.receipt.name == "Ahi Tuna Bowl"
    assert await _count(pg_env["url"]) == before + 1
    new = await _rows(pg_env["url"], "SELECT * FROM nutrition_entries WHERE id=$1", done.receipt.entry_id)
    assert new[0]["user_id"] == alice and new[0]["name"] == "Ahi Tuna Bowl" and float(new[0]["protein"]) == 40.0
    meta = json.loads(new[0]["component_metadata"])
    assert meta["items"][0]["food_id"] == f"history:{tuna_id}"


async def _pg_not_found_turn(pg_env):
    from test_food_agent_pg import As
    store, (alice, bob) = pg_env["store"], pg_env["users"]
    today = _today()
    await _seed(store, alice, "Burger King Royal Crispy Wrap (2)", today, protein=30.0, calories=620.0,
                macro_source="Burger King nutrition")
    await _seed(store, bob, "Ahi Tuna Bowl", today - timedelta(days=1))  # someone else's tuna
    before = await _count(pg_env["url"])
    agent, script = _pg_agent(store)
    with As(alice):
        script.push({"type": "recall_meal", "query": "ahi tuna bowl", "when_hint": "last time"})
        result = await agent.turn("I had an ahi tuna bowl again, the one I had last time", session_ref="pg")
        again = await agent.turn("yes", session_ref="pg")
    return before, result, again


@pg
async def test_pg_no_tuna_history_writes_nothing_and_never_names_the_wrap(pg_env):
    before, result, again = await _pg_not_found_turn(pg_env)
    assert result.preview is None and result.receipt is None and again.receipt is None
    reply = result.reply.casefold()
    assert "burger" not in reply and "wrap" not in reply and "logged" not in reply
    assert await _count(pg_env["url"]) == before


@pg
async def test_pg_no_tuna_history_reply_is_honest(pg_env):
    _, result, _ = await _pg_not_found_turn(pg_env)
    reply = result.reply.casefold()
    assert "couldn't find" in reply and "ahi tuna bowl" in reply
