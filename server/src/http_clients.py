"""Shared outbound HTTP clients.

Creating an `httpx.AsyncClient` per request paid a fresh TCP + TLS handshake to
the same host every time: one per LLM round, one per Exa search, one per
extraction. Clients are bound to an event loop, so one is kept per running
loop and per timeout profile; tests that run their own loops get their own.
"""
from __future__ import annotations

import asyncio
import weakref

import httpx

_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[float, httpx.AsyncClient]]" = (
    weakref.WeakKeyDictionary())


def shared_client(timeout: float) -> httpx.AsyncClient:
    """A keep-alive client for the current event loop and timeout."""
    loop = asyncio.get_running_loop()
    per_loop = _clients.get(loop)
    if per_loop is None:
        per_loop = {}
        _clients[loop] = per_loop
    client = per_loop.get(timeout)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False)
        per_loop[timeout] = client
    return client
