from uuid import uuid4
import pytest
from agent_canvas import CanvasService
from auth import bind_user, reset_user
from test_agent_canvas import MemoryStore


@pytest.mark.asyncio
async def test_refresh_rebinds_logger_after_existing_plan_editor_changes_exercise():
    name = 'Fixture press'
    async def today(_):
        return {'has_plan': True, 'today_type': 'Push', 'done': False,
                'exercises': [{'name': name, 'sets': 3, 'reps': '8', 'rest_sec': 60}]}
    service = CanvasService(store_factory=MemoryStore, food_factory=lambda: {},
                            coach_factory=lambda: {'get_today_session': today})
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        await service.snapshot(sid, create=True)
        before = await service.action(sid, {'action': 'start_workout'})
        name = 'Fixture fly'
        after = await service.action(sid, {'action': 'refresh'})
        assert after['revision'] > before['revision']
        assert after['workout']['exercises'][0]['name'] == name
        logger = next(c for c in after['surfaces'][0]['components'] if c['component'] == 'SetLogger')
        assert logger['reference'] == after['workout']['activeExerciseId']
        await service.action(sid, {'action': 'quiet'})
        assert (await service.action(sid, {'action': 'refresh'}))['surfaces'] == []
    finally:
        reset_user(scope)
