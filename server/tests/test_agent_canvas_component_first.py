"""Product regression tests; production handlers, offline tenant store."""
from uuid import uuid4
from copy import deepcopy
import pytest
from auth import bind_user, reset_user, current_user_id
from agent_canvas import CanvasService, validate_surface, validate_action
from test_agent_canvas import MemoryStore

class SetupStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.users = {}
    def row(self):
        return self.users.setdefault(current_user_id(), {"name": None, "metrics": {}, "targets": None, "plan": None})
    async def get_display_name(self): return self.row()["name"]
    async def get_metrics(self): return self.row()["metrics"]
    async def fetch_targets(self, *_): return self.row()["targets"]
    async def fetch_workout_plan(self): return self.row()["plan"]
    async def fetch_workout_library(self): return [{"id": "fixture", "name": "Fixture Squat"}]
    async def put_display_name(self, name): self.row()["name"] = name; return {"display_name": name}
    async def put_metrics(self, values): self.row()["metrics"] = values; return values
    async def insert_targets(self, day, values): self.row()["targets"] = values; return values
    async def put_workout_plan(self, plan): self.row()["plan"] = plan; return plan
    async def compare_and_swap_workout_plan(self, before, after):
        if self.row()["plan"] != before: return None
        self.row()["plan"] = deepcopy(after); return after

@pytest.fixture
def setup_service(monkeypatch):
    import server as srv
    store = SetupStore()
    monkeypatch.setattr(srv, 'store_client', lambda: store)
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {}, coach_factory=lambda: srv._coach_tool_handlers(canvas=True))
    token = bind_user(uuid4())
    yield service, store
    reset_user(token)

@pytest.mark.asyncio
async def test_agent_composes_plan_preview_without_writing_and_confirm_is_scoped(setup_service):
    service, store = setup_service
    observed = []
    async def agent(**kw):
        observed.append(kw['handlers'])
        await kw['handlers']['present_surface']({'view':'plan'})
        return 'Sample ready.', []
    service.agent = agent
    sid = str(uuid4())
    result = await service.turn(sid, str(uuid4()), 'make me a fake workout', adapter='text')
    assert 'WorkoutPlanPreview' in kinds(result)
    assert any(c.get('rows') for s in result['surfaces'] for c in s['components'] if c['component']=='WorkoutPlanPreview')
    assert store.row()['plan'] is None
    assert {'set_display_name','set_metrics','request_metrics_form','set_targets','set_workout_plan'} <= observed[0].keys()
    assert [s['surfaceId'] for s in result['surfaces']] == ['approval'], 'The requested preview must replace setup, not be buried below forms'
    approval = result['approval']['id']
    other = bind_user(uuid4())
    try:
        with pytest.raises(ValueError): await service.action(sid, {'action':'confirm','reference':approval})
        assert store.row()['plan'] is None
    finally: reset_user(other)
    await service.action(sid, {'action':'confirm','reference':approval})
    assert store.row()['plan']['days']['Full Body']['exercises'][0]['name']=='Fixture Squat'
    with pytest.raises(ValueError): await service.action(sid, {'action':'confirm','reference':approval})

@pytest.mark.asyncio
async def test_structured_metrics_targets_and_name_use_validated_confirmed_handlers(setup_service):
    service, store = setup_service
    sid = str(uuid4())
    await service.snapshot(sid, create=True)
    with pytest.raises(ValueError):
        await service.action(sid, {'action':'submit_metrics','numbers':{'height_cm':1,'weight_kg':80,'goal_weight_kg':75}})
    for action, numbers, field in [
        ('submit_metrics', {'height_cm':180,'weight_kg':80,'goal_weight_kg':75}, 'metrics'),
        ('submit_targets', {'calories':2000,'protein':140,'carbs':200,'fat':60,'fiber':25}, 'targets'),
    ]:
        result = await service.action(sid, {'action':action,'numbers':numbers})
        assert not store.row()[field]
        await service.action(sid, {'action':'confirm','reference':result['approval']['id']})
        assert store.row()[field] == numbers
    async def agent(**kw):
        await kw['handlers']['set_display_name']({'name':'Fixture'})
        return 'Review name.', []
    service.agent=agent
    result=await service.turn(sid,str(uuid4()),'Call me Fixture',adapter='text')
    assert store.row()['name'] is None
    await service.action(sid, {'action':'confirm','reference':result['approval']['id']})
    assert store.row()['name']=='Fixture'
    assert 'SetupChecklist' in kinds(await service.snapshot(sid))

@pytest.mark.asyncio
async def test_text_only_provider_cannot_drop_workout_component_request(setup_service):
    service, store=setup_service
    async def agent(**kw): return 'Sure, here is a workout.', []
    service.agent=agent
    result=await service.turn(str(uuid4()),str(uuid4()),'make me a fake workout',adapter='text')
    assert 'WorkoutPlanPreview' in kinds(result)
    assert store.row()['plan'] is None

@pytest.mark.parametrize('action', ['raw_profile_write','onboarding_execute'])
def test_unknown_setup_actions_rejected(action):
    with pytest.raises(ValueError): validate_action({'action':action})

