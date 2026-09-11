"""Mounted loopback fixture for the real native URLSession transport test."""

from __future__ import annotations

from datetime import date
import os

from live_route_smoke import FixtureProvider, FixtureStore
from live_coach import LiveCoachPolicy, LiveCoachService
import server as production_server
import uvicorn


async def connect_fixture_provider(url: str, **kwargs):
    return FixtureProvider()


def main() -> None:
    service = LiveCoachService(
        store=FixtureStore(),
        provider_connect=connect_fixture_provider,
        api_key="fixture-project-key",
        today_provider=lambda: date(2026, 9, 10),
        policy=LiveCoachPolicy(connect_timeout=1, close_timeout=1, idle_timeout=10),
    )
    uvicorn.run(
        production_server.create_app(live_service=service),
        host="127.0.0.1",
        port=int(os.environ.get("LIVE_COACH_FIXTURE_PORT", "18765")),
        log_level="info",
        ws_max_size=65_536,
        ws_max_queue=16,
    )


if __name__ == "__main__":
    main()
