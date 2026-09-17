import os
from uuid import uuid4

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

os.environ.setdefault("APP_SHARED_TOKEN", "fixture-only")
os.environ.setdefault("DATABASE_URL", "postgresql://fixture.invalid/not-used")
import server as srv
from agent_canvas import CanvasService
from test_agent_canvas import MemoryStore


class AuthStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.users = {"fixture-a": uuid4(), "fixture-b": uuid4()}
    async def resolve_device(self, token):
        return self.users.get(token)
    async def resolve_device_for_live(self, token):
        return self.users.get(token)
    async def aclose(self):
        pass


def test_canvas_http_and_voice_routes_keep_bearer_tenancy_and_reject_extra_fields(monkeypatch):
    store = AuthStore()
    async def agent(**kwargs):
        return "Ready", []
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {}, agent=agent)
    monkeypatch.setattr(srv, "_client", store)
    monkeypatch.setattr(srv, "_canvas_service", service, raising=False)
    sid = str(uuid4())
    url = f"/api/agent-canvas/{sid}"
    with TestClient(srv.create_app()) as client:
        assert client.get(url).status_code == 401
        headers = {"Authorization": "Bearer fixture-a"}
        assert client.get(url, headers=headers).status_code == 200
        response = client.post(url + "/turn", headers=headers,
                               json={"turn_id": str(uuid4()), "message": "Hi"})
        assert response.status_code == 200, response.text
        assert response.json()["sessionId"] == sid
        assert client.post(url + "/turn", headers=headers,
                           json={"turn_id": str(uuid4()), "message": "Hi", "user_id": str(uuid4())}).status_code == 400
        assert client.post(url + "/action", headers=headers, json={"action": "raw_post"}).status_code == 400
        # Same public session UUID is not an authorization token.
        other = client.get(url, headers={"Authorization": "Bearer fixture-b"}).json()
        assert other["surfaces"] == []
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(url + "/live"):
                pass
        assert exc.value.code == 4401
