#!/usr/bin/env python3
"""Mint a single high-entropy invite code for a user and print it once."""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path
from uuid import UUID

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/src"))

from auth import DEFAULT_MATT_USER_ID  # noqa: E402


async def run() -> None:
    """Create the configured user if needed, then mint one invite code."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    user_id = UUID(os.environ.get("MATT_USER_ID", "").strip() or str(DEFAULT_MATT_USER_ID))
    display_name = os.environ.get("MATT_DISPLAY_NAME", "Matt").strip() or "Matt"
    code = secrets.token_urlsafe(16)

    conn = await asyncpg.connect(database_url)
    try:
        async with conn.transaction():
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
