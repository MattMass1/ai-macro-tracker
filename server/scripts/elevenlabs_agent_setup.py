#!/usr/bin/env python3
"""Idempotent create/update of the MMMacros ElevenLabs voice agent.

Safe by default: with no flag it performs ONLY read-only account calls (list
LLMs, agents, tools, detect an existing MMMacros agent) and prints the exact
JSON it WOULD send. Pass --apply to actually create/update. Agent/tool/secret
CRUD is config, not billed conversation minutes.

The agent is a thin relay: its only job is to call the agent_turn webhook with
the user's message and speak the returned `reply`. All reasoning, tools, food
lookup, writes and verification stay on our server behind that webhook.

Env:
  ELEVENLABS_API_KEY      required
  ELEVENLABS_TOOL_SECRET  shared secret for the webhook; generated+printed if unset
  ELEVENLABS_WEBHOOK_URL  the agent_turn URL (or pass --webhook-url)

Usage:
  python scripts/elevenlabs_agent_setup.py                      # dry run
  python scripts/elevenlabs_agent_setup.py --webhook-url URL --apply
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

API_BASE = "https://api.elevenlabs.io"
AGENT_NAME = "MMMacros Coach (voice)"
TOOL_NAME = "mmmacros_agent_turn"
SECRET_NAME = "mmmacros_tool_secret"
SECRET_HEADER = "X-Elevenlabs-Tool-Secret"
DEFAULT_VOICE_ID = "CwhRBWXzGAHq8TQ4Fs17"  # "Roger" — calm; override with --voice-id
PLACEHOLDER_URL = "https://REPLACE-ME.trycloudflare.com/api/voice/elevenlabs/agent-turn"
LLM_PREFERENCE = ("gemini-2.5-flash", "gemini-2.0-flash", "gpt-4o-mini")

# Reused from agent_canvas.canvas_live_start — the relay stays dumb on purpose.
RELAY_PROMPT = (
    "You are the voice adapter for MMMacros. For every user request, call the "
    "agent_turn tool exactly once with the user's complete current request, "
    "including relevant referents. Never calculate, choose domain tools, or "
    "claim a write yourself. Speak only the `reply` field returned by the tool, "
    "verbatim. If the reply asks a clarifying question, ask exactly that. "
    "Partial transcripts are not permission to infer extra food or writes."
)
FIRST_MESSAGE = "Hey, it's your coach. What did you eat, or what do you want to check?"


class SetupError(RuntimeError):
    pass


def _request(method: str, path: str, api_key: str, body: dict | None = None) -> dict:
    url = API_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("xi-api-key", api_key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        raise SetupError(f"{method} {path} -> {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise SetupError(f"{method} {path} -> {exc}") from None
    return json.loads(raw) if raw else {}


def pick_llm(api_key: str) -> str:
    try:
        data = _request("GET", "/v1/convai/llm/list", api_key)
    except SetupError as exc:
        print(f"  (could not list LLMs: {exc}; defaulting to {LLM_PREFERENCE[0]})")
        return LLM_PREFERENCE[0]
    available = {
        item.get("llm") or item.get("id") or item.get("name")
        for item in (data.get("llms") or data.get("models") or data)
        if isinstance(item, dict)
    }
    for want in LLM_PREFERENCE:
        if want in available:
            return want
    return next((m for m in available if m and ("flash" in m or "mini" in m)),
                LLM_PREFERENCE[0])


def _name_of(item: dict) -> str | None:
    # Tools nest the name under tool_config; agents/secrets keep it top-level.
    return item.get("name") or (item.get("tool_config") or {}).get("name")


def find_by_name(items: list, name: str, *id_keys: str) -> str | None:
    for item in items:
        if isinstance(item, dict) and _name_of(item) == name:
            for key in id_keys:
                if item.get(key):
                    return item[key]
    return None


def ensure_secret(api_key: str, value: str, apply: bool) -> str:
    existing = _request("GET", "/v1/convai/secrets", api_key).get("secrets", [])
    secret_id = find_by_name(existing, SECRET_NAME, "secret_id", "id")
    if secret_id:
        print(f"  secret '{SECRET_NAME}' exists ({secret_id}); value not overwritten")
        return secret_id
    body = {"type": "new", "name": SECRET_NAME, "value": value}
    print(f"  would CREATE secret '{SECRET_NAME}'")
    if not apply:
        return "<secret_id-pending>"
    created = _request("POST", "/v1/convai/secrets", api_key, body)
    return created.get("secret_id") or created.get("id")


def tool_config(webhook_url: str, secret_id: str) -> dict:
    return {
        "tool_config": {
            "type": "webhook",
            "name": TOOL_NAME,
            "description": 'Run the shared MMMacros session. Example: {"message":"Show my remaining macros"}.',
            "response_timeout_secs": 20,
            "api_schema": {
                "url": webhook_url,
                "method": "POST",
                "request_headers": {SECRET_HEADER: {"secret_id": secret_id}},
                "request_body_schema": {
                    "type": "object",
                    "required": ["message", "conversation_id"],
                    "properties": {
                        "message": {"type": "string",
                                     "description": "The user's complete current request, verbatim."},
                        "conversation_id": {"type": "string",
                                             "dynamic_variable": "system__conversation_id"},
                        "turn_index": {"type": "string",
                                        "dynamic_variable": "system__agent_turns"},
                    },
                },
            },
        }
    }


def agent_config(llm: str, voice_id: str, tool_id: str) -> dict:
    return {
        "name": AGENT_NAME,
        "conversation_config": {
            "agent": {
                "prompt": {"prompt": RELAY_PROMPT, "llm": llm, "temperature": 0.1,
                           "tool_ids": [tool_id]},
                "first_message": FIRST_MESSAGE,
                "language": "en",
            },
            "turn": {"turn_timeout": 7, "turn_eagerness": "normal"},
            # Custom/cloned voices are fine-tuned for specific models; English
            # agents require a turbo or flash v2 model. flash v2 = lowest latency.
            "tts": {"voice_id": voice_id, "model_id": "eleven_flash_v2"},
        },
        "platform_settings": {
            "privacy": {"record_voice": False, "retention_days": 1,
                        "delete_audio": True},
        },
        "tags": ["mmmacros", "voice", "spike"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--webhook-url", default=os.environ.get("ELEVENLABS_WEBHOOK_URL", PLACEHOLDER_URL))
    parser.add_argument("--voice-id", default=os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID))
    parser.add_argument("--apply", action="store_true", help="actually create/update (default: dry run)")
    args = parser.parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        print("ELEVENLABS_API_KEY is not set (put it in server/.env).")
        return 2
    mode = "APPLY" if args.apply else "DRY RUN (read-only; pass --apply to write)"
    print(f"== MMMacros ElevenLabs agent setup: {mode} ==")
    if args.webhook_url == PLACEHOLDER_URL:
        print(f"  WARNING: using placeholder webhook URL. Re-run with --webhook-url "
              f"once cloudflared is up. ({PLACEHOLDER_URL})")

    tool_secret = os.environ.get("ELEVENLABS_TOOL_SECRET", "").strip()
    if not tool_secret:
        tool_secret = secrets.token_urlsafe(32)
        print(f"  NOTE: ELEVENLABS_TOOL_SECRET was unset. Generated one — add this to "
              f"server/.env so the webhook accepts the agent:\n    ELEVENLABS_TOOL_SECRET={tool_secret}")

    llm = pick_llm(api_key)
    print(f"  relay LLM: {llm}")
    print(f"  voice id : {args.voice_id}")

    secret_id = ensure_secret(api_key, tool_secret, args.apply)

    tools = _request("GET", "/v1/convai/tools", api_key).get("tools", [])
    tool_id = find_by_name(tools, TOOL_NAME, "id", "tool_id")
    tcfg = tool_config(args.webhook_url, secret_id)
    if tool_id:
        print(f"  would UPDATE tool '{TOOL_NAME}' ({tool_id})")
        if args.apply:
            _request("PATCH", f"/v1/convai/tools/{tool_id}", api_key, tcfg)
    else:
        print(f"  would CREATE tool '{TOOL_NAME}'")
        print("    " + json.dumps(tcfg, indent=2).replace("\n", "\n    "))
        if args.apply:
            created = _request("POST", "/v1/convai/tools", api_key, tcfg)
            tool_id = created.get("id") or created.get("tool_id")
    if not args.apply:
        tool_id = tool_id or "<tool_id-pending>"

    agents = _request("GET", "/v1/convai/agents", api_key).get("agents", [])
    agent_id = find_by_name(agents, AGENT_NAME, "agent_id")
    acfg = agent_config(llm, args.voice_id, tool_id)
    if agent_id:
        print(f"  would UPDATE agent '{AGENT_NAME}' ({agent_id})")
        if args.apply:
            _request("PATCH", f"/v1/convai/agents/{agent_id}", api_key, acfg)
    else:
        print(f"  would CREATE agent '{AGENT_NAME}'")
        if args.apply:
            created = _request("POST", "/v1/convai/agents/create", api_key, acfg)
            agent_id = created.get("agent_id")

    if args.apply:
        print(f"\nDONE. Agent id: {agent_id}\n  Add to server/.env:\n    ELEVENLABS_AGENT_ID={agent_id}")
    else:
        print("\nDry run complete. Re-run with --apply (and a real --webhook-url) to create.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
