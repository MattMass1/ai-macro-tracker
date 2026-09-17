"""Closed presentation composition, independent of data-processing tools."""
from uuid import uuid4
import pytest
from agent_canvas import CanvasService
from auth import bind_user, reset_user
from test_agent_canvas import MemoryStore


@pytest.mark.asyncio
async def test_agent_can_order_and_hide_workout_components_but_not_inject_a_write():
    attempts = []
    async def today(_):
        return {"has_plan": True, "today_type": "Push", "done": False,
                "exercises": [{"name": "Fixture press", "sets": 3, "reps": "8", "rest_sec": 60}]}
    async def agent(**kwargs):
        handler = kwargs['handlers']['present_surface']
        try:
            await handler({"view": "workout", "components": ["ActiveExercise", "SetLogger"]})
            attempts.append(True)
        except ValueError:
            attempts.append(False)
        for invalid in (["ConfirmationCard"], ["WebView"], ["SetLogger", "SetLogger"], []):
            with pytest.raises(ValueError):
                await handler({"view": "workout", "components": invalid})
        return "Ready", []
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": today}, agent=agent)
    scope = bind_user(uuid4())
    try:
        result = await service.turn(str(uuid4()), str(uuid4()), "Show only my exercise and logger", adapter="text")
        assert attempts == [True]
        assert [c['component'] for c in result['surfaces'][0]['components']] == ['ActiveExercise', 'SetLogger']
        assert result['surfaces'][0]['components'][1]['actions'][0]['reference'] == result['workout']['activeExerciseId']
    finally:
        reset_user(scope)
