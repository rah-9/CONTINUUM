# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Snapshots of a working tree's content, and what changed between two of them (D-042).

Inside git, a snapshot is the git tree id of the working copy's *content*: tracked files
as they are on disk plus untracked, non-ignored files. It is built in a private copy of
the index, so the user's index is never touched, and the tree's objects land in the
repository's own object store. Two snapshots therefore give an exact diff of everything
that changed between them, whichever tool changed it (an edit, a shell command, a
formatter): ``git diff-tree`` between the two trees.

Outside git, the same is done in a private *shadow* store, ``$CONTINUUM_HOME/snapshots/``,
with the directory as its work tree: files are found by walking the directory (bounded:
at most ``SHADOW_MAX_FILES`` files, none over ``SHADOW_MAX_FILE_BYTES``), the user's
directory is never written to, and the store is local to this machine.

Standard library only: app hooks call this before and after every tool call that can
write, so it must start fast. Commands run without a shell, with constant arguments.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
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
# Files that usually hold credentials: their changes are listed, their content is never
# stored in a diff (redaction cannot know every secret format)
_SECRET_NAME = re.compile(
    r"(?:^|/)(?:\.env(?:\.[^/]*)?|\.envrc|\.netrc|\.npmrc|\.pypirc|\.pgpass|id_(?:rsa|dsa|"
    r"ecdsa|ed25519)[^/]*|[^/]*\.(?:pem|key|p12|pfx|jks|keystore)|credentials[^/]*|"
    r"secrets?\.(?:json|ya?ml|toml))$", re.I)


def is_secret(rel: str) -> bool:
    return _SECRET_NAME.search(rel) is not None
DIFF_MAX_BYTES = 1 << 20          # a stored diff is at most this large (then truncated)
DIFF_MAX_FILES = 500

GIT = shutil.which("git")
# Namespace of shadow store names, so they never collide with another tool's hash of the
# same path. Changing it orphans existing stores (they are rebuilt on the next snapshot).
_STORE_NAMESPACE = bytes.fromhex("636f6e74696e75756d2d736861646f77")
SHADOW_MAX_FILES = 20_000         # a bigger directory outside git is not snapshotted
SHADOW_MAX_FILE_BYTES = 10 << 20  # nor are files larger than this (data, media)


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
         stdin: bytes | None = None, timeout: float = 60) -> bytes:
    out = git(root, *args, env=env, stdin=stdin, timeout=timeout)
    if out is None:
        raise ObservationError(f"`git {' '.join(args[:2])}` failed in {root}; "
                               "cannot fingerprint the code")
    return out


def _left(deadline: float | None) -> float:
    """Seconds a git call may take within a snapshot's budget."""
    if deadline is None:
        return 60.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ObservationError("the snapshot ran out of time")
    return remaining


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


def content_tree(root: Path, gitdir: Path, untracked: list[bytes],
                 deadline: float | None = None) -> str:
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
            must(root, "add", "--update", env=env, timeout=_left(deadline))
        if untracked:  # exact names from `git status`, as bytes (any encoding)
            must(root, "add", "--pathspec-from-file=-", "--pathspec-file-nul", env=env,
                 stdin=b"\0".join(untracked), timeout=_left(deadline))
        return must(root, "write-tree", env=env,
                    timeout=_left(deadline)).strip().decode()


def git_dir(path: Path) -> tuple[Path, Path] | None:
    """(work tree top level, absolute git dir) of a git work tree, else None."""
    out = git(path, "rev-parse", "--show-toplevel", "--absolute-git-dir")
    lines = out.splitlines() if out else []
    if len(lines) != 2:
        return None
    return Path(os.fsdecode(lines[0].strip())), Path(os.fsdecode(lines[1].strip()))


def take(path: str | Path, budget: float | None = None) -> dict[str, Any] | None:
    """A snapshot of the work tree containing ``path``: ``{"root", "tree"}`` (plus
    ``"git_dir"`` for a shadow store outside git); None when it cannot be taken within
    ``budget`` seconds (a snapshot is evidence, never a guess)."""
    deadline = None if budget is None else time.monotonic() + budget
    found = git_dir(Path(path))
    if found is None:
        return shadow_take(Path(path), deadline)
    root, gitdir = found
    try:
        status = must(root, "status", "--porcelain=v2", "--branch", "-z",
                      "--untracked-files=all", timeout=_left(deadline))
        _, _, _, untracked = parse_status(status)
        return {"root": str(root), "tree": content_tree(root, gitdir, untracked, deadline)}
    except ObservationError:
        return None


def shadow_dir(root: Path) -> Path:
    """The private store that snapshots ``root`` (a directory outside git)."""
    home = Path(os.environ.get("CONTINUUM_HOME") or Path.home() / ".continuum")
    key = hashlib.sha256(_STORE_NAMESPACE + os.fsencode(str(root))).hexdigest()[:24]
    return home / "snapshots" / f"{key}.git"


