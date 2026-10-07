"""Preview context CAS regressions, with synthetic tenant-partitioned stores."""
from copy import deepcopy
from uuid import uuid4

import pytest

from auth import bind_user, reset_user
from test_today_workout_plan import CanvasDayStore, PULL, _service
import domain


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["routine", "completion", "sets"])
async def test_intervening_context_change_refuses_old_preview(change):
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        await service._propose_today_workout(session, deepcopy(PULL))
        if change == "routine":
            store.plan["days"]["Push"]["exercises"][0]["sets"] = 5
        elif change == "completion":
            store.state = {"rotation_index": 1, "done_date": domain.effective_date()}
        else:
            # Even adding sets to an exercise retained by the proposal changes
            # what was previewed; the new completed state must be shown first.
            store.workouts.append({"id": "new", "exercise": "Lat Pulldown",
                "date": domain.effective_date().isoformat(), "sets": [{"weight": 80, "reps": 8}]})
        with pytest.raises(ValueError, match="changed|fresh preview"):
            await service.action(session.canvas.session_id, {"action": "confirm", "reference": session.approval["id"]})
        assert store.day_plan_writes == []
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_store_rechecks_logging_day_after_waiting_for_tenant_lock(monkeypatch):
    import store as store_module
    from datetime import timedelta
    from workout_store_fixture import WorkoutTransactionPool
    day = domain.effective_date()
    events = []

    class Connection(WorkoutTransactionPool):
        async def execute(self, sql, *args):
            await super().execute(sql, *args)
            events.append("locked")
            monkeypatch.setattr(store_module, "effective_date", lambda: day + timedelta(days=1))

        async def fetchval(self, *args):
            raise AssertionError("expired preview must not snapshot or write")

        async def fetchrow(self, *args):
            raise AssertionError("expired preview must not write")

    scope = bind_user(uuid4())
    try:
        store = store_module.Store("postgresql://unused")
        assert await store.compare_and_swap_day_workout_plan(
            day, None, "Pull", [], "old-day", expected_context={}, connection=Connection()) is None
    finally:
        reset_user(scope)
    assert events == ["locked"]
