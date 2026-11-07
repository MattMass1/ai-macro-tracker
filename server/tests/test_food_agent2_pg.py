"""food_agent2 against a real throwaway PostgreSQL. Fresh synthetic users, fake model and providers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from food_agent.drafts import StoreDrafts
from food_agent2.contracts import Waiting
from food_agent2.retrieval import Retrieval
from food_agent2.turn import FoodAgent2

from test_food_agent2_flow import CLUB, Script, add, club_sources, portion, revise
from test_food_agent_pg import As, _rows, env, pg  # noqa: F401


def agent_for(store, sources=None):
    script = Script()
    now = lambda: datetime.now(timezone.utc)  # noqa: E731 - the store stamps updated_at with now()
    return FoodAgent2(StoreDrafts(store), script, Retrieval(sources or club_sources()), None, now), script


@pg
async def test_preview_yes_saves_one_row_with_the_read_back_receipt(env):
    store, (alice, _bob) = env["store"], env["users"]
    agent, script = agent_for(store)
    with As(alice):
        script.push(add(CLUB))
        preview = await agent.turn("club", session_ref="canvas:1")
        assert preview.preview is not None
        done = await agent.turn("yes", session_ref="canvas:1")
    assert done.receipt.verified
    rows = await _rows(env["url"], "SELECT user_id,calories,macro_source FROM nutrition_entries")
    assert len(rows) == 1 and rows[0]["user_id"] == alice and float(rows[0]["calories"]) == 520


@pg
async def test_provider_panel_never_reaches_the_draft_row(env):
    store, (alice, _bob) = env["store"], env["users"]
    agent, script = agent_for(store)
    with As(alice):
        script.push(add(CLUB))
        await agent.turn("club", session_ref="canvas:1")
    rows = await _rows(env["url"], "SELECT resolution::text AS body FROM meal_draft_items")
    assert '"panel": {}' in rows[0]["body"]
    assert "520" not in rows[0]["body"]


@pg
async def test_waiting_state_is_stored_inside_the_database_limit(env):
    store, (alice, _bob) = env["store"], env["users"]
    agent, script = agent_for(store)
    with As(alice):
        script.push(add(CLUB))
        await agent.turn("club", session_ref="canvas:1")
    rows = await _rows(env["url"], "SELECT pending_question FROM meal_drafts")
    assert Waiting.read(rows[0]["pending_question"]).stage == "preview"
    assert len(rows[0]["pending_question"]) <= 1000


@pg
async def test_one_users_yes_never_confirms_anothers_preview(env):
    store, (alice, bob) = env["store"], env["users"]
    agent, script = agent_for(store)
    with As(alice):
        script.push(add(CLUB))
        await agent.turn("club", session_ref="canvas:1")
    with As(bob):
        script.push({"type": "confirm"})
        result = await agent.turn("yes", session_ref="canvas:1")
        context = script.calls[-1][1]
    assert result.receipt is None and context["open_drafts"] == []
    assert await _rows(env["url"], "SELECT id FROM nutrition_entries") == []


@pg
async def test_lost_commit_response_reconciles_to_one_row(env):
    store, (alice, _bob) = env["store"], env["users"]
    agent, script = agent_for(store)
    real = store.commit_meal_draft

    async def lost(*args, **kwargs):
        await real(*args, **kwargs)
        raise ConnectionError("synthetic: committed, response lost")

    with As(alice):
        script.push(add(CLUB))
        await agent.turn("club", session_ref="canvas:1")
        store.commit_meal_draft = lost
        first = await agent.turn("yes", session_ref="canvas:1")
        store.commit_meal_draft = real
        second = await agent.turn("yes", session_ref="canvas:1")
    assert first.outcome.effect == "unknown"
    assert second.receipt.verified
    assert len(await _rows(env["url"], "SELECT id FROM nutrition_entries")) == 1


@pg
async def test_amending_keeps_the_same_row_and_every_food(env):
    store, (alice, _bob) = env["store"], env["users"]
    agent, script = agent_for(store)
    with As(alice):
        script.push(add(CLUB))
        await agent.turn("club", session_ref="canvas:1")
        await agent.turn("yes", session_ref="canvas:1")
        script.push(revise("i1", new_portion=portion("serving_fraction", 0.5, text="half")))
        preview = await agent.turn("actually half", session_ref="canvas:1")
        assert preview.reply.endswith("Save the change?")
        done = await agent.turn("yes", session_ref="canvas:1")
    assert done.reply.startswith("Updated ")
    rows = await _rows(env["url"], "SELECT calories,entry_revision FROM nutrition_entries")
    assert len(rows) == 1 and float(rows[0]["calories"]) == 260 and rows[0]["entry_revision"] == 2


@pg
async def test_cancelled_draft_writes_nothing(env):
    store, (alice, _bob) = env["store"], env["users"]
    agent, script = agent_for(store)
    with As(alice):
        script.push(add(CLUB))
        await agent.turn("club", session_ref="canvas:1")
        assert (await agent.turn("no", session_ref="canvas:1")).reply == "OK, not logged."
    assert await _rows(env["url"], "SELECT id FROM nutrition_entries") == []
