"""Tests for the ElevenLabs voice layer: token minting, the agent_turn webhook,
server-trusted id derivation, and route auth. No network, no real SDK."""
import dataclasses
import os
from uuid import UUID, uuid4

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("APP_SHARED_TOKEN", "fixture-only")
os.environ.setdefault("DATABASE_URL", "postgresql://fixture.invalid/not-used")

import elevenlabs_voice as elv  # noqa: E402
import server as srv  # noqa: E402
from test_agent_canvas import MemoryStore  # noqa: E402


# ---- pure id derivation -----------------------------------------------------

def test_session_and_turn_ids_are_deterministic_and_server_trusted():
    conv = "conv_abc"
    s1 = elv.derive_session_id(conv)
    s2 = elv.derive_session_id(conv)
    assert s1 == s2 and str(UUID(s1)) == s1  # canonical and stable
    t0 = elv.derive_turn_id(s1, conv, 0)
    t1 = elv.derive_turn_id(s1, conv, 1)
    assert str(UUID(t0)) == t0
    assert t0 != t1  # distinct utterances get distinct business keys
    assert elv.derive_turn_id(s1, conv, 0) == t0  # a retry of the same turn replays


def test_turn_id_ignores_message_text():
    # Two identical utterances must still be two different turns (log twice).
    conv = "conv_xyz"
    sid = elv.derive_session_id(conv)
    assert elv.derive_turn_id(sid, conv, 5) != elv.derive_turn_id(sid, conv, 6)


def test_secret_ok_rejects_empty_and_mismatch():
    assert elv.secret_ok("s3cret", "s3cret") is True
    assert elv.secret_ok("nope", "s3cret") is False
    assert elv.secret_ok("", "s3cret") is False
    assert elv.secret_ok("s3cret", "") is False
    assert elv.secret_ok(None, "s3cret") is False


# ---- token minting ----------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response, recorder):
        self._response = response
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self._recorder["url"] = url
        self._recorder["params"] = params
        self._recorder["headers"] = headers
        return self._response


@pytest.mark.asyncio
async def test_mint_token_returns_token_and_never_leaks_api_key():
    recorder = {}
    factory = lambda: _FakeClient(  # noqa: E731
        _FakeResponse(200, {"token": "tok_123", "conversation_id": "conv_1"}), recorder
    )
    result = await elv.mint_conversation_token(
        api_key="sk-secret", agent_id="agent_1", client_factory=factory
    )
    assert result == {"token": "tok_123", "conversation_id": "conv_1", "agent_id": "agent_1"}
    assert "sk-secret" not in str(result)
    assert recorder["headers"]["xi-api-key"] == "sk-secret"  # key used as header only
    assert recorder["params"] == {"agent_id": "agent_1"}


@pytest.mark.asyncio
async def test_mint_token_rejects_missing_config_and_bad_status():
    with pytest.raises(elv.ElevenLabsVoiceError):
        await elv.mint_conversation_token(api_key="", agent_id="agent_1")
    with pytest.raises(elv.ElevenLabsVoiceError):
        await elv.mint_conversation_token(api_key="k", agent_id="")
    factory = lambda: _FakeClient(_FakeResponse(401, {"detail": "nope"}), {})  # noqa: E731
    with pytest.raises(elv.ElevenLabsVoiceError):
        await elv.mint_conversation_token(api_key="k", agent_id="a", client_factory=factory)


# ---- run_agent_turn ---------------------------------------------------------

@pytest.mark.asyncio
async def test_run_agent_turn_rejects_bad_secret_without_running_turn():
    called = []

    async def turn_fn(s, t, m):
        called.append((s, t, m))
        return {"reply": "x"}

    payload, status = await elv.run_agent_turn(
        {"message": "hi", "conversation_id": "c"},
        provided_secret="wrong", expected_secret="right", turn_fn=turn_fn,
    )
    assert status == 401 and called == []


@pytest.mark.asyncio
async def test_run_agent_turn_derives_ids_and_returns_reply():
    seen = {}

    async def turn_fn(session_id, turn_id, message):
        seen["ids"] = (session_id, turn_id, message)
        return {"reply": "You have 800 calories left.", "surfaces": []}

    body = {"message": "remaining macros", "conversation_id": "conv_9", "turn_index": 3}
    payload, status = await elv.run_agent_turn(
        body, provided_secret="s", expected_secret="s", turn_fn=turn_fn
    )
    assert status == 200
    assert payload == {"reply": "You have 800 calories left."}
    expect_session = elv.derive_session_id("conv_9")
    expect_turn = elv.derive_turn_id(expect_session, "conv_9", 3)
    assert seen["ids"] == (expect_session, expect_turn, "remaining macros")