def test_setup_rows_and_action_payloads_are_closed():
    with pytest.raises(ValueError):
        validate_surface({'surfaceId':'task','lifecycle':'task','components':[{'id':'setup','component':'SetupChecklist','rows':[{'label':'Bad','detail':'Bad','url':'https://untrusted.invalid'}]}]})
    with pytest.raises(ValueError):
        validate_action({'action':'submit_metrics','numbers':{'user_id':123}})
    with pytest.raises(ValueError):
        validate_surface({'surfaceId':'task','lifecycle':'task','components':[{'id':'setup','component':'OnboardingScript'}]})

def test_authenticated_root_stays_canvas_and_setup_components_are_native():
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    content=(root/'ios/MacroTracker/ContentView.swift').read_text().split('struct ProgressDashboardView')[0]
    assert 'ChatLogView' not in content
    assert 'AgentCanvasView(canvas: store.canvas)' in content
    assert 'switch loader.route' not in content
    renderer=(root/'ios/MacroTracker/Views/AgentSurfaceRenderer.swift').read_text()
    assert 'case .setupChecklist' in renderer and 'case .workoutPlanPreview' in renderer
    assert 'submitMetrics' in renderer and 'submitTargets' in renderer

@pytest.mark.asyncio
async def test_real_agent_dispatches_closed_setup_composition(setup_service, monkeypatch):
    import httpx, json
    from coach import run_agent
    service, store = setup_service
    monkeypatch.setenv('OPENAI_ACCESS_TOKEN', 'fixture-only')
    payloads=[]
    async def post(token, payload):
        payloads.append(payload)
        message = {'content': 'Your setup cards are ready.'} if len(payloads)>1 else {
            'content': None, 'tool_calls': [{'id':'fixture-call','type':'function','function':{
                'name':'present_surface','arguments':json.dumps({'view':'setup','components':['SetupChecklist','ProfileMetrics','TargetStatus','WorkoutPlanPreview']})}}]}
        return httpx.Response(200,request=httpx.Request('POST','https://fixture.invalid'),json={'choices':[{'message':message}]})
    async def agent(**kw): return await run_agent(**kw, post=post)
    service.agent=agent
    result=await service.turn(str(uuid4()),str(uuid4()),'Help me set up',adapter='text')
    assert {'SetupChecklist','ProfileMetrics','TargetStatus','WorkoutPlanPreview'} <= kinds(result)
    assert len(payloads)==2
    tools={t['function']['name']:t['function'] for t in payloads[0]['tools']}
    assert tools['set_targets']['parameters']['properties']=={}
    assert 'confirm' not in tools and 'log_workout' not in tools

@pytest.mark.asyncio
async def test_first_plan_store_insert_is_tenant_scoped_and_never_overwrites():
    from store import Store
    import json
    seen=[]
    class Pool:
        async def fetchval(self,sql,*args):
            seen.append((sql,args))
            assert 'ON CONFLICT(user_id) DO NOTHING' in sql
            assert args[0]==current_user_id()
            return args[1] if len(seen)==1 else None
    store=Store('postgresql://fixture.invalid/fixture')
    async def connect(): return Pool()
    store.connect=connect
    scope=bind_user(uuid4())
    try:
        assert await store.compare_and_swap_workout_plan(None,{'version':1})=={'version':1}
        assert await store.compare_and_swap_workout_plan(None,{'version':2}) is None
    finally: reset_user(scope)

@pytest.mark.asyncio
async def test_setup_uncertainty_and_stale_preview_cannot_be_retried(setup_service):
    service,store=setup_service
    sid=str(uuid4()); await service.snapshot(sid,create=True)
    values={'height_cm':180,'weight_kg':80,'goal_weight_kg':75}
    result=await service.action(sid,{'action':'submit_metrics','numbers':values})
    store.row()['metrics']={'height_cm':190}
    with pytest.raises(ValueError,match='changed'):
        await service.action(sid,{'action':'confirm','reference':result['approval']['id']})
    await service.action(sid,{'action':'cancel','reference':result['approval']['id']})
    result=await service.action(sid,{'action':'submit_metrics','numbers':values})
    async def uncertain(values): raise TimeoutError('fixture')
    store.put_metrics=uncertain
    with pytest.raises(TimeoutError):
        await service.action(sid,{'action':'confirm','reference':result['approval']['id']})
    with pytest.raises(ValueError,match='uncertain'):
        await service.action(sid,{'action':'confirm','reference':result['approval']['id']})

def kinds(result):
    return {c['component'] for s in result['surfaces'] for c in s['components']}

@pytest.mark.asyncio
async def test_fresh_snapshot_is_native_setup_and_completion_keeps_session(setup_service):
    service, store = setup_service
    sid = str(uuid4())
    fresh = await service.snapshot(sid, create=True)
    assert {'SetupChecklist', 'ProfileMetrics', 'TargetStatus', 'WorkoutPlanPreview'} <= kinds(fresh)
    assert store.row()['plan'] is None
    store.row().update(name='Fixture', metrics={'height_cm':180, 'weight_kg':80, 'goal_weight_kg':75}, targets={'calories':2000}, plan={'version':1,'rotation':['Full Body'],'days':{'Full Body':{'label':'Full Body','exercises':[]}}})
    completed = await service.snapshot(sid)
    assert completed['sessionId'] == fresh['sessionId']
    assert 'SetupChecklist' not in kinds(completed)
    assert 'MacroProgress' in kinds(completed)
