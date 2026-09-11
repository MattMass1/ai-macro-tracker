"""Environment configuration for the macro tracker service.

Every required variable is checked once at import time of `load_config()` so the
process refuses to start rather than 500-ing on the first request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from auth import DEFAULT_MATT_USER_ID

_ENV_LOADED = False


def _load_dotenv() -> None:
    """Load `server/.env` if present. Real environments (Render) set vars directly."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    for candidate in (
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
        Path.cwd() / "server" / ".env",
    ):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        return


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing."""


@dataclass(frozen=True)
class Config:
    database_url: str
    app_shared_token: str
    local_tz: str
    day_rollover_hour: int
    briefs_dir: Path
    port: int
    matt_user_id: UUID
    allowed_origins: list[str] = field(default_factory=list)
    openai_api_key: str = ""


def _require(name: str, missing: list[str], default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        missing.append(name)
    return value


def load_config() -> Config:
    """Build the service config, raising `ConfigError` naming every missing var."""
    _load_dotenv()
    missing: list[str] = []

    cfg = Config(
        database_url=_require("DATABASE_URL", missing),
        app_shared_token=_require("APP_SHARED_TOKEN", missing),
        local_tz=os.environ.get("LOCAL_TZ", "America/New_York").strip(),
        day_rollover_hour=int(os.environ.get("DAY_ROLLOVER_HOUR", "4")),
        briefs_dir=Path(os.environ.get("BRIEFS_DIR", "/opt/data/briefs")),
        port=int(os.environ.get("PORT", "8000")),
        matt_user_id=UUID(
            os.environ.get("MATT_USER_ID", "").strip() or str(DEFAULT_MATT_USER_ID)
        ),
        allowed_origins=[
            origin.strip()
            for origin in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
            if origin.strip()
        ],
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
    )

    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Copy server/.env.example to server/.env and fill it in."
        )
    return cfg


_cached: Config | None = None


def get_config() -> Config:
    """Memoized `load_config()`."""
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached
