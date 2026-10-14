"""Observe a working tree: fingerprint and summary, for any kind of project.

Git repositories: fingerprint over HEAD, the full diff against HEAD (staged and
unstaged) and the contents of untracked, non-ignored files. Other directories:
fingerprint over every file's path and content hash, skipping VCS, virtualenv
and dependency/cache directories. Commands run without a shell.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from continuum.coding.models import RepoState
from continuum.core.errors import SpecValidationError

SKIP_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
                       ".mypy_cache", ".ruff_cache", ".pytest_cache", ".continuum", "dist",
                       "build", ".tox", ".idea", ".next", "target"})
MAX_FILES = 50_000
MAX_LISTED_CHANGES = 200


def resolve_repo(path: str) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise SpecValidationError(f"repository path {path!r} is not a directory")
    return root


GIT = shutil.which("git")


def _git(root: Path, *args: str) -> bytes | None:
    if GIT is None:
        return None
    try:
        # Constant read-only git subcommands; arguments never come from a model.
        out = subprocess.run([GIT, "-C", str(root), *args],  # noqa: S603
                             capture_output=True, timeout=60, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


NOISE_SUFFIXES = (".pyc", ".pyo")


def _is_noise(rel: str) -> bool:
    """Caches and dependency/build output that running checks creates (e.g. __pycache__).

    Without this, merely running the tests changes the fingerprint and no check could
    ever count as "passed on the current code"."""
    parts = rel.rstrip("/").split("/")
    return any(part in SKIP_DIRS for part in parts) or rel.endswith(NOISE_SUFFIXES)


def observe(root: Path) -> RepoState:
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside is not None and inside.strip() == b"true":
        return _observe_git(root)
    return _observe_files(root)


def _observe_git(root: Path) -> RepoState:
    head = (_git(root, "rev-parse", "HEAD") or b"").decode().strip() or None
    branch = (_git(root, "rev-parse", "--abbrev-ref", "HEAD") or b"").decode().strip() or None
    diff = _git(root, "diff", "HEAD", "--binary") if head else _git(root, "diff", "--binary")
    status = (_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") or b"")
    untracked = [p for p in (_git(root, "ls-files", "--others", "--exclude-standard", "-z")
                             or b"").decode(errors="replace").split("\0")
                 if p and not _is_noise(p)]
    h = hashlib.sha256()
    h.update(b"head:" + (head or "none").encode())
    h.update(b"\ndiff:" + (diff or b""))
    for rel in sorted(untracked):
        path = root / rel
        if path.is_file():
            h.update(f"\nuntracked:{rel}:{_file_sha(path)}".encode())
    changed = sorted({entry[3:] for entry in status.decode(errors="replace").split("\0")
                      if len(entry) > 3 and not _is_noise(entry[3:])})
    return RepoState(fingerprint="sha256:" + h.hexdigest(), vcs="git", head=head,
                     branch=branch, dirty=bool(changed),
                     changed_files=changed[:MAX_LISTED_CHANGES])


def _observe_files(root: Path) -> RepoState:
    h = hashlib.sha256()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if not path.is_file() or path.is_symlink() or name.endswith(NOISE_SUFFIXES):
                continue
            count += 1
            if count > MAX_FILES:
                raise SpecValidationError(f"{root} has more than {MAX_FILES} files; use git")
            h.update(f"{path.relative_to(root).as_posix()}:{_file_sha(path)}\n".encode())
    return RepoState(fingerprint="sha256:" + h.hexdigest(), vcs="files")
