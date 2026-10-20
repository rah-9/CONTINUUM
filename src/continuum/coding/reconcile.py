# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Turning an unknown outcome into a known one with a read-only probe (D-047).

A call that could act outside the repository and lost its result blocks new external
calls until its outcome is known (D-042, AGENTS.md invariants 5 and 8). For some calls
the outside world can be *asked*, without repeating anything:

* ``git push``: ``git ls-remote`` reads the remote's branch. If it points at exactly the
  commit the push sent, the push took effect: **applied**, with the remote's answer as
  evidence, and the block lifts by itself.

A probe only ever proves presence. A remote that does not (yet) show the commit is not
proof that the push failed (a lagging mirror, a different commit pushed since, a
rejected-then-retried push), so the outcome stays unknown and a person decides, as
before (AGENTS.md invariant 5: "not found" from a lagging query is not absence proof).

Probes run in the background drainer, never on a hook's hot path, with a timeout and
without prompting for credentials. Their arguments come from the recorded command, so
each one is validated (no option injection) and git runs without a shell.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

PROBE_TIMEOUT = 15.0
_SAFE = re.compile(r"^[\w./:@+~-]+$")
_PUSH_FLAGS_WITH_VALUE = frozenset({"--repo", "--receive-pack", "--exec", "-o",
                                    "--push-option", "--signed"})
_UNPROVABLE = frozenset({"--all", "--mirror", "--tags", "--delete", "-d", "--prune",
                         "--follow-tags"})


def push_targets(command: str) -> tuple[str | None, list[tuple[str, str]]] | None:
    """``(remote, [(local ref, remote branch)])`` of a plain ``git push``; None when the
    command is not one, or pushes something a branch comparison cannot prove (tags,
    deletions, mirrors)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for i, word in enumerate(words):
        if word.rsplit("/", 1)[-1] == "git":
            rest = words[i + 1:]
            while rest and rest[0] in ("-C", "-c"):          # git -C dir / -c k=v
                rest = rest[2:]
            if not rest or rest[0] != "push":
                continue
            args = rest[1:]
            if any(a in _UNPROVABLE for a in args) or any(
                    a in ("&&", "||", ";", "|") for a in args):
                args = args[:next((k for k, a in enumerate(args)
                                   if a in ("&&", "||", ";", "|")), len(args))]
                if any(a in _UNPROVABLE for a in args):
                    return None
            positional: list[str] = []
            skip = False
            for a in args:
                if skip:
                    skip = False
                    continue
                if a in _PUSH_FLAGS_WITH_VALUE:
                    skip = True
                    continue
                if a.startswith("-"):
                    continue
                positional.append(a)
            remote = positional[0] if positional else None
            refs: list[tuple[str, str]] = []
            for spec in positional[1:]:
                spec = spec.lstrip("+")
                src, _, dst = spec.partition(":")
                if not src:
                    return None                              # ":branch" deletes
                refs.append((src, (dst or src).removeprefix("refs/heads/")))
            return remote, refs
    return None


def _git(root: Path, *args: str, timeout: float = PROBE_TIMEOUT) -> str | None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "",
           "SSH_ASKPASS": "", "GCM_INTERACTIVE": "never"}
    try:
        out = subprocess.run(["git", "-C", str(root), *args],  # noqa: S603, S607
                             capture_output=True, timeout=timeout, check=False, env=env,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


def probe_push(root: str | Path, command: str,
               pushed: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Evidence that a ``git push`` took effect, or None (still unknown).

    ``pushed``: the commit each local ref pointed at when the push ran, if recorded;
    otherwise the ref's current commit (a later commit makes the comparison fail, which
    keeps the outcome unknown: never a false "applied")."""
    root = Path(root)
    found = push_targets(command)
    if found is None:
        return None
    remote, refs = found
    if not refs:                                           # `git push`: current branch
        branch = (_git(root, "symbolic-ref", "--quiet", "--short", "HEAD") or "").strip()
        if not branch:
            return None
        refs = [(branch, branch)]
        if remote is None:
            remote = (_git(root, "config", "--get", f"branch.{branch}.remote")
                      or "origin").strip()
    remote = remote or "origin"
    if not _SAFE.match(remote) or remote.startswith("-") or any(
            not _SAFE.match(r) or r.startswith("-") for pair in refs for r in pair):
        return None
    evidence = []
    for src, dst in refs:
        local = (pushed or {}).get(src) or (_git(root, "rev-parse", "--verify", "--quiet",
                                                 f"{src}^{{commit}}") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", local):
            return None
        answer = _git(root, "ls-remote", "--heads", remote, f"refs/heads/{dst}")
        if answer is None:
            return None                                    # offline, no access: unknown
        remote_sha = answer.split()[0] if answer.strip() else ""
        if remote_sha != local:
            return None                                    # not shown: still unknown
        evidence.append(f"{remote} refs/heads/{dst} = {local[:12]}")
    return {"outcome": "applied", "probe": "git ls-remote",
            "evidence": "; ".join(evidence)}
