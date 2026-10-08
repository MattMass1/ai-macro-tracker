"""Fail-closed release gates for the nullable-nutrient (calorie-only) contract.

Migration 003 lets nutrition_entries store SQL NULL for unknown protein, carbs,
fat and fiber. Native builds older than the nullable FoodEntry model cannot
decode such rows, so the schema change and the write path are released
separately:

* The schema (003) only relaxes NOT NULL. While no NULL row exists, every
  existing client keeps working.
* A calorie-only write requires ALL of:
  1. the operator switch ``CALORIE_ONLY_WRITES_ENABLED=true`` (exact word),
  2. a request from a client that declares it reads nullable nutrients
     (``X-Nutrient-Contract: nullable-v1``), and
  3. when ``CALORIE_ONLY_WRITES_USER_IDS`` is set, the authenticated user is
     on that comma-separated canary list.

Anything else, including a missing, blank or misspelled value, keeps the gate
closed. Closing the gate is the forward-disable rollback: existing NULL rows
stay NULL and readable; no value is ever rewritten to zero.

The client declaration proves only that the *writing* device can read NULLs.
Other devices on the same account may still run an old build, so the operator
switch (and canary list) must stay off until that account's devices update.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from contextvars import ContextVar, Token
from uuid import UUID

from auth import current_user_id

CALORIE_ONLY_WRITES_FLAG = "CALORIE_ONLY_WRITES_ENABLED"
CALORIE_ONLY_USERS_FLAG = "CALORIE_ONLY_WRITES_USER_IDS"
NUTRIENT_CONTRACT_HEADER = "x-nutrient-contract"
NULLABLE_NUTRIENTS_V1 = "nullable-v1"

_client_contracts: ContextVar[frozenset[str]] = ContextVar(
    "client_nutrient_contracts", default=frozenset()
)


def bind_client_contract(headers: Mapping[str, str] | None) -> Token[frozenset[str]]:
    """Bind the nutrient contracts the calling client declared for this request."""
    raw = ""
    if headers is not None:
        raw = headers.get(NUTRIENT_CONTRACT_HEADER) or headers.get("X-Nutrient-Contract") or ""
    tokens = frozenset(
        part.strip().casefold() for part in str(raw).split(",") if part.strip()
    )
    return _client_contracts.set(tokens)


def reset_client_contract(token: Token[frozenset[str]]) -> None:
    _client_contracts.reset(token)


def client_reads_nullable_nutrients() -> bool:
    """True only when the current request's client declared ``nullable-v1``."""
    return NULLABLE_NUTRIENTS_V1 in _client_contracts.get()


def calorie_only_writes_enabled() -> bool:
    """Operator switch. Only the exact value ``true`` (any case) opens it."""
    return os.environ.get(CALORIE_ONLY_WRITES_FLAG, "").strip().casefold() == "true"


def _user_in_canary() -> bool:
    raw = os.environ.get(CALORIE_ONLY_USERS_FLAG, "").strip()
    if not raw:
        return True  # no canary list: the operator switch applies to all users
    try:
        allowed = {UUID(part.strip()) for part in raw.split(",") if part.strip()}
    except ValueError:
        return False  # a malformed list never widens access
    try:
        return current_user_id() in allowed
    except RuntimeError:
        return False


def calorie_only_write_allowed() -> bool:
    """All gates must be open before any NULL nutrient may be written."""
    return (calorie_only_writes_enabled() and client_reads_nullable_nutrients()
            and _user_in_canary())


def gate_status() -> dict[str, bool]:
    """Non-secret gate state for diagnostics and tests."""
    return {
        "operator_switch": calorie_only_writes_enabled(),
        "client_declares_nullable": client_reads_nullable_nutrients(),
        "user_in_canary": _user_in_canary(),
        "allowed": calorie_only_write_allowed(),
    }