def remove_shadow(root: Path) -> bool:
    """Delete the shadow store of ``root`` (erasure). Git writes its objects read-only,
    which Windows refuses to delete: make each one writable and retry. True when gone."""
    store = shadow_dir(root)
    if not store.exists():
        return True

    def writable_then_retry(func: Any, path: str, _exc: BaseException) -> None:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)

    with contextlib.suppress(OSError):
        shutil.rmtree(store, onexc=writable_then_retry)
    return not store.exists()


def _walk(root: Path, deadline: float | None = None) -> list[bytes] | None:
    """Relative paths (bytes, ``/``-separated) of the files a shadow snapshot holds; None
    when the directory is too big to snapshot on every tool call."""
    files: list[bytes] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if deadline is not None and time.monotonic() > deadline:
            return None
        top = Path(dirpath) == root
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not (top and d in TOP_LEVEL_BUILD_DIRS)]
        for name in filenames:
            if name.endswith(NOISE_SUFFIXES):
                continue
            full = Path(dirpath) / name
            try:
                info = os.lstat(full)
            except OSError:
                continue
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)) \
                    or info.st_size > SHADOW_MAX_FILE_BYTES:
                continue
            files.append(os.fsencode(full.relative_to(root).as_posix()))
            if len(files) > SHADOW_MAX_FILES:
                return None
    return files


def shadow_take(path: Path, deadline: float | None = None) -> dict[str, Any] | None:
    """Snapshot a directory outside git in its shadow store. Never the home directory or
    a filesystem root: those are not projects, and walking them is not cheap."""
    try:
        root = path.expanduser().resolve()
    except OSError:
        return None
    if not root.is_dir() or root == Path(root.anchor) or root == Path.home().resolve():
        return None
    files = _walk(root, deadline)
    if files is None:
        return None
    store = shadow_dir(root)
    if not (store / "HEAD").is_file():
        store.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(store.parent, 0o700)
        if git(store.parent, "init", "-q", "--bare", store.name) is None:
            return None
    where = (f"--git-dir={store}", f"--work-tree={root}")
    env = {"GIT_INDEX_FILE": str(store / "index"), "GIT_LITERAL_PATHSPECS": "1"}
    try:
        # the index keeps git's stat cache, so unchanged files are not hashed again
        indexed = set(filter(None, must(root, *where, "ls-files", "-z", env=env,
                                        timeout=_left(deadline)).split(b"\0")))
        gone = indexed - set(files)
        if gone:
            must(root, *where, "rm", "-q", "--cached", "--pathspec-from-file=-",
                 "--pathspec-file-nul", env=env, stdin=b"\0".join(sorted(gone)),
                 timeout=_left(deadline))
        if files:
            must(root, *where, "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",
                 env=env, stdin=b"\0".join(files), timeout=_left(deadline))
        tree = must(root, *where, "write-tree", env=env,
                    timeout=_left(deadline)).strip().decode()
    except ObservationError:
        return None
    return {"root": str(root), "tree": tree, "git_dir": str(store)}


def _without_secret_files(patch: bytes) -> bytes:
    """The patch with the content of credential files replaced by a one-line note."""
    if not patch:
        return patch
    parts = re.split(rb"(?m)^(?=diff --git )", patch)
    kept = []
    for part in parts:
        head = part.split(b"\n", 1)[0]
        m = re.match(rb"diff --git a/(.+?) b/", head)
        if m and is_secret(show(m.group(1))):
            kept.append(head + b"\n[content not stored: a credentials file]\n")
        else:
            kept.append(part)
    return b"".join(kept)


def diff(root: str | Path, before: str, after: str,
         store: str | None = None) -> dict[str, Any] | None:
    """What changed between two snapshots: files with status and +/- lines, and the
    unified diff (bounded). ``store``: the shadow store of a directory outside git. None
    when either tree is no longer available."""
    if before == after:
        return {"files": [], "patch": "", "patch_bytes": 0, "truncated": False}
    root = Path(root)
    pre = (f"--git-dir={store}",) if store else ()
    numstat = git(root, *pre, "diff-tree", "-r", "-z", "--no-renames", "--numstat", before, after)
    status = git(root, *pre, "diff-tree", "-r", "-z", "--no-renames", "--name-status", before,
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
    raw_patch = (git(root, *pre, "diff-tree", "-r", "-p", "--no-renames", "--no-color", before,
                     after) or b"") if files else b""
    raw_patch = _without_secret_files(raw_patch)
    return {"files": files[:DIFF_MAX_FILES], "files_total": len(files),
            "patch": raw_patch[:DIFF_MAX_BYTES].decode("utf-8", errors="replace"),
            "patch_bytes": len(raw_patch), "truncated": len(raw_patch) > DIFF_MAX_BYTES}
