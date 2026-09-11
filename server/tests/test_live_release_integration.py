"""Regression coverage for the integrated production ASGI app."""

from __future__ import annotations

import os
from uuid import UUID

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

os.environ.setdefault("APP_SHARED_TOKEN", "fixture-shared-token")
os.environ.setdefault("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")

import server as srv
from live_coach import LiveCoachService


class FakeStore:
    """Authentication-only store that never touches a database."""

    async def resolve_device(self, token: str) -> UUID | None:
        return None

    async def resolve_device_for_live(self, token: str) -> UUID | None:
        return None

    async def aclose(self) -> None:
        pass


async def reject_provider(*args, **kwargs):
    pytest.fail("unauthorized requests must not reach the Live provider")


def test_integrated_app_keeps_production_routes_and_auth_gates(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(srv, "_client", store)
    service = LiveCoachService(
        store=store,
        provider_connect=reject_provider,
        api_key="fixture-project-key",
    )

    with TestClient(srv.create_app(live_service=service)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/readiness").status_code == 401
        assert client.post("/mcp").status_code == 401

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/api/live-coach"):
                pass

    assert exc_info.value.code == 4401
    assert exc_info.value.reason == "Missing or invalid bearer token"
