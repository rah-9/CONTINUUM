"""Observe a working tree: identity, fingerprint and code map, for any kind of project.

Git repositories: the fingerprint is the git tree of the working copy's *content*
(tracked files as they are on disk plus untracked, non-ignored files), so a commit of
the same content is not a change (D-035). The repository's identity is its top-level
directory, and across checkouts its normalized remote URL (D-036). Other
directories: fingerprint over every file's path and content hash, skipping VCS,
virtualenv and dependency/cache directories. Commands run without a shell.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

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


def canonical_root(path: str) -> Path:
    """The directory a session is keyed by: the git top level when ``path`` is inside a
    git work tree, so an agent started in ``repo/src`` and one started in ``repo`` share
    the session. Falls back to ``path`` itself when the top level is not an acceptable
    project directory (e.g. a dotfiles repository in the home directory)."""
    given = resolve_repo(path)
    top = _git(given, "rev-parse", "--show-toplevel")
    if not top or not top.strip():
        return given
    try:
        return resolve_repo(os.fsdecode(top.strip()))
    except (PathContainmentError, SpecValidationError):
        return given


_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")


def normalize_remote(url: str) -> str | None:
    """A credential-free, comparable identity for a remote URL.

    ``https://user:token@GitHub.com/Org/Repo.git``, ``git@github.com:org/repo`` and
    ``ssh://git@github.com/org/repo/`` all become ``github.com/org/repo``."""
    url = url.strip()
    if not url:
        return None
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if scheme.lower() == "file":
            return "file:" + rest.rstrip("/").lower()
        host, _, path = rest.partition("/")
        host = host.rsplit("@", 1)[-1]          # drop user info (and any credentials)
        host = host.split(":", 1)[0]             # drop the port
    elif (m := _SCP_LIKE.match(url)) and not os.path.isabs(url) \
            and not re.match(r"^[A-Za-z]:[\\/]", url):  # not a Windows drive path
        host, path = m.group(1), m.group(2)
    else:
        return "file:" + url.rstrip("/").lower()  # a local path remote
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}/{path}".lower() if host and path else None


def repo_key(root: Path) -> str | None:
    """Identity of the repository across checkouts: its normalized ``origin`` remote (or
    its only remote). ``None`` when there is none: then only the path identifies it."""
    url = _git(root, "config", "--get", "remote.origin.url")
    if not url:
        names = (_git(root, "remote") or b"").split()
        if len(names) != 1:
            return None
        url = _git(root, "config", "--get", f"remote.{names[0].decode()}.url")
    return normalize_remote(url.decode("utf-8", errors="replace")) if url else None


def current_branch(root: Path) -> str | None:
    """The checked-out branch as ``git status`` names it (``(detached)`` without one);
    ``None`` outside git."""
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return None
    ref = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    return ref.decode("utf-8", errors="replace").strip() if ref else "(detached)"


GIT = shutil.which("git")


def _git(root: Path, *args: str, env: dict[str, str] | None = None,
         stdin: bytes | None = None) -> bytes | None:
    if GIT is None:
        return None
    try:
        # Constant git subcommands; arguments never come from a model.
        out = subprocess.run([GIT, "-C", str(root), *args],  # noqa: S603
                             capture_output=True, timeout=60, check=False, input=stdin,
                             env={**os.environ, **env} if env else None)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def _must(root: Path, *args: str, env: dict[str, str] | None = None,
          stdin: bytes | None = None) -> bytes:
    out = _git(root, *args, env=env, stdin=stdin)
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
    probe = _git(root, "rev-parse", "--is-inside-work-tree", "--absolute-git-dir")
    lines = probe.splitlines() if probe else []
    if len(lines) == 2 and lines[0].strip() == b"true":
        return _observe_git(root, Path(os.fsdecode(lines[1].strip())))
    return _observe_files(root)


def _show(raw: bytes) -> str:
    """A path for display/storage: valid UTF-8 always (PostgreSQL rejects surrogates)."""
    return raw.decode("utf-8", errors="replace")


