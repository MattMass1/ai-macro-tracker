from copy import deepcopy
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
    captured = []
    class Pool:
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
