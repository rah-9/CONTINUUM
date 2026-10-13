"""Runtime settings from environment variables (secrets never come from files in git)."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DSN = "postgresql://continuum:continuum@localhost:5432/continuum"


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get("CONTINUUM_DATABASE_URL", DEFAULT_DSN))
    db_schema: str = field(default_factory=lambda: os.environ.get("CONTINUUM_DB_SCHEMA",
                                                                  "public"))
    home: Path = field(default_factory=lambda: Path(os.environ.get("CONTINUUM_HOME",
                                                                   ".continuum")))
    config_dir: Path = field(default_factory=lambda: Path(os.environ.get(
        "CONTINUUM_CONFIG_DIR", "config")))
    owner_id: str = field(default_factory=lambda: os.environ.get(
        "CONTINUUM_OWNER_ID", f"ctl-{socket.gethostname()}-{os.getpid()}"))
    lease_ttl_seconds: int = field(default_factory=lambda: int(os.environ.get(
        "CONTINUUM_LEASE_TTL_SECONDS", "30")))
