"""Tests for the read-only voice delegation model comparison helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "compare_delegation_models.py"
SPEC = importlib.util.spec_from_file_location("compare_delegation_models", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)


def test_verify_terra_model_requires_exact_config_scalar(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    monkeypatch.setattr(compare, "TERRA_CONFIG", config)

    config.write_text("model:\n  default: gpt-5.6-terra-900k\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="exact Terra model scalar"):
        compare._verify_terra_model()

    config.write_text("model:\n  default: gpt-5.6-terra\n", encoding="utf-8")
    compare._verify_terra_model()


def test_main_distinguishes_missing_api_credential_from_terra_pin(capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(compare, "_verify_terra_model", lambda: None)

    assert compare.main() == 2
    assert "OPENAI_API_KEY is unavailable" in capsys.readouterr().out
