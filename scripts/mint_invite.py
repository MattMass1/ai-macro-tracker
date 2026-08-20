#!/usr/bin/env python3
"""Mint a single high-entropy invite code for a user and print it once."""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/src"))

from auth import DEFAULT_MATT_USER_ID  # noqa: E402


async def run() -> None:
    """Mint one invite code. Default: bound to Matt's user (existing data).

    With --new-user NAME (or NEW_USER env): create a FRESH user row with that
    display name and bind the code to it — a real friend, isolated from Matt.
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    display_name = os.environ.get("NEW_USER", "").strip() or os.environ.get("MATT_DISPLAY_NAME", "").strip()
    if not display_name:
        raise SystemExit("NEW_USER (or MATT_DISPLAY_NAME) is required")
    code = secrets.token_urlsafe(16)

    conn = await asyncpg.connect(database_url)
    try:
        async with conn.transaction():
            if os.environ.get("NEW_USER"):
                # Fresh friend account: new UUID, NOT admin, no data.
                user_id = uuid4()
                await conn.execute(
                    "INSERT INTO users(id,display_name,is_admin) VALUES($1,$2,false)",
                    user_id, display_name,
                )
                print(f"NEW USER: {display_name} ({user_id})", file=sys.stderr)
            else:
                # Matt's own device: bind to his existing user.
                user_id = UUID(os.environ.get("MATT_USER_ID", "").strip() or str(DEFAULT_MATT_USER_ID))
                await conn.execute(
                    "INSERT INTO users(id,display_name,is_admin) VALUES($1,$2,true) "
                    "ON CONFLICT(id) DO NOTHING",
                    user_id, display_name,
                )
            await conn.execute(
                "INSERT INTO invite_codes(code,user_id) VALUES($1,$2)", code, user_id
            )
    finally:
        await conn.close()

    print(code)


if __name__ == "__main__":
    asyncio.run(run())
