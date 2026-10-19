# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""A local, append-only spool between app hooks and the database (D-042).

A hook runs before and after *every* tool call, so it must cost milliseconds, never wait
for the database, and never lose an event when the database is down. It redacts the
payload, writes it to its own file (atomic rename), starts a background drainer if none
is running, and exits. The drainer applies spooled payloads to the journal in order,
deleting each file only after its transaction committed; a payload that cannot be
applied is moved to ``failed/`` and logged, never retried forever.

Standard library only (plus the redactor). The spool lives in ``$CONTINUUM_HOME/spool``
with owner-only permissions.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

LOCK_STALE_SECONDS = 120


def home() -> Path:
    return Path(os.environ.get("CONTINUUM_HOME") or Path.home() / ".continuum")


def spool_dir() -> Path:
    path = home() / "spool"
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def _redact(value: Any, redactor: Any, depth: int = 0) -> Any:
    if depth > 40:
        return None
    if isinstance(value, str):
        return redactor.text(value)
    if isinstance(value, list):
        return [_redact(v, redactor, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v, redactor, depth + 1) for k, v in value.items()}
    return value


def write(entry: dict[str, Any]) -> Path:
    """Spool one entry (redacted first: secrets never reach the disk through us)."""
    from continuum.core.redaction import Redactor

    entry = _redact(entry, Redactor.from_environment())
    folder = spool_dir()
    name = f"{time.time_ns():020d}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
    tmp = folder / (name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False)
    os.replace(tmp, folder / name)
    return folder / name


def pending() -> list[Path]:
    return sorted(p for p in spool_dir().glob("*.json"))


def entries() -> Iterator[tuple[Path, dict[str, Any] | None]]:
    for path in pending():
        try:
            yield path, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            yield path, None


def quarantine(path: Path) -> None:
    failed = spool_dir() / "failed"
    failed.mkdir(exist_ok=True)
    with contextlib.suppress(OSError):
        os.replace(path, failed / path.name)


class Lock:
    """One drainer at a time (an exclusive create, portable; stale after 2 minutes)."""

    def __init__(self) -> None:
        self.path = spool_dir() / "drain.lock"
        self.held = False

    def acquire(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                        self.path.unlink()
                        continue
                except OSError:
                    continue
                return False
        return False

    def touch(self) -> None:
        if self.held:
            with contextlib.suppress(OSError):
                os.utime(self.path)

    def release(self) -> None:
        if self.held:
            with contextlib.suppress(OSError):
                self.path.unlink()
            self.held = False


def start_drainer() -> None:
    """Start a background drainer that outlives the hook (never waits for it)."""
    if os.environ.get("CONTINUUM_HOOKS_SYNC"):
        return
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    with contextlib.suppress(OSError):
        subprocess.Popen([sys.executable, "-m", "continuum.coding.hooks", "--drain"],
                         **kwargs)


# ---- external-effect blocks (AGENTS.md inv. 8) ---------------------------------------

def _block_file(root: str) -> Path:
    digest = hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()
    folder = home() / "blocks"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{digest[:32]}.json"


def set_blocks(root: str, unresolved: list[dict[str, Any]]) -> None:
    """Record the external calls of this repository whose outcome is unknown; the
    ``PreToolUse`` hook reads it (no database on the hot path)."""
    path = _block_file(root)
    external = [{"call_key": u["call_key"], "tool": u["tool"]} for u in unresolved
                if u.get("effect") == "external"]
    if external:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"root": root, "unresolved": external}), encoding="utf-8")
        os.replace(tmp, path)
    elif path.exists():
        with contextlib.suppress(OSError):
            path.unlink()


def blocks(root: str) -> list[dict[str, Any]]:
    path = _block_file(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found = data.get("unresolved") if isinstance(data, dict) else None
    return found if isinstance(found, list) else []
