"""Loopback-only mounted route smoke with a fake store and fake Live provider."""

from __future__ import annotations

import asyncio
import base64
from datetime import date
import json
import os
from pathlib import Path
import socket
import sys
from uuid import UUID

os.environ["DATABASE_URL"] = "postgresql://fixture.invalid/macro_tracker"
os.environ["APP_SHARED_TOKEN"] = "fixture-shared-token"
os.environ.pop("OPENAI_API_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from auth import current_user_id
from live_coach import LiveCoachPolicy, LiveCoachService
import server as production_server


TENANT = UUID("0a77fc45-f4d4-42b1-9ae0-e91a303644ff")


class FixtureStore:
    def __init__(self) -> None:
        self.read_tenants: list[UUID] = []
        self.token_lookups: list[str] = []

    async def resolve_device_for_live(self, token: str) -> UUID | None:
        self.token_lookups.append(token)
        return TENANT if token == "fixture-device-token" else None

    def _read(self):
        tenant = current_user_id()
        self.read_tenants.append(tenant)

    async def fetch_targets(self, day):
        self._read()
        return {"calories": 2100, "protein": 170, "carbs": 220, "fat": 70, "fiber": 30}

    async def fetch_meals(self, day, end=None):
        self._read()
        return []

    async def fetch_workout_plan(self):
        self._read()
        return {"rotation": ["Push"], "days": {"Push": {"exercises": ["Bench Press"]}}}

    async def fetch_workouts(self, start=None, end=None, exercise=None):
        self._read()
        return []

    async def fetch_prs(self):
        self._read()
        return []

    async def fetch_workout_library(self):
        self._read()
        return [{"name": "Bench Press", "workout_type": "Push"}]


class FixtureProvider:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.received: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        event = json.loads(raw)
        self.received.append(event)
        if event["type"] == "session.start":
            await self.incoming.put(json.dumps({"type": "session.started"}))
        elif event["type"] == "session.input_audio.append":
            await self.incoming.put(json.dumps({
                "type": "session.output_transcript.delta",
                "delta": "Keep going.",
                "private": "must-not-cross",
            }))
            await self.incoming.put(json.dumps({
                "type": "session.output_audio.delta",
                "delta": event["audio"],
            }))
        elif event["type"] == "session.input_audio.mute":
            await self.incoming.put(json.dumps({"type": "session.input_audio.muted"}))
        elif event["type"] == "session.input_audio.unmute":
            await self.incoming.put(json.dumps({"type": "session.input_audio.unmuted"}))
        elif event["type"] == "session.close":
            await self.incoming.put(json.dumps({
                "type": "session.closed",
                "reason": "close_requested",
                "usage": {"private": True},
            }))

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


async def main() -> None:
    store = FixtureStore()
    provider = FixtureProvider()
    provider_calls: list[tuple[str, dict]] = []

    async def provider_connect(url: str, **kwargs):
        provider_calls.append((url, kwargs))
        return provider

    service = LiveCoachService(
        store=store,
        provider_connect=provider_connect,
        api_key="fixture-project-key",
        today_provider=lambda: date(2026, 9, 10),
        policy=LiveCoachPolicy(connect_timeout=1, close_timeout=1, idle_timeout=5),
    )
    app = production_server.create_app(live_service=service)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        endpoint = f"ws://127.0.0.1:{port}/api/live-coach"

        unauthorized_status = None
        try:
            async with connect(endpoint) as websocket:
                await websocket.recv()
        except InvalidStatus as exc:
            unauthorized_status = exc.response.status_code
        except ConnectionClosed as exc:
            unauthorized_status = exc.code
        assert unauthorized_status in {403, 4401}
        assert provider_calls == []
        assert store.token_lookups == []

        pcm = b"\x00\x00\x01\x00"
        async with connect(
            endpoint,
            additional_headers={"Authorization": "Bearer fixture-device-token"},
        ) as websocket:
            assert json.loads(await websocket.recv()) == {"type": "session.started"}
            await websocket.send(json.dumps({
                "type": "session.input_audio.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
                "user_id": "must-be-rejected",
            }))
            try:
                await asyncio.wait_for(websocket.recv(), timeout=0.05)
                raise AssertionError("tenant-bearing client event was not rejected")
            except TimeoutError:
                pass
            await websocket.send(json.dumps({
                "type": "session.input_audio.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }))
            assert json.loads(await websocket.recv()) == {
                "type": "session.output_transcript.delta",
                "delta": "Keep going.",
            }
            assert json.loads(await websocket.recv()) == {
                "type": "session.output_audio.delta",
                "delta": base64.b64encode(pcm).decode("ascii"),
            }
            await websocket.send(json.dumps({"type": "session.input_audio.mute"}))
            assert json.loads(await websocket.recv()) == {"type": "session.input_audio.muted"}
            await websocket.send(json.dumps({"type": "session.close"}))
            assert json.loads(await websocket.recv()) == {
                "type": "session.closed",
                "reason": "close_requested",
            }

        assert store.token_lookups == ["fixture-device-token"]
        assert store.read_tenants == [TENANT] * 6
        assert len(provider_calls) == 1
        assert provider.received[0]["session"]["model"] == "gpt-live-1"
        assert provider.received[0]["session"]["store"] is False
        assert provider.received[-1] == {"type": "session.close"}
        assert provider.closed is True
        print(json.dumps({
            "authorized": "session.closed",
            "provider_sessions": len(provider_calls),
            "read_calls": len(store.read_tenants),
            "unauthorized_status": unauthorized_status,
            "writes": 0,
        }, sort_keys=True))
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=3)
        listener.close()


if __name__ == "__main__":
    asyncio.run(main())
