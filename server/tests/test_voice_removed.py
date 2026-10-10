"""Voice is gone (owner, 2026-10-10): no ElevenLabs routes, no realtime
socket route, no ElevenLabs module or config. The agent turn/action/snapshot
routes the classic chat uses stay."""
import importlib.util


def test_voice_entry_points_are_gone(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
    monkeypatch.setenv("APP_SHARED_TOKEN", "fixture-shared-token")
    import server as srv

    assert importlib.util.find_spec("elevenlabs_voice") is None
    for name in ("api_elevenlabs_token", "api_elevenlabs_agent_turn", "api_canvas_live"):
        assert not hasattr(srv, name), name
    for field in ("elevenlabs_api_key", "elevenlabs_agent_id", "elevenlabs_tool_secret", "voice_provider"):
        assert not hasattr(srv.CONFIG, field), field
    paths = {getattr(route, "path", "") for route in srv.create_app().routes}
    assert not any("voice" in path or path.endswith("/live") for path in paths), sorted(paths)
    assert "/api/agent-canvas/{session_id}/turn" in paths
    assert "/api/agent-canvas/{session_id}/action" in paths
