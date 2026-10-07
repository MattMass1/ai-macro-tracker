from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent_canvas import CanvasService
from auth import bind_user, reset_user
from test_agent_canvas import MemoryStore


class PlanStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.plan = {"version": 1, "rotation": ["Push"], "days_per_week": 3,
                     "days": {"Push": {"label": "Push", "exercises": [
                         {"name": "Fixture press", "sets": 3, "reps": "8-10", "rest_sec": 90},
                         {"name": "Fixture fly", "sets": 2, "reps": "12", "rest_sec": 60}]}}}
        self.writes = []
        self.break_readback = False

    async def fetch_workout_plan(self):
        if self.break_readback and self.writes:
            raise RuntimeError("fixture readback failure")
        return deepcopy(self.plan)

    async def fetch_workout_library(self):
        return [{"name": "Cable Flyes"}, {"name": "Fixture press"}, {"name": "Fixture fly"}]

    async def put_workout_plan(self, plan):
        self.writes.append(deepcopy(plan))
        self.plan = deepcopy(plan)
        return deepcopy(plan)

    async def compare_and_swap_workout_plan(self, before, after):
        if self.plan != before:
            return None
        return await self.put_workout_plan(after)

    async def today(self, _):
        return {"today_type": "Push", "has_plan": True, "done": False,
                "exercises": self.plan["days"]["Push"]["exercises"]}


@pytest.mark.asyncio
async def test_swap_requires_owned_draft_and_confirm_then_verified_readback():
    store = PlanStore()
    async def agent(**kwargs):
        result = await kwargs["handlers"]["request_exercise_swap"]({"target": "last", "replacement": "Cable Flyes"})
        assert result["status"] == "awaiting_confirmation"
        return "Review the replacement.", []
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": store.today}, agent=agent)
    sid = str(uuid4())
    scope = bind_user(uuid4())
    try:
        response = await service.turn(sid, str(uuid4()), "Replace last exercise with Cable Flyes", adapter="text")
        assert not store.writes
        draft = response["approval"]["id"]
        await service.action(sid, {"action": "quiet"})
        assert (await service.snapshot(sid))["surfaces"][0]["lifecycle"] == "approval"
        with pytest.raises(ValueError):
            await service.action(sid, {"action": "confirm", "reference": "forged"})
        response = await service.action(sid, {"action": "confirm", "reference": draft})
        assert response["approval"] is None
        assert len(store.writes) == 1
        assert store.plan["days"]["Push"]["exercises"][0]["name"] == "Fixture press"
        assert store.plan["days"]["Push"]["exercises"][1]["name"] == "Cable Flyes"
        assert store.plan["days"]["Push"]["exercises"][1]["rest_sec"] == 60
        with pytest.raises(ValueError):
            await service.action(sid, {"action": "confirm", "reference": draft})
        assert len(store.writes) == 1
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_plan_compare_and_swap_is_atomic_and_tenant_scoped():
    from store import Store
    from workout_store_fixture import WorkoutTransactionPool
    captured = []
    class Pool(WorkoutTransactionPool):
        async def fetchval(self, sql, *args):
            captured.append((sql, args))
            return None
    store = Store("postgresql://fixture.invalid/not-used")
    async def connect():
        return Pool()
    store.connect = connect
    user = uuid4()
    scope = bind_user(user)
    try:
        assert await store.compare_and_swap_workout_plan({"before": True}, {"after": True}) is None
    finally:
        reset_user(scope)
    sql, args = captured[0]
    assert "WHERE user_id=$1 AND plan=$2::jsonb" in sql
    assert "RETURNING plan" in sql
    assert args[0] == user


