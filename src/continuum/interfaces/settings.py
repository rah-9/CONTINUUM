"""Runtime settings from environment variables (secrets never come from files in git)."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DSN = "postgresql://continuum:continuum@localhost:5432/continuum"


def _default_home() -> Path:
    """Per-user, not per-cwd: IDEs launch `continuum mcp` from arbitrary (sometimes
    read-only, e.g. ``/``) working directories."""
    return Path(os.environ.get("CONTINUUM_HOME") or Path.home() / ".continuum").expanduser()


def _default_config_dir() -> Path:
    """``CONTINUUM_CONFIG_DIR``, else ``./config`` when it holds tasks (running from a
    checkout), else ``<home>/config``."""
    if os.environ.get("CONTINUUM_CONFIG_DIR"):
        return Path(os.environ["CONTINUUM_CONFIG_DIR"]).expanduser()
    local = Path("config")
    return local if (local / "tasks").is_dir() else _default_home() / "config"


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get("CONTINUUM_DATABASE_URL", DEFAULT_DSN))
    db_schema: str = field(default_factory=lambda: os.environ.get("CONTINUUM_DB_SCHEMA",
                                                                  "public"))
    home: Path = field(default_factory=_default_home)
    config_dir: Path = field(default_factory=_default_config_dir)
    owner_id: str = field(default_factory=lambda: os.environ.get(
        "CONTINUUM_OWNER_ID", f"ctl-{socket.gethostname()}-{os.getpid()}"))
    lease_ttl_seconds: int = field(default_factory=lambda: int(os.environ.get(
        "CONTINUUM_LEASE_TTL_SECONDS", "30")))
    db_timeout_seconds: float = field(default_factory=lambda: float(os.environ.get(
        "CONTINUUM_DB_TIMEOUT_SECONDS", "10")))