@pytest.mark.asyncio
async def test_run_agent_turn_missing_conversation_id_is_rejected():
    async def turn_fn(s, t, m):
        raise AssertionError("must not run without a conversation id")

    payload, status = await elv.run_agent_turn(
        {"message": "hi"}, provided_secret="s", expected_secret="s", turn_fn=turn_fn
    )
    assert status == 400


@pytest.mark.asyncio
async def test_run_agent_turn_surfaces_turn_validation_as_spoken_reply():
    async def turn_fn(s, t, m):
        raise ValueError("Use a message between 1 and 1000 characters")

    payload, status = await elv.run_agent_turn(
        {"message": "hi", "conversation_id": "c"},
        provided_secret="s", expected_secret="s", turn_fn=turn_fn,
    )
    assert status == 200 and "message between" in payload["reply"]


# ---- routes (auth + wiring) -------------------------------------------------

class AuthStore(MemoryStore):
    def __init__(self, owner, other):
        super().__init__()
        self.users = {"owner-tok": owner, "other-tok": other}

    async def resolve_device(self, token):
        return self.users.get(token)

    async def resolve_device_for_live(self, token):
        return self.users.get(token)

    async def aclose(self):
        pass


def _configure(monkeypatch, **overrides):
    owner = overrides.pop("owner")
    cfg = dataclasses.replace(srv.CONFIG, matt_user_id=owner, **overrides)
    monkeypatch.setattr(srv, "CONFIG", cfg)


def test_token_route_owner_only_and_flag_gated(monkeypatch):
    owner, other = uuid4(), uuid4()
    store = AuthStore(owner, other)
    monkeypatch.setattr(srv, "_client", store)
    _configure(monkeypatch, owner=owner, voice_provider="elevenlabs",
               elevenlabs_api_key="sk-secret", elevenlabs_agent_id="agent_1")

    async def fake_mint(*, api_key, agent_id):
        return {"token": "tok_live", "conversation_id": "conv_1", "agent_id": agent_id}

    monkeypatch.setattr(srv.elevenlabs_voice, "mint_conversation_token", fake_mint)

    with TestClient(srv.create_app()) as client:
        url = "/api/voice/elevenlabs/token"
        assert client.post(url).status_code == 401  # no bearer
        owner_h = {"Authorization": "Bearer owner-tok"}
        res = client.post(url, headers=owner_h)
        assert res.status_code == 200, res.text
        assert res.json()["token"] == "tok_live"
        assert "sk-secret" not in res.text  # key never leaves the server
        # Non-owner is refused even with a valid device token.
        assert client.post(url, headers={"Authorization": "Bearer other-tok"}).status_code == 403


def test_token_route_409_when_provider_is_openai(monkeypatch):
    owner = uuid4()
    store = AuthStore(owner, uuid4())
    monkeypatch.setattr(srv, "_client", store)
    _configure(monkeypatch, owner=owner, voice_provider="openai")
    with TestClient(srv.create_app()) as client:
        res = client.post("/api/voice/elevenlabs/token", headers={"Authorization": "Bearer owner-tok"})
        assert res.status_code == 409


def test_agent_turn_webhook_requires_secret_and_runs_shared_turn(monkeypatch):
    owner = uuid4()
    store = AuthStore(owner, uuid4())
    monkeypatch.setattr(srv, "_client", store)
    _configure(monkeypatch, owner=owner, voice_provider="elevenlabs", elevenlabs_tool_secret="hook-secret")

    calls = []

    class FakeCanvas:
        async def turn(self, session_id, turn_id, message, *, adapter):
            calls.append((session_id, turn_id, message, adapter))
            return {"reply": "Logged it.", "surfaces": []}

    monkeypatch.setattr(srv, "_canvas_service", FakeCanvas(), raising=False)

    with TestClient(srv.create_app()) as client:
        url = "/api/voice/elevenlabs/agent-turn"
        body = {"message": "log a banana", "conversation_id": "conv_1", "turn_index": 0}
        # Missing secret -> rejected, turn never runs.
        assert client.post(url, json=body).status_code == 401
        assert calls == []
        # Bad secret -> rejected.
        assert client.post(url, json=body, headers={"X-Elevenlabs-Tool-Secret": "nope"}).status_code == 401
        # Correct secret -> runs the shared voice turn and speaks the reply.
        res = client.post(url, json=body, headers={"X-Elevenlabs-Tool-Secret": "hook-secret"})
        assert res.status_code == 200, res.text
        assert res.json() == {"reply": "Logged it."}
        assert len(calls) == 1
        session_id, turn_id, message, adapter = calls[0]
        assert adapter == "voice"
        assert message == "log a banana"
        assert session_id == elv.derive_session_id("conv_1")
        assert turn_id == elv.derive_turn_id(session_id, "conv_1", 0)