# Fields before the path in `git status --porcelain=v2` records (1: ordinary change,
# 2: rename/copy, followed by the original path as its own NUL-terminated record,
# u: unmerged).
_V2_PATH_FIELD = {b"1": 8, b"2": 9, b"u": 10}


def _parse_status(status: bytes) -> tuple[str | None, str | None, list[str], list[bytes]]:
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
            shown = _show(raw)
            if _is_noise(shown):
                continue
            changed.add(shown)
            if record[:2] == b"? ":
                untracked.append(raw)
    return head, branch, sorted(changed), untracked


def _submodules(root: Path) -> list[bytes]:
    """Paths of gitlinks (mode 160000) checked out in this work tree."""
    if not (root / ".gitmodules").exists():  # the common case: skip listing the index
        return []
    out = _must(root, "ls-files", "--stage", "-z")
    return [line.split(b"\t", 1)[1] for line in out.split(b"\0")
            if line.startswith(b"160000 ") and b"\t" in line]


def _content_tree(root: Path, gitdir: Path, untracked: list[bytes]) -> bytes:
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
            _must(root, "add", "--update", env=env)
        if untracked:  # exact names from `git status`, as bytes (any encoding)
            _must(root, "add", "--pathspec-from-file=-", "--pathspec-file-nul", env=env,
                  stdin=b"\0".join(untracked))
        return _must(root, "write-tree", env=env).strip()


def _observe_git(root: Path, gitdir: Path) -> RepoState:
    status = _must(root, "status", "--porcelain=v2", "--branch", "-z",
                   "--untracked-files=all")
    head, branch, changed, untracked = _parse_status(status)
    # The fingerprint is the code's content, not its history: a commit is not a change.
    h = hashlib.sha256()
    h.update(b"tree:" + _content_tree(root, gitdir, untracked))
    # A gitlink records only the submodule's commit: fingerprint its content too.
    for rel in sorted(_submodules(root)):
        sub = root / os.fsdecode(rel)
        if (sub / ".git").exists():
            h.update(b"\nsubmodule:" + rel + b":" + observe(sub).fingerprint.encode())
    return RepoState(fingerprint="sha256:" + h.hexdigest(), vcs="git", head=head,
                     branch=branch, dirty=bool(changed),
                     changed_files=changed[:MAX_LISTED_CHANGES])


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree
MAP_FILES = 40
MAP_NAMES = 8
# A definition on an added line, in most languages (def/class/function/fn/struct/...).
_DEFINITION = re.compile(
    rb"^\+\s*(?:export\s+|pub(?:\([a-z]+\))?\s+|public\s+|private\s+|static\s+|async\s+)*"
    rb"(?:def|class|function|func|fn|struct|enum|interface|trait|type|impl|module)\s+"
    rb"([A-Za-z_$][\w$]*)")


def session_changes(root: Path, base: str | None) -> dict[str, Any] | None:
    """Where this session worked, observed from git (no model involved): every file that
    differs from the session's starting commit (committed or not, plus untracked files),
    with line counts, the enclosing context git reports for each hunk, and names defined
    on added lines. Lets the next model read the session's code instead of exploring the
    repository. ``None`` when unknown (e.g. the starting commit is gone)."""
    ref = base or EMPTY_TREE
    flags = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")
    numstat = _git(root, "diff", "--numstat", "-z", *flags, ref)
    patch = _git(root, "-c", "core.quotepath=off", "diff", "-U0", *flags, ref)
    status = _git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    if numstat is None or patch is None or status is None:
        return None
    files: dict[str, dict[str, Any]] = {}
    for record in numstat.split(b"\0"):
        parts = record.split(b"\t", 2)
        if len(parts) == 3 and parts[2]:
            path = _show(parts[2])
            if not _is_noise(path):
                files[path] = {"path": path, "status": "modified",
                               "added": int(parts[0]) if parts[0].isdigit() else 0,
                               "deleted": int(parts[1]) if parts[1].isdigit() else 0,
                               "near": [], "defines": []}
    current: dict[str, Any] | None = None
    for line in patch.split(b"\n"):
        if line.startswith(b"+++ "):
            name = line[4:]
            current = files.get(_show(name[2:] if name.startswith(b"b/") else name))
        elif current is None:
            continue
        elif line.startswith(b"@@"):
            context = line.split(b"@@", 2)[-1].strip()
            near = _show(context)[:80]
            if near and near not in current["near"] and len(current["near"]) < MAP_NAMES:
                current["near"].append(near)
        elif line.startswith(b"+") and (m := _DEFINITION.match(line)):
            defined = _show(m.group(1))
            if defined not in current["defines"] and len(current["defines"]) < MAP_NAMES:
                current["defines"].append(defined)
    _, _, _, untracked = _parse_status(status)
    for raw in untracked:
        path = _show(raw)
        file = root / os.fsdecode(raw)
        files[path] = {"path": path, "status": "untracked", "deleted": 0, "near": [],
                       **_scan_new_file(file)}
    ranked = sorted(files.values(), key=lambda f: (-(f["added"] + f["deleted"]), f["path"]))
    return {"since": base, "basis": "git", "total_files": len(ranked),
            "files": ranked[:MAP_FILES]}


