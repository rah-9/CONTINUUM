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
from continuum.core.errors import ContinuumError, PathContainmentError, SpecValidationError

# Never project content: dependency trees, virtualenvs and caches, skipped at any depth.
SKIP_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
                       ".mypy_cache", ".ruff_cache", ".pytest_cache", ".continuum", ".tox",
                       ".idea", ".next"})
# Build output skipped only at the project root (``src/build/`` can be real source).
TOP_LEVEL_BUILD_DIRS = frozenset({"dist", "build", "target"})
MAX_FILES = 50_000
MAX_LISTED_CHANGES = 200


class ObservationError(ContinuumError):
    """The repository could not be observed; never guess a fingerprint."""

    code = "observation_failed"


def _allowed_roots() -> list[Path]:
    raw = os.environ.get("CONTINUUM_CODING_ROOTS", "")
    return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]


def resolve_repo(path: str) -> Path:
    """Resolve and contain a repository path (AGENTS.md: enforce path containment).

    The filesystem root and the home directory itself are never accepted. When
    ``CONTINUUM_CODING_ROOTS`` is set, the repository must be inside one of them."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise SpecValidationError(f"repository path {path!r} is not a directory")
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise PathContainmentError(f"{root} is not a project directory")
    allowed = _allowed_roots()
    if allowed and not any(root == a or root.is_relative_to(a) for a in allowed):
        raise PathContainmentError(f"{root} is outside CONTINUUM_CODING_ROOTS")
    return root


GIT = shutil.which("git")


def _git(root: Path, *args: str) -> bytes | None:
    if GIT is None:
        return None
    try:
        # Constant read-only git subcommands; arguments never come from a model.
        out = subprocess.run([GIT, "-C", str(root), *args],  # noqa: S603
                             capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def _must(root: Path, *args: str) -> bytes:
    out = _git(root, *args)
    if out is None:
        raise ObservationError(f"`git {' '.join(args[:2])}` failed in {root}; "
                               "cannot fingerprint the code")
    return out


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


NOISE_SUFFIXES = (".pyc", ".pyo")


def _is_noise(rel: str) -> bool:
    """Caches that running checks creates (e.g. ``__pycache__``) and top-level build output.

    Without this, merely running the tests changes the fingerprint and no check could
    ever count as "passed on the current code"."""
    parts = rel.rstrip("/").split("/")
    return (any(part in SKIP_DIRS for part in parts) or parts[0] in TOP_LEVEL_BUILD_DIRS
            or rel.endswith(NOISE_SUFFIXES))


def observe(root: Path) -> RepoState:
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside is not None and inside.strip() == b"true":
        return _observe_git(root)
    return _observe_files(root)


# Raw content diffs: no external diff drivers, textconv filters or colour from config.
_DIFF = ("--binary", "--no-ext-diff", "--no-textconv", "--no-color")


def _changed_paths(status: bytes) -> list[str]:
    """Parse ``git status --porcelain=v1 -z``; renames/copies carry an extra source entry."""
    entries = status.decode(errors="replace").split("\0")
    changed: set[str] = set()
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) <= 3:
            continue
        if entry[0] in "RC" or entry[1] in "RC":
            i += 1  # skip the original path of a rename/copy
        if not _is_noise(entry[3:]):
            changed.add(entry[3:])
    return sorted(changed)


def _observe_git(root: Path) -> RepoState:
    head = (_git(root, "rev-parse", "--verify", "-q", "HEAD") or b"").decode().strip() or None
    branch = (_git(root, "rev-parse", "--abbrev-ref", "HEAD") or b"").decode().strip() or None
    h = hashlib.sha256()
    h.update(b"head:" + (head or "none").encode())
    if head:
        h.update(b"\ndiff:" + _must(root, "diff", "HEAD", *_DIFF))
    else:  # no commits yet: the index (staged) and the worktree against it
        h.update(b"\nstaged:" + _must(root, "diff", "--cached", *_DIFF))
        h.update(b"\nunstaged:" + _must(root, "diff", *_DIFF))
    status = _must(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    untracked = [p for p in _must(root, "ls-files", "--others", "--exclude-standard", "-z")
                 .decode(errors="replace").split("\0") if p and not _is_noise(p)]
    for rel in sorted(untracked):
        path = root / rel
        if path.is_symlink():  # hash the link itself, never content outside the repo
            h.update(f"\nuntracked-link:{rel}:{os.readlink(path)}".encode())
        elif path.is_file():
            h.update(f"\nuntracked:{rel}:{_file_sha(path)}".encode())
    changed = _changed_paths(status)
    return RepoState(fingerprint="sha256:" + h.hexdigest(), vcs="git", head=head,
                     branch=branch, dirty=bool(changed),
                     changed_files=changed[:MAX_LISTED_CHANGES])


def _observe_files(root: Path) -> RepoState:
    h = hashlib.sha256()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        top = Path(dirpath) == root
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                             and not (top and d in TOP_LEVEL_BUILD_DIRS))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file() or name.endswith(NOISE_SUFFIXES):
                continue
            count += 1
            if count > MAX_FILES:
                raise SpecValidationError(f"{root} has more than {MAX_FILES} files; use git")
            h.update(f"{path.relative_to(root).as_posix()}:{_file_sha(path)}\n".encode())
    return RepoState(fingerprint="sha256:" + h.hexdigest(), vcs="files")
