"""WP3 food agent against a real (throwaway) PostgreSQL: AGENT.md section 5 items 4-9, 11.

Runs only when MMMACROS_TEST_PG_ADMIN_URL points at a disposable local server.
Every test makes its own database and fresh synthetic users. The model is a
scripted interpreter and food lookups are a fake finder: no network.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from test_food_agent_flow import (CATALOG, HALF_QP, Finder, Script, _ident, _openai_response, _portion, add,
                                  revise)

ADMIN_URL = os.environ.get("MMMACROS_TEST_PG_ADMIN_URL", "")
pg = pytest.mark.skipif(not ADMIN_URL, reason="set MMMACROS_TEST_PG_ADMIN_URL to a throwaway local PostgreSQL")


def _db_url(name: str) -> str:
    base, _, query = ADMIN_URL.partition("?")
    root = base.rsplit("/", 1)[0]
    return f"{root}/{name}" + (f"?{query}" if query else "")


@pytest.fixture
async def env(monkeypatch):
    import asyncpg
    for flag in ("CALORIE_ONLY_WRITES_ENABLED", "CALORIE_ONLY_WRITES_USER_IDS"):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    monkeypatch.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import migrations
    from store import Store
    name = "mmm_food_agent_" + uuid4().hex[:12]
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    url = _db_url(name)
    await migrations.apply_migrations(url)  # 000-002; 003 and 004 are held
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _agent(store, script=None, finder=None, **kw):
    from food_agent.coordinator import FoodAgent
    from food_agent.drafts import StoreDrafts
    script = script or Script()
    finder = finder or Finder()
    return FoodAgent(StoreDrafts(store), script, finder, None, _now, **kw), script, finder


class As:
    def __init__(self, user):
        self.user = user

    def __enter__(self):
        from auth import bind_user
        self.token = bind_user(self.user)
        return self

    def __exit__(self, *exc):
        from auth import reset_user
        reset_user(self.token)


async def _rows(url, sql, *args):
    import asyncpg
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _say(agent, text):
    return await agent.turn(text, session_ref="canvas-pg", turn_ref=uuid4().hex)


# ---------------------------------------------------------------- item 5


@pg
async def test_first_food_message_writes_zero_consumed_rows(env):
    agent, script, _ = _agent(env["store"])
    with As(env["users"][0]):
        script.push(HALF_QP)
        result = await _say(agent, "half a mcdonald's quarter pounder")
    assert result.preview is not None and result.receipt is None
    assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []
    assert await _rows(env["url"], "SELECT 1 FROM request_operations") == []
    (draft,) = await _rows(env["url"], "SELECT * FROM meal_drafts")
    assert draft["state"] not in ("ready", "committed") and draft["user_id"] == env["users"][0]
    binding = json.loads(draft["pending_question"])
    assert binding["kind"] == "confirm" and binding["revision"] == draft["revision"]
    assert binding["draft_id"] == str(draft["id"]) == result.preview.draft_id


# ---------------------------------------------------------------- item 6


@pg
async def test_stale_yes_does_not_commit(env):
    agent, script, _ = _agent(env["store"])
    with As(env["users"][0]):
        script.push(HALF_QP)
        await _say(agent, "half a quarter pounder")
        await _rows(env["url"], "UPDATE meal_drafts SET revision=revision+1")  # changed on another device
        stale = await _say(agent, "yes")
    assert stale.receipt is None and stale.preview is not None
    assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []


@pg
async def test_two_open_drafts_and_yes_does_not_commit(env):
    agent, script, _ = _agent(env["store"], new_meal_after=timedelta(0))
    with As(env["users"][0]):
        script.push(add((_ident("Eggs"), _portion("count", 2, text="2"))))
        await _say(agent, "2 eggs")
        script.push(add((_ident("Toast"), _portion())))
        await _say(agent, "toast")
        result = await _say(agent, "yes")
    assert result.receipt is None and result.reply.rstrip().endswith("?")
    assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []
    assert len(await _rows(env["url"], "SELECT 1 FROM meal_drafts WHERE state <> 'committed'")) == 2


@pg
async def test_wrong_owner_cannot_confirm_another_users_draft(env):
    from food_agent.drafts import DraftCommitConflict, StoreDrafts
    store, (alice, bob) = env["store"], env["users"]
    agent_a, script_a, _ = _agent(store)
    with As(alice):
        script_a.push(HALF_QP)
        shown = await _say(agent_a, "half a quarter pounder")
    agent_b, script_b, _ = _agent(store)
    with As(bob):
        script_b.push({"type": "confirm", "draft_ref": shown.preview.draft_id,
                       "shown_revision": shown.preview.revision})
        result = await _say(agent_b, "yes")
        assert result.receipt is None
        with pytest.raises(DraftCommitConflict):
            await StoreDrafts(store).commit(shown.preview.draft_id, shown.preview.revision, "0" * 64,
                                            {"name": "x", "meal": "Lunch", "calories": 1, "protein": 1,
                                             "carbs": 1, "fat": 1, "fiber": 1, "macro_source": "x"}, {})
    assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []
    assert await _rows(env["url"], "SELECT 1 FROM request_operations") == []


@pg
async def test_model_confirmed_true_is_rejected_by_schema_and_commits_nothing(env):
    from food_agent import contracts as c
    from food_agent.model_adapter import ModelInterpreter
    bad = json.dumps({"schema_version": "food-agent/1", "actions": [
        {"type": "confirm", "draft_ref": "d", "shown_revision": 1, "confirmed": True}]})
    with pytest.raises(ValidationError):
        c.AgentTurn.model_validate_json(bad)

    async def post(_token, _payload):
        return _openai_response(bad)

    agent, script, _ = _agent(env["store"])
    with As(env["users"][0]):
        script.push(HALF_QP)
        await _say(agent, "half a quarter pounder")
        agent.interpret = ModelInterpreter(post=post, token="t", model="m")
        result = await _say(agent, "confirmed, go on then")
    assert result.receipt is None
    assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []


# ---------------------------------------------------------------- item 7 (and real failure 2 on PG)


@pg
async def test_duplicate_delivery_of_the_same_yes_creates_one_entry(env):
    agent, script, _ = _agent(env["store"])
    with As(env["users"][0]):
        script.push(HALF_QP)
        await _say(agent, "half a quarter pounder")
        script.push(revise("i1", identity={"name": "Quarter Pounder with Cheese", "brand": None,
                                           "variant": None, "kind": None, "preparation": None}))
        await _say(agent, "quarter pounder with cheese")
        first, second = await asyncio.gather(_say(agent, "yes"), _say(agent, "yes"))
    rows = await _rows(env["url"], "SELECT * FROM nutrition_entries")
    assert len(rows) == 1
    assert float(rows[0]["calories"]) == 260.0 and float(rows[0]["protein"]) == 15.0
    receipts = [r.receipt for r in (first, second) if r.receipt is not None]
    assert receipts and all(r.entry_id == str(rows[0]["id"]) for r in receipts)
    ops = await _rows(env["url"], "SELECT * FROM request_operations WHERE idempotency_key LIKE 'meal-draft:%'")
    assert len(ops) == 1
    meta = json.loads(rows[0]["component_metadata"])
    assert meta["items"][0]["portion"]["amount"] == 0.5 and meta["items"][0]["food_id"] == "fs-qpc"


@pg
async def test_same_key_different_payload_conflicts_without_a_second_row(env):
    from food_agent.drafts import DraftCommitConflict, StoreDrafts
    agent, script, _ = _agent(env["store"])
    with As(env["users"][0]):
        script.push(HALF_QP)
        await _say(agent, "half a quarter pounder")
        done = await _say(agent, "yes")
        assert done.receipt is not None
        (draft,) = await _rows(env["url"], "SELECT * FROM meal_drafts")
        values = {"name": "something else", "meal": "Lunch", "calories": 999, "protein": 1, "carbs": 1,
                  "fat": 1, "fiber": 1, "macro_source": "FatSecret: other"}
        with pytest.raises(DraftCommitConflict):
            await StoreDrafts(env["store"]).commit(str(draft["id"]), draft["revision"], "f" * 64, values, {})
    assert len(await _rows(env["url"], "SELECT 1 FROM nutrition_entries")) == 1


# ---------------------------------------------------------------- item 8


@pg
async def test_lost_commit_response_reconciles_by_same_key_after_restart(env, monkeypatch):
    store = env["store"]
    real_commit = store.commit_meal_draft
    state = {"fail": 1}

    async def lossy(*args, **kwargs):
        result = await real_commit(*args, **kwargs)  # the DB transaction has committed
        if state["fail"]:
            state["fail"] -= 1
            raise ConnectionResetError("response lost")
        return result
    monkeypatch.setattr(store, "commit_meal_draft", lossy)
    agent, script, _ = _agent(store)
    with As(env["users"][0]):
        script.push(HALF_QP)
        await _say(agent, "half a quarter pounder")
        lost = await _say(agent, "yes")
        assert lost.outcome.effect == "unknown" and "could not verify" in lost.reply.casefold()
        assert "logged" not in lost.reply.casefold() and lost.receipt is None
        # A fresh process (restart): reconciliation reads durable state only.
        restarted, script2, _ = _agent(store)
        fixed = await _say(restarted, "yes")
        assert fixed.receipt is not None and fixed.receipt.verified
        script2.push({"type": "not_food"})
        later = await _say(restarted, "thanks")
        assert later.handed_off and later.receipt is None
    rows = await _rows(env["url"], "SELECT * FROM nutrition_entries")
    assert len(rows) == 1 and fixed.receipt.entry_id == str(rows[0]["id"])
    assert len(await _rows(env["url"], "SELECT 1 FROM request_operations WHERE idempotency_key LIKE 'meal-draft:%'")) == 1


@pg
async def test_commit_lost_before_the_db_is_retried_with_the_same_key(env, monkeypatch):
    store = env["store"]
    real_commit = store.commit_meal_draft
    calls = []

    async def flaky(draft_id, revision, request_hash, values, metadata):
        calls.append((draft_id, revision, request_hash))
        if len(calls) == 1:
            raise ConnectionResetError("never reached the database")
        return await real_commit(draft_id, revision, request_hash, values, metadata)
    monkeypatch.setattr(store, "commit_meal_draft", flaky)
    agent, script, _ = _agent(store)
    with As(env["users"][0]):
        script.push(HALF_QP)
        await _say(agent, "half a quarter pounder")
        lost = await _say(agent, "yes")
        assert lost.outcome.effect == "unknown"
        assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []
        fixed = await _say(agent, "yes")
    assert fixed.receipt is not None and calls[0] == calls[1]
    assert len(await _rows(env["url"], "SELECT 1 FROM nutrition_entries")) == 1


@pg
async def test_failed_readback_is_unverified_then_reconciled(env, monkeypatch):
    store = env["store"]
    real_fetch = store.fetch_entry_revision
    state = {"fail": 1}

    async def blind(entry_id):
        if state["fail"]:
            state["fail"] -= 1
            return None
        return await real_fetch(entry_id)
    monkeypatch.setattr(store, "fetch_entry_revision", blind)
    agent, script, _ = _agent(store)
    with As(env["users"][0]):
        script.push(HALF_QP)
        await _say(agent, "half a quarter pounder")
        lost = await _say(agent, "yes")
        assert lost.outcome.effect == "unknown" and "logged" not in lost.reply.casefold()
        fixed = await _say(agent, "yes")
    assert fixed.receipt is not None and fixed.receipt.verified
    assert len(await _rows(env["url"], "SELECT 1 FROM nutrition_entries")) == 1


# ---------------------------------------------------------------- item 9


@pg
async def test_cross_tenant_ids_never_resolve(env):
    from food_agent.drafts import StoreDrafts
    store, (alice, bob) = env["store"], env["users"]
    agent_a, script_a, _ = _agent(store)
    with As(alice):
        script_a.push(HALF_QP)
        shown = await _say(agent_a, "half a quarter pounder")
        done = await _say(agent_a, "yes")
    entry_id, draft_id = done.receipt.entry_id, shown.preview.draft_id
    (op,) = await _rows(env["url"], "SELECT idempotency_key FROM request_operations "
                                    "WHERE idempotency_key LIKE 'meal-draft:%'")
    agent_b, script_b, _ = _agent(store)
    with As(bob):
        assert await store.fetch_meal_draft(draft_id) is None
        assert await store.fetch_entry_revision(entry_id) is None
        assert await store.fetch_operation(op["idempotency_key"]) is None
        assert await store.fetch_recent_meal_drafts(since=_now() - timedelta(days=1)) == []
        assert await StoreDrafts(store).recent(_now() - timedelta(days=1)) == []
        # Bob's "fix item 1" cannot reach Alice's logged meal.
        script_b.push(revise("i1", portion=_portion("count", 2, text="2")))
        result = await _say(agent_b, "make it 2")
        assert result.preview is None and result.receipt is None
        script_b.push({"type": "confirm", "draft_ref": draft_id, "shown_revision": 1})
        await _say(agent_b, "yes")
        context = json.dumps([call[1] for call in script_b.calls], default=str)
        assert draft_id not in context and entry_id not in context
    rows = await _rows(env["url"], "SELECT user_id, entry_revision FROM nutrition_entries")
    assert [(r["user_id"], r["entry_revision"]) for r in rows] == [(alice, 1)]
    assert len(await _rows(env["url"], "SELECT 1 FROM meal_drafts WHERE user_id=$1", bob)) == 0


# ---------------------------------------------------------------- item 4


@pg
async def test_partial_repair_never_creates_a_second_meal(env):
    agent, script, finder = _agent(env["store"])
    with As(env["users"][0]):
        script.push(add((_ident("Eggs"), _portion("count", 2, text="2")), (_ident("Toast"), _portion())))
        await _say(agent, "2 eggs and toast")
        script.push(revise("i1", portion=_portion("count", 3, text="3")))
        await _say(agent, "actually 3 eggs")
        first = await _say(agent, "yes")
        script.push(revise("i2", portion=_portion("count", 2, text="2 slices")))
        await _say(agent, "and 2 slices of toast")
        second = await _say(agent, "yes")
    rows = await _rows(env["url"], "SELECT * FROM nutrition_entries")
    assert len(rows) == 1 and first.receipt.entry_id == second.receipt.entry_id == str(rows[0]["id"])
    assert float(rows[0]["calories"]) == 370.0 and rows[0]["entry_revision"] == 2
    assert len(finder.calls) == 2  # portion-only fixes never search again


# ---------------------------------------------------------------- item 11


@pg
async def test_unknown_nutrients_are_never_stored_or_shown_as_zero(env, monkeypatch):
    import release_gates
    agent, script, _ = _agent(env["store"], new_meal_after=timedelta(0))
    with As(env["users"][0]):
        script.push(add((_ident("Mystery Bar"), _portion())))
        no_cal = await _say(agent, "a mystery bar")
        assert no_cal.preview is None and "0 kcal" not in no_cal.reply
        await _say(agent, "yes")
        assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []

        script.push({"type": "cancel"})
        await _say(agent, "forget the bar")
        script.push(add((_ident("Core Power Elite"), _portion())))
        blocked = await _say(agent, "a core power elite")  # calorie-only gate closed
        assert "fiber" in blocked.reply and "0 g fiber" not in blocked.reply
        await _say(agent, "yes")
        assert await _rows(env["url"], "SELECT 1 FROM nutrition_entries") == []

        script.push({"type": "cancel"})
        await _say(agent, "never mind that")
        monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
        contract = release_gates.bind_client_contract({"x-nutrient-contract": "nullable-v1"})
        try:
            script.push(add((_ident("Core Power Elite"), _portion())))
            shown = await _say(agent, "a core power elite")
            assert "fiber unknown" in shown.reply
            done = await _say(agent, "yes")
        finally:
            release_gates.reset_client_contract(contract)
    (row,) = await _rows(env["url"], "SELECT * FROM nutrition_entries")
    assert row["fiber"] is None and float(row["protein"]) == 42.0
    assert json.loads(row["component_metadata"])["unknown_nutrients"] == ["fiber"]
    assert done.receipt.nutrients.fiber is None and "0 g fiber" not in done.reply