SCAN_BYTES = 256 * 1024  # a new file's definitions are read from its first 256 KiB


def _scan_new_file(file: Path) -> dict[str, Any]:
    """Line count and definitions of a new file, reading a bounded prefix (an untracked
    1 GB data file must not be read into memory to build a code map)."""
    try:
        if not file.is_file() or file.is_symlink():
            return {"added": 0, "defines": []}
        with file.open("rb") as fh:
            head = fh.read(SCAN_BYTES)
            lines = head.count(b"\n")
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                lines += chunk.count(b"\n")
    except OSError:
        return {"added": 0, "defines": []}
    defines = list(dict.fromkeys(_show(m.group(1)) for m in (
        _DEFINITION.match(b"+" + ln) for ln in head.splitlines()) if m))[:MAP_NAMES]
    return {"added": lines, "defines": defines}


def files_changed_since(root: Path, since: float) -> dict[str, Any]:
    """The code map outside git: files modified after ``since`` (POSIX time), by mtime.
    Deletions cannot be seen without a VCS; the map says so (``basis``)."""
    found: list[dict[str, Any]] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        top = Path(dirpath) == root
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                             and not (top and d in TOP_LEVEL_BUILD_DIRS))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            count += 1
            if count > MAX_FILES or name.endswith(NOISE_SUFFIXES) or path.is_symlink():
                continue
            try:
                if path.stat().st_mtime <= since:
                    continue
            except OSError:
                continue
            found.append({"path": path.relative_to(root).as_posix(), "status": "changed",
                          "deleted": 0, "near": [], **_scan_new_file(path)})
    found.sort(key=lambda f: (-f["added"], f["path"]))
    return {"since": None, "basis": "mtime", "total_files": len(found),
            "files": found[:MAP_FILES]}


def commits_since(root: Path, base: str, limit: int = 20) -> list[dict[str, str]] | None:
    """Commits on HEAD since ``base`` (newest first), observed from git.

    ``None`` when unknown: ``base`` is gone or is no longer an ancestor of HEAD (history
    was rewritten). Informational only; verification never depends on it."""
    if _git(root, "merge-base", "--is-ancestor", base, "HEAD") is None:
        return None
    out = _git(root, "log", "--no-show-signature", "--format=%h%x1f%s", "-z",
               f"-n{limit}", f"{base}..HEAD")
    if out is None:
        return None
    commits = []
    for record in out.split(b"\0"):
        sha, _, subject = record.partition(b"\x1f")
        if sha:
            commits.append({"sha": sha.decode().strip(), "subject": _show(subject)})
    return commits


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
            rel = os.fsencode(path.relative_to(root).as_posix())  # names may not be UTF-8
            h.update(rel + b":" + _file_sha(path).encode() + b"\n")
    return RepoState(fingerprint="sha256:" + h.hexdigest(), vcs="files")