@pytest.mark.asyncio
async def test_uncertain_confirmation_publishes_new_revision_and_never_rewrites():
    store = PlanStore()
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": store.today})
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        session = service.session(sid, create=True)
        await service._request_swap(session, {"target": "last", "replacement": "Cable Flyes"})
        before = await service.snapshot(sid)
        store.break_readback = True
        intent = {"action": "confirm", "reference": before["approval"]["id"]}
        with pytest.raises(RuntimeError):
            await service.action(sid, intent)
        after = await service.snapshot(sid)
        assert after["revision"] > before["revision"]
        assert after["approval"]["status"] == "uncertain"
        with pytest.raises(ValueError, match="uncertain"):
            await service.action(sid, intent)
        assert len(store.writes) == 1
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_plan_edit_previews_exact_target_then_native_confirm_preserves_metadata():
    store = PlanStore()
    store.plan["future_contract"] = {"keep": [1, "two"]}
    store.plan["days"]["Push"]["note"] = "unchanged"

    async def agent(**kwargs):
        result = await kwargs["handlers"]["request_plan_edit"]({
            "day": "Push", "exercise": "Fixture press", "sets": 4, "reps": "6-8"
        })
        return result["detail"], []

    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": store.today}, agent=agent)
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        response = await service.turn(sid, str(uuid4()), "Make Fixture press 4 sets of 6-8", adapter="voice")
        assert store.writes == []
        assert response["approval"]["status"] == "pending"
        draft = response["approval"]["id"]
        await service.action(sid, {"action": "confirm", "reference": draft})
        assert store.plan["days"]["Push"]["exercises"][0]["sets"] == 4
        assert store.plan["days"]["Push"]["exercises"][0]["reps"] == "6-8"
        assert store.plan["days"]["Push"]["exercises"][0]["rest_sec"] == 90
        assert store.plan["days"]["Push"]["exercises"][1]["name"] == "Fixture fly"
        assert store.plan["days"]["Push"]["note"] == "unchanged"
        assert store.plan["future_contract"] == {"keep": [1, "two"]}
        with pytest.raises(ValueError, match="no longer available"):
            await service.action(sid, {"action": "confirm", "reference": draft})
        assert len(store.writes) == 1
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_python_plan_edit_serializer_matches_shared_native_fixture():
    fixture = json.loads((Path(__file__).resolve().parents[2]
                          / "docs/agent-canvas/fixtures/plan-edit.json").read_text())
    store = PlanStore()
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": store.today})
    scope = bind_user(uuid4())
    try:
        session = service.session(fixture["sessionId"], create=True)
        await service._request_plan_edit(session, {
            "day": "Push", "exercise": "Fixture press", "sets": 4, "reps": "6-8",
        })
        actual = await service.snapshot(fixture["sessionId"])
        draft_id = actual["approval"]["id"]
        actual["instanceId"] = fixture["instanceId"]
        actual["serverTime"] = fixture["serverTime"]
        actual["approval"]["id"] = fixture["approval"]["id"]
        for component in actual["surfaces"][0]["components"]:
            if component.get("reference") == draft_id:
                component["reference"] = fixture["approval"]["id"]
            for action in component.get("actions", []):
                if action.get("reference") == draft_id:
                    action["reference"] = fixture["approval"]["id"]
        assert actual == fixture
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_plan_edit_ambiguity_and_cancel_never_write():
    store = PlanStore()
    store.plan["rotation"].append("Pull")
    store.plan["days"]["Pull"] = deepcopy(store.plan["days"]["Push"])
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": store.today})
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        session = service.session(sid, create=True)
        result = await service._request_plan_edit(session, {"exercise": "Fixture press", "sets": 4})
        assert result == {"status": "needs_clarification",
                          "question": "Fixture press appears on Push, Pull. Which plan day should I edit?"}
        assert store.writes == [] and session.approval is None
        result = await service._request_plan_edit(session, {
            "day": "Push", "exercise": "Fixture press", "replacement": "Cable Flyes"
        })
        draft = session.approval["id"]
        assert result["status"] == "awaiting_confirmation" and store.writes == []
        await service.action(sid, {"action": "cancel", "reference": draft})
        assert store.writes == [] and store.plan["days"]["Push"]["exercises"][0]["name"] == "Fixture press"
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_plan_edit_stale_preview_never_overwrites_newer_plan():
    store = PlanStore()
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": store.today})
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        session = service.session(sid, create=True)
        await service._request_plan_edit(session, {"day": "Push", "exercise": "Fixture press", "sets": 4})
        draft = session.approval["id"]
        store.plan["external_change"] = True
        with pytest.raises(ValueError, match="plan changed"):
            await service.action(sid, {"action": "confirm", "reference": draft})
        assert store.writes == []
        assert store.plan["days"]["Push"]["exercises"][0]["sets"] == 3
    finally:
        reset_user(scope)
