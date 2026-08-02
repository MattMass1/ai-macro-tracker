"""Environment configuration for the macro tracker service.

Every required variable is checked once at import time of `load_config()` so the
process refuses to start rather than 500-ing on the first request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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


# Defaults that are facts about this specific Notion workspace, not secrets.
DEFAULT_PARENT_PAGE_ID = "3415aac7-7d31-8176-8d64-eb008a13e919"
DEFAULT_NUTRITION_DS_ID = "a9165caa-f1ba-4a3d-9d9b-c850bd1b4c4c"
DEFAULT_NUTRITION_DB_ID = "f450b636-8123-409f-b1e5-960251f377dd"
DEFAULT_FITNESS_DS_ID = "7a35aac7-7d31-82cd-9f2f-072f91d9f61f"
DEFAULT_MAXREPS_DS_ID = "52a10855-2304-4b71-ac09-ab39772268a4"


@dataclass(frozen=True)
class Config:
    notion_token: str
    nutrition_ds_id: str
    targets_ds_id: str
    presets_ds_id: str
    fitness_ds_id: str
    maxreps_ds_id: str
    parent_page_id: str
    app_shared_token: str
    local_tz: str
    day_rollover_hour: int
    briefs_dir: Path
    port: int
    allowed_origins: list[str] = field(default_factory=list)


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
        notion_token=_require("NOTION_TOKEN", missing),
        nutrition_ds_id=_require("NUTRITION_DS_ID", missing, DEFAULT_NUTRITION_DS_ID),
        targets_ds_id=_require("TARGETS_DS_ID", missing),
        presets_ds_id=_require("PRESETS_DS_ID", missing),
        fitness_ds_id=_require("FITNESS_DS_ID", missing, DEFAULT_FITNESS_DS_ID),
        maxreps_ds_id=_require("MAXREPS_DS_ID", missing, DEFAULT_MAXREPS_DS_ID),
        parent_page_id=os.environ.get("PARENT_PAGE_ID", DEFAULT_PARENT_PAGE_ID).strip(),
        app_shared_token=_require("APP_SHARED_TOKEN", missing),
        local_tz=os.environ.get("LOCAL_TZ", "America/New_York").strip(),
        day_rollover_hour=int(os.environ.get("DAY_ROLLOVER_HOUR", "4")),
        briefs_dir=Path(os.environ.get("BRIEFS_DIR", "/opt/data/briefs")),
        port=int(os.environ.get("PORT", "8000")),
        allowed_origins=[
            origin.strip()
            for origin in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
            if origin.strip()
        ],
    )

    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Copy server/.env.example to server/.env and fill them in "
            "(TARGETS_DS_ID and PRESETS_DS_ID are printed by setup.py)."
        )
    return cfg


_cached: Config | None = None


def get_config() -> Config:
    """Memoized `load_config()`."""
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached
