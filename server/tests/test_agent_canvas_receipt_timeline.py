"""Phase B: the receipt timeline is a native, read-only task surface.

`show_log` (native tap) and `present_surface {"view": "log"}` (model) both present a
single ReceiptTimeline component. The server never places meal or set values in
the surface; the app renders its own authoritative day and workout reads.
"""
from uuid import uuid4

import pytest

from agent_canvas import ACTIONS, COMPONENTS, CANVAS_INSTRUCTIONS, PRESENTATION_ACTIONS, CanvasService
from auth import bind_user, reset_user
from test_agent_canvas import MemoryStore


def _service(agent=None):
    async def default_agent(**_kwargs):
        return "ok", []
    return CanvasService(store_factory=MemoryStore, food_factory=lambda: {},
                        coach_factory=lambda: {}, agent=agent or default_agent)


def test_contract_names_are_registered():
    assert "ReceiptTimeline" in COMPONENTS and "show_log" in ACTIONS
    assert PRESENTATION_ACTIONS["log"] == "show_log"
    assert "today's log" in CANVAS_INSTRUCTIONS


@pytest.mark.asyncio
async def test_show_log_action_presents_a_value_free_timeline():
    service = _service()
    sid = str(uuid4())
    scope = bind_user(uuid4())
    try:
        await service.snapshot(sid, create=True)
        result = await service.action(sid, {"action": "show_log"})
        with pytest.raises(ValueError):
            await service.action(sid, {"action": "show_log", "calories": 500})
    finally:
        reset_user(scope)
    task = next(s for s in result["surfaces"] if s["surfaceId"] == "task")
    assert [c["component"] for c in task["components"]] == ["ReceiptTimeline"]
    component = task["components"][0]
    assert component["title"] == "Today's log"
    assert not {"text", "rows", "value", "meals", "sets"} & set(k for k, v in component.items() if v)
    assert result["approval"] is None


@pytest.mark.asyncio
async def test_present_surface_log_view_is_available_to_the_model():
    seen = {}

    async def agent(**kwargs):
        catalog = {t["name"]: t for t in kwargs["tool_catalog"]}
        seen["views"] = catalog["present_surface"]["input_schema"]["properties"]["view"]["enum"]
        seen["components"] = catalog["present_surface"]["input_schema"]["properties"]["components"]["items"]["enum"]
        await kwargs["handlers"]["present_surface"]({"view": "log"})
        with pytest.raises(ValueError):
            await kwargs["handlers"]["present_surface"]({"view": "log", "components": ["MacroProgress"]})
        return "Here is what you logged today.", []

    service = _service(agent)
    scope = bind_user(uuid4())
    try:
        result = await service.turn(str(uuid4()), str(uuid4()), "what did I eat today", adapter="voice")
    finally:
        reset_user(scope)
    assert "log" in seen["views"] and "ReceiptTimeline" in seen["components"]
    task = next(s for s in result["surfaces"] if s["surfaceId"] == "task")
    assert [c["component"] for c in task["components"]] == ["ReceiptTimeline"]
