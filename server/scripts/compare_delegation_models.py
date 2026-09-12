#!/usr/bin/env python3
"""Compare voice delegation tool selection without executing any tool calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT / "src"))

from live_coach import (  # noqa: E402
    _BACKEND_INSTRUCTIONS,
    _bounded_context_json,
    _voice_delegation_tools,
)

LUNA_MODEL = "gpt-5.6-luna"
TERRA_MODEL = "gpt-5.6-terra"
TERRA_CONFIG = Path("/opt/data/profiles/career/config.yaml")
UTTERANCES = (
    "Log 6 ounces of 93/7 ground beef for dinner.",
    "I ate two eggs and a slice of sourdough for breakfast.",
    "Add a medium banana as a snack.",
)
CONTEXT = {
    "today": {
        "date": "2026-09-12",
        "nutrition": {"calories": 820, "protein": 74, "carbs": 61, "fat": 31, "fiber": 12},
        "workout_logged": False,
        "workout_exercises": [],
    },
    "targets": {"calories": 2400, "protein": 190, "carbs": 220, "fat": 75, "fiber": 30},
    "plan": {"rotation": ["Push", "Pull", "Legs"]},
    "recent_workouts": [],
}


def _short_text(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


def _verify_terra_model() -> None:
    if not TERRA_CONFIG.is_file():
        raise RuntimeError(f"Terra model config unavailable: {TERRA_CONFIG}")
    try:
        config = yaml.safe_load(TERRA_CONFIG.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Terra model config is invalid YAML: {TERRA_CONFIG}") from exc
    model_config = config.get("model") if isinstance(config, dict) else None
    configured_model = (
        model_config.get("default") if isinstance(model_config, dict) else model_config
    )
    if configured_model != TERRA_MODEL:
        raise RuntimeError(
            f"exact Terra model scalar {TERRA_MODEL!r} is absent from {TERRA_CONFIG}"
        )


def _request(model: str, utterance: str, api_key: str) -> dict[str, Any]:
    payload = {
        "model": model,
        "instructions": _BACKEND_INSTRUCTIONS + _bounded_context_json(CONTEXT),
        "input": utterance,
        "tools": _voice_delegation_tools(),
        "tool_choice": "auto",
        "max_output_tokens": 2048,
        "parallel_tool_calls": False,
        "reasoning": {"effort": "low"},
        "store": False,
    }
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    with urlopen(request, timeout=90) as response:
        body = json.load(response)
    elapsed_ms = (time.monotonic() - started) * 1000
    output = body.get("output") if isinstance(body, dict) else None
    items = output if isinstance(output, list) else []
    calls = [item for item in items if isinstance(item, dict) and item.get("type") == "function_call"]
    texts = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "output_text":
                texts.append(_short_text(block.get("text")))
    return {
        "latency_ms": round(elapsed_ms, 1),
        "tool_call": bool(calls),
        "tool": _short_text(calls[0].get("name"), 80) if calls else None,
        "text": _short_text(" ".join(texts)),
    }


def main() -> int:
    try:
        _verify_terra_model()
    except RuntimeError as exc:
        print(f"BLOCKED: Terra model verification failed: {exc}")
        return 2
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("BLOCKED: OPENAI_API_KEY is unavailable; no comparison requests were sent.")
        return 2
    print(f"Terra verified in {TERRA_CONFIG}; tools are observed only and never executed.")
    try:
        for model in (LUNA_MODEL, TERRA_MODEL):
            for run in range(1, 4):
                for utterance_number, utterance in enumerate(UTTERANCES, 1):
                    result = _request(model, utterance, api_key)
                    print(
                        f"model={model} run={run} utterance={utterance_number} "
                        f"latency_ms={result['latency_ms']} tool_call={result['tool_call']} "
                        f"tool={result['tool']!r} text={result['text']!r}"
                    )
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: comparison request failed: {type(exc).__name__}: {_short_text(exc, 200)}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
