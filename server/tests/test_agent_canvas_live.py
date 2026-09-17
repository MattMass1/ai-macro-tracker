import json
from uuid import uuid4

import pytest
from auth import bind_user, reset_user
from agent_canvas import CanvasService
from test_agent_canvas import MemoryStore


@pytest.mark.asyncio
async def test_live_adapter_delegates_complete_text_into_the_same_turn():
    from agent_canvas import canvas_live_start
    seen = []
    async def agent(**kwargs):
        seen.append(kwargs["message"])
        return "Ready", []
    store = MemoryStore()
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {}, agent=agent)
    sid = str(uuid4())
    scope = bind_user(uuid4())
    try:
        handler = service.voice_handler(sid)
        result = await handler("live-call-1", {"message": "Show macros"})
        assert seen == ["Show macros"]
        assert result["sessionId"] == sid
        assert await handler("live-call-1", {"message": "Show macros"}) == result
        assert len(seen) == 1
        with pytest.raises(ValueError):
            await handler("live-call-2", {"message": "Show macros", "user_id": str(uuid4())})
    finally:
        reset_user(scope)
    event = canvas_live_start({})
    assert event["session"]["model"] == "gpt-live-1"
    backend = event["session"]["delegation"]["responses"]
    assert backend["model"] == "gpt-5.6-luna"
    assert [tool["name"] for tool in backend["tools"]] == ["agent_turn"]
    assert backend["tools"][0]["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_live_bridge_emits_only_server_owned_validated_canvas_result():
    from live_coach import LiveCoachService, LiveCoachPolicy, sanitize_provider_event
    from agent_canvas import canvas_tool_event
    from test_live_coach import FakeClientWebSocket
    import asyncio

    sid = str(uuid4())
    payload = {"protocol": "mmacros.canvas.v1", "catalog": "mmacros.native.v1",
               "sessionId": sid, "instanceId": str(uuid4()), "revision": 1, "serverTime": 1000, "surfaces": [], "reply": "Ready"}
    assert sanitize_provider_event({"type": "agent.canvas", "canvas": payload}) is None
    assert canvas_tool_event("agent_turn", json.dumps(payload))["canvas"] == payload
    assert canvas_tool_event("log_meal", json.dumps(payload)) is None
    for bad_clock in (None, True, -1, float("inf"), "1000"):
        assert canvas_tool_event("agent_turn", json.dumps({**payload, "serverTime": bad_clock})) is None
    assert canvas_tool_event("agent_turn", '{"surfaces":[{"component":"WebView"}]}') is None

    class Provider:
        def __init__(self):
            self.events = asyncio.Queue()
            self.sent = []
        async def recv(self):
            return await self.events.get()
        async def send(self, data):
            self.sent.append(json.loads(data))
    provider = Provider()
    socket = FakeClientWebSocket()
    async def handler(*_):
        return payload
    service = LiveCoachService(store=None, provider_connect=None, api_key="fixture-only",
                               policy=LiveCoachPolicy(idle_timeout=0.1, close_timeout=0.01),
                               tool_handlers={"agent_turn": handler}, tool_result_event=canvas_tool_event)
    await provider.events.put(json.dumps({"type": "response.event", "event": {
        "type": "response.output_item.done", "item": {"type": "function_call", "name": "agent_turn",
        "call_id": "call-1", "arguments": '{"message":"Show macros"}'}}}))
    await service._bridge(socket, provider, frozenset({"agent_turn"}))
    assert any(event.get("type") == "agent.canvas" for event in socket.sent)
