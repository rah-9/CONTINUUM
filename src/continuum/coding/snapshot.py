# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Snapshots of a working tree's content, and what changed between two of them (D-042).

Inside git, a snapshot is the git tree id of the working copy's *content*: tracked files
as they are on disk plus untracked, non-ignored files. It is built in a private copy of
the index, so the user's index is never touched, and the tree's objects land in the
repository's own object store. Two snapshots therefore give an exact diff of everything
that changed between them, whichever tool changed it (an edit, a shell command, a
formatter): ``git diff-tree`` between the two trees.

Standard library only: app hooks call this before and after every tool call that can
write, so it must start fast. Commands run without a shell, with constant arguments.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from continuum.core.errors import ContinuumError

# Never project content: dependency trees, virtualenvs and caches, skipped at any depth.
SKIP_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
                       ".mypy_cache", ".ruff_cache", ".pytest_cache", ".continuum", ".tox",
                       ".idea", ".next"})
# Build output skipped only at the project root (``src/build/`` can be real source).
TOP_LEVEL_BUILD_DIRS = frozenset({"dist", "build", "target"})
NOISE_SUFFIXES = (".pyc", ".pyo")
DIFF_MAX_BYTES = 1 << 20          # a stored diff is at most this large (then truncated)
DIFF_MAX_FILES = 500

GIT = shutil.which("git")


class ObservationError(ContinuumError):
    """The repository could not be observed; never guess a fingerprint."""

    code = "observation_failed"


def git(root: Path, *args: str, env: dict[str, str] | None = None,
        stdin: bytes | None = None, timeout: float = 60) -> bytes | None:
    if GIT is None:
        return None
    try:
        # Constant git subcommands; arguments never come from a model.
        out = subprocess.run([GIT, "-C", str(root), *args],  # noqa: S603
                             capture_output=True, timeout=timeout, check=False, input=stdin,
                             env={**os.environ, **env} if env else None)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def must(root: Path, *args: str, env: dict[str, str] | None = None,
         stdin: bytes | None = None) -> bytes:
    out = git(root, *args, env=env, stdin=stdin)
    if out is None:
        raise ObservationError(f"`git {' '.join(args[:2])}` failed in {root}; "
                               "cannot fingerprint the code")
    return out


def is_noise(rel: str) -> bool:
    """Caches that running checks creates (e.g. ``__pycache__``) and top-level build output.

    Without this, merely running the tests changes the fingerprint and no check could
    ever count as "passed on the current code"."""
    parts = rel.rstrip("/").split("/")
    return (any(part in SKIP_DIRS for part in parts) or parts[0] in TOP_LEVEL_BUILD_DIRS
            or rel.endswith(NOISE_SUFFIXES))


def show(raw: bytes) -> str:
    """A path for display/storage: valid UTF-8 always (PostgreSQL rejects surrogates)."""
    return raw.decode("utf-8", errors="replace")


# Fields before the path in `git status --porcelain=v2` records (1: ordinary change,
# 2: rename/copy, followed by the original path as its own NUL-terminated record,
# u: unmerged).
_V2_PATH_FIELD = {b"1": 8, b"2": 9, b"u": 10}


def parse_status(status: bytes) -> tuple[str | None, str | None, list[str], list[bytes]]:
    """Parse ``git status --porcelain=v2 --branch -z --untracked-files=all`` once.

    Returns HEAD's commit (None before the first commit), the branch, the changed paths
    (for display) and the untracked paths (raw bytes; .gitignore already applied)."""
    head: str | None = None
    branch: str | None = None
    changed: set[str] = set()
    untracked: list[bytes] = []
    records = status.split(b"\0")
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if record.startswith(b"# branch.oid "):
            oid = record.split(b" ", 2)[2].decode()
            head = None if oid == "(initial)" else oid
        elif record.startswith(b"# branch.head "):
            branch = record.split(b" ", 2)[2].decode(errors="replace")
        elif record[:2] in (b"? ",) or record[:1] in _V2_PATH_FIELD:
            if record[:2] == b"? ":
                raw = record[2:]
            else:
                raw = record.split(b" ", _V2_PATH_FIELD[record[:1]])[-1]
                if record[:1] == b"2":
                    i += 1  # the original path of a rename/copy
            shown = show(raw)
            if is_noise(shown):
                continue
            changed.add(shown)
            if record[:2] == b"? ":
                untracked.append(raw)
    return head, branch, sorted(changed), untracked


