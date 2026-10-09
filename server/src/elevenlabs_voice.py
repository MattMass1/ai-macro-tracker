"""ElevenLabs Agents voice: token minting and the agent_turn webhook.

This module is the ONLY new voice-layer surface. It never touches CanvasService
business logic: the webhook derives server-trusted session/turn ids from platform
fields and calls the same `CanvasService.turn(adapter="voice")` the GPT adapter
uses. The ElevenLabs API key lives server-side only; the app receives just a
short-lived conversation token.

Dependencies (CONFIG, canvas_service, user binding) are injected by server.py so
this module stays import-cycle free and unit-testable without the full app.
"""
from __future__ import annotations

import hmac
import itertools
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID, uuid5

import httpx

logger = logging.getLogger("elevenlabs_voice")

# Fixed namespace so a given ElevenLabs conversation always maps to the same
# canvas session id. Not a secret; it only has to be stable.
CONVERSATION_NAMESPACE = UUID("8f2a1d6e-4c3b-5a7f-9e10-2b3c4d5e6f70")

ELEVENLABS_API_BASE = "https://api.elevenlabs.io"
TOKEN_PATH = "/v1/convai/conversation/token"

# Fallback per-conversation turn counter used only when the platform does not
# send a turn index. In-process, single-worker; good enough for the spike. The
# authoritative source is the injected system__agent_turns value.
_fallback_turn_counters: dict[str, Any] = defaultdict(lambda: itertools.count())


class ElevenLabsVoiceError(RuntimeError):
    """Raised when ElevenLabs token issuance fails."""


async def mint_conversation_token(
    *,
    api_key: str,
    agent_id: str,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> dict[str, str]:
    """Exchange the server-only API key for a short-lived WebRTC conversation
    token bound to our agent. Returns token + conversation_id + agent_id. The
    API key is never returned."""
    if not api_key or not agent_id:
        raise ElevenLabsVoiceError("ElevenLabs voice is not configured")
    make_client = client_factory or (lambda: httpx.AsyncClient(timeout=15.0))
    try:
        async with make_client() as client:
            response = await client.get(
                ELEVENLABS_API_BASE + TOKEN_PATH,
                params={"agent_id": agent_id},
                headers={"xi-api-key": api_key},
            )
    except httpx.HTTPError as exc:
        raise ElevenLabsVoiceError("Could not reach ElevenLabs") from exc
    if response.status_code != 200:
        # Never echo the upstream body; it could contain account detail.
        raise ElevenLabsVoiceError(
            f"ElevenLabs token request failed ({response.status_code})"
        )
    body = response.json()
    token = body.get("token")
    if not isinstance(token, str) or not token:
        raise ElevenLabsVoiceError("ElevenLabs returned no token")
    return {
        "token": token,
        "conversation_id": str(body.get("conversation_id") or ""),
        "agent_id": agent_id,
    }


def secret_ok(provided: str | None, expected: str) -> bool:
    """Constant-time shared-secret check for the server-to-server webhook."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def derive_session_id(conversation_id: str) -> str:
    """Server-trusted canvas session id from the platform conversation id."""
    return str(uuid5(CONVERSATION_NAMESPACE, conversation_id))


def derive_turn_id(session_id: str, conversation_id: str, turn_index: int) -> str:
    """Canonical UUID turn id, stable per utterance and unique per turn. Never
    derived from message text, so two identical utterances log twice."""
    return str(uuid5(UUID(session_id), f"{conversation_id}:{turn_index}"))


def _coerce_turn_index(value: Any, conversation_id: str) -> int:
    """Prefer the platform-sent turn index (system__agent_turns); fall back to an
    in-process counter so a missing value cannot collapse distinct turns."""
    if isinstance(value, bool):
        value = None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if isinstance(value, float):
        return int(value)
    logger.warning("agent_turn missing a turn index; using in-process counter")
    return next(_fallback_turn_counters[conversation_id])


TurnFn = Callable[[str, str, str], Awaitable[Mapping[str, Any]]]


async def run_agent_turn(
    body: Mapping[str, Any],
    *,
    provided_secret: str | None,
    expected_secret: str,
    turn_fn: TurnFn,
) -> tuple[dict[str, Any], int]:
    """Validate the webhook, derive trusted ids, run the shared turn, and return
    the spoken reply. Returns (payload, status_code). On an unverifiable outcome
    the caller must not let the agent claim success."""
    if not secret_ok(provided_secret, expected_secret):
        return {"error": "unauthorized"}, 401
    if not isinstance(body, Mapping):
        return {"reply": "Sorry, I could not read that request."}, 400
    message = body.get("message")
    conversation_id = body.get("conversation_id")
    if not isinstance(message, str) or not message.strip():
        return {"reply": "I did not catch that. Could you say it again?"}, 200
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return {"error": "missing conversation_id"}, 400
    conversation_id = conversation_id.strip()
    turn_index = _coerce_turn_index(body.get("turn_index"), conversation_id)
    session_id = derive_session_id(conversation_id)
    turn_id = derive_turn_id(session_id, conversation_id, turn_index)
    try:
        result = await turn_fn(session_id, turn_id, message)
    except ValueError as exc:
        # Validation-level rejection from the shared turn (bad length, etc).
        return {"reply": str(exc)[:500]}, 200
    reply = ""
    if isinstance(result, Mapping):
        reply = str(result.get("reply") or "")
    return {"reply": reply[:1000]}, 200
