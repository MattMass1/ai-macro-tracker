"""Tenant identity helpers shared by HTTP handlers, MCP tools, and scripts."""
from __future__ import annotations

from contextvars import ContextVar, Token
from uuid import NAMESPACE_URL, UUID, uuid5

DEFAULT_MATT_USER_ID = uuid5(NAMESPACE_URL, "macro-tracker:matt")
_current_user_id: ContextVar[UUID | None] = ContextVar("current_user_id", default=None)


def bind_user(user_id: UUID | str) -> Token[UUID | None]:
    """Bind a trusted server-resolved user id to the current async context."""
    return _current_user_id.set(UUID(str(user_id)))


def reset_user(token: Token[UUID | None]) -> None:
    """Restore the tenant context that preceded ``bind_user``."""
    _current_user_id.reset(token)


def current_user_id() -> UUID:
    """Return the bound tenant id, failing closed when none is available."""
    user_id = _current_user_id.get()
    if user_id is None:
        raise RuntimeError("No authenticated user is bound to this operation")
    return user_id