def content_tree(root: Path, gitdir: Path, untracked: list[bytes]) -> str:
    """Git tree id of the working tree's *content*: tracked files as they are on disk plus
    the given untracked files (non-ignored, cache noise already removed).

    Built in a private copy of the index, so the user's index is never touched and
    git's stat cache keeps it cheap. Staging, committing or amending identical content
    yields the same tree; any content change (edit, add, delete, mode, symlink target)
    yields a different one. Names are bytes to git, so any file name works."""
    with tempfile.TemporaryDirectory(prefix="continuum-index-") as tmp:
        index = Path(tmp) / "index"
        env = {"GIT_INDEX_FILE": str(index), "GIT_LITERAL_PATHSPECS": "1"}
        if (gitdir / "index").is_file():  # no index yet: nothing is tracked
            shutil.copyfile(gitdir / "index", index)
            # tracked edits and deletions, whole tree; no pathspec, so newer gits cannot
            # fail with "pathspec did not match" (git 2.55 does in an empty repository)
            must(root, "add", "--update", env=env)
        if untracked:  # exact names from `git status`, as bytes (any encoding)
            must(root, "add", "--pathspec-from-file=-", "--pathspec-file-nul", env=env,
                 stdin=b"\0".join(untracked))
        return must(root, "write-tree", env=env).strip().decode()


def git_dir(path: Path) -> tuple[Path, Path] | None:
    """(work tree top level, absolute git dir) of a git work tree, else None."""
    out = git(path, "rev-parse", "--show-toplevel", "--absolute-git-dir")
    lines = out.splitlines() if out else []
    if len(lines) != 2:
        return None
    return Path(os.fsdecode(lines[0].strip())), Path(os.fsdecode(lines[1].strip()))


def take(path: str | Path) -> dict[str, Any] | None:
    """A snapshot of the work tree containing ``path``: ``{"root", "tree"}``; None outside
    git or when git fails (a snapshot is evidence, never a guess)."""
    found = git_dir(Path(path))
    if found is None:
        return None
    root, gitdir = found
    try:
        status = must(root, "status", "--porcelain=v2", "--branch", "-z",
                      "--untracked-files=all")
        _, _, _, untracked = parse_status(status)
        return {"root": str(root), "tree": content_tree(root, gitdir, untracked)}
    except ObservationError:
        return None


def diff(root: str | Path, before: str, after: str) -> dict[str, Any] | None:
    """What changed between two snapshots: files with status and +/- lines, and the
    unified diff (bounded). None when either tree is no longer available."""
    if before == after:
        return {"files": [], "patch": "", "patch_bytes": 0, "truncated": False}
    root = Path(root)
    numstat = git(root, "diff-tree", "-r", "-z", "--no-renames", "--numstat", before, after)
    status = git(root, "diff-tree", "-r", "-z", "--no-renames", "--name-status", before,
                 after)
    if numstat is None or status is None:
        return None
    kinds: dict[str, str] = {}
    parts = status.split(b"\0")
    for code, raw in zip(parts[0::2], parts[1::2], strict=False):
        if code:
            kinds[show(raw)] = {"A": "added", "D": "deleted"}.get(code[:1].decode(),
                                                                   "modified")
    files: list[dict[str, Any]] = []
    for record in numstat.split(b"\0"):
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            continue
        added, deleted, raw = fields
        path = show(raw)
        if is_noise(path):
            continue
        files.append({"path": path, "status": kinds.get(path, "modified"),
                      "added": int(added) if added.isdigit() else None,   # None: binary
                      "deleted": int(deleted) if deleted.isdigit() else None})
    raw_patch = (git(root, "diff-tree", "-r", "-p", "--no-renames", "--no-color", before,
                     after) or b"") if files else b""
    return {"files": files[:DIFF_MAX_FILES], "files_total": len(files),
            "patch": raw_patch[:DIFF_MAX_BYTES].decode("utf-8", errors="replace"),
            "patch_bytes": len(raw_patch), "truncated": len(raw_patch) > DIFF_MAX_BYTES}
