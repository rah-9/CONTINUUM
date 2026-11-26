#!/usr/bin/env python3
"""agent-continuity: keep coding work continuous across models, chats and apps.

A standalone agent skill. One file, Python 3.9+, standard library only, no server.
Everything is stored in ``.continuity/journal.jsonl`` inside the project (append-only,
one JSON event per line; kept out of git through ``.git/info/exclude``).

It keeps, outside the chat:
  * what the user asked, in their own words (recorded notes + messages captured from
    Claude Code / Codex chat transcripts on this machine);
  * decisions, context, blockers, checkpoints and next steps;
  * checks (test commands) and whether each one passed *on the current code*: a pass is
    bound to a content fingerprint of the files, so an edit makes it stale while a
    commit alone does not;
  * tasks that can be marked done only when their checks pass on the current code;
  * a ledger of external effects (push, deploy, publish, emails, payments ...): an
    effect whose result was lost is *unknown*, and new external actions are refused
    until it is verified by evidence or the user decides.

``resume`` prints a compact brief for the next model. Run ``--help`` on any command.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

VERSION = "1.0.0"
STORE_DIR = ".continuity"
EXIT_REFUSED = 3
NOTE_KINDS = ("requirement", "decision", "context", "blocker", "next_step")
OUTCOMES = ("done", "failed", "unknown")
DEFAULT_BUDGET = 12000          # characters of brief (about 3k tokens)
MESSAGE_CAP = 4000              # longest captured message shown whole in the brief
OUTPUT_TAIL = 3000              # check output kept inline in the journal
CHECK_TIMEOUT = 1800

Event = Dict[str, Any]


class Refused(Exception):
    """A safety rule refused the action (exit code 3)."""


class Failure(Exception):
    """The command could not run (exit code 1)."""


# --------------------------------------------------------------------------- redaction

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]
_ASSIGNMENT = re.compile(
    r"(?i)\b((?:[a-z0-9]+_)*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key))"
    r"(\s*[:=]\s*)(['\"]?)([^\s'\"]{4,})\3")
_URL_CREDENTIAL = re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^@\s/]+)(@)", re.I)


def redact(text: str) -> str:
    """Mask API keys, tokens, passwords and private keys before anything is stored."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = _ASSIGNMENT.sub(
        lambda m: m.group(1) + m.group(2) + m.group(3) + "[REDACTED]" + m.group(3), text)
    return _URL_CREDENTIAL.sub(r"\1[REDACTED]\3", text)


# --------------------------------------------------------------------------- helpers

def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_git(root: Path, *args: str, stdin: Optional[str] = None) -> Optional[str]:
    try:
        proc = subprocess.run(["git", *args], cwd=str(root), input=stdin, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def find_root(start: Path) -> Tuple[Path, bool]:
    top = run_git(start, "rev-parse", "--show-toplevel")
    if top and top.strip():
        return Path(top.strip()), True
    return start.resolve(), False


def short(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def one_line(text: str, limit: int = 200) -> str:
    return short(" ".join(text.split()), limit)


# --------------------------------------------------------------------------- store

class Store:
    def __init__(self, root: Path, is_git: bool) -> None:
        self.root = root
        self.is_git = is_git
        self.dir = root / STORE_DIR
        self.path = self.dir / "journal.jsonl"
        self._events: Optional[List[Event]] = None

    def ensure(self) -> None:
        (self.dir / "outputs").mkdir(parents=True, exist_ok=True)
        if not self.is_git:
            return
        rel = run_git(self.root, "rev-parse", "--git-path", "info/exclude")
        if not rel:
            return
        exclude = Path(rel.strip())
        if not exclude.is_absolute():
            exclude = self.root / exclude
        line = "/" + STORE_DIR + "/"
        try:
            existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if line not in existing.split("\n"):
                exclude.parent.mkdir(parents=True, exist_ok=True)
                with exclude.open("a", encoding="utf-8") as fh:
                    sep = "" if existing.endswith("\n") or not existing else "\n"
                    fh.write(sep + line + "\n")
        except OSError:
            pass

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.ensure()
        with (self.dir / ".lock").open("a+") as fh:
            try:
                import fcntl
            except ImportError:  # Windows: single-writer use is assumed
                yield
                return
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def events(self) -> List[Event]:
        if self._events is None:
            self._events = []
            if self.path.exists():
                with self.path.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        with contextlib.suppress(ValueError):
                            event = json.loads(line)
                            if isinstance(event, dict) and isinstance(event.get("id"), int):
                                self._events.append(event)
        return self._events

    def append(self, kind: str, agent: Optional[str], **data: Any) -> Event:
        with self.lock():
            self._events = None
            events = self.events()
            last = events[-1]["id"] if events else 0
            event: Event = {"id": last + 1, "ts": now(), "kind": kind, "agent": agent}
            event.update(data)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            events.append(event)
            return event


# --------------------------------------------------------------------------- fingerprint

_SKIP_DIRS = {".git", STORE_DIR, "node_modules", "__pycache__", ".venv", "venv", ".tox",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
              ".coverage", "htmlcov", ".nyc_output", "coverage", "target", ".gradle"}
_SKIP_SUFFIXES = (".pyc", ".pyo", ".class", ".o", ".log", ".tmp")


def _ignored(rel_parts: Tuple[str, ...]) -> bool:
    """Caches and build output that running a check creates; not the code under test."""
    return any(part in _SKIP_DIRS for part in rel_parts) or \
        (bool(rel_parts) and rel_parts[-1].endswith(_SKIP_SUFFIXES))


def fingerprint(store: Store, paths: List[str]) -> str:
    """Content fingerprint of the project files (or of ``paths``).

    Based on file contents, not on commits: committing unchanged files keeps a check
    fresh, while any edit, new file or deletion in scope makes it stale.
    """
    digest = hashlib.sha256()
    root = store.root
    if store.is_git:
        listed = run_git(root, "ls-files", "-z", "-co", "--exclude-standard", "--",
                         *(paths or ["."]))
        if listed is None:
            raise Failure("git ls-files failed; cannot fingerprint the code")
        files = sorted({f for f in listed.split("\0")
                        if f and "\n" not in f and not _ignored(tuple(f.split("/")))
                        and (root / f).is_file()})
        if files:
            blobs = run_git(root, "hash-object", "--stdin-paths", stdin="\n".join(files) + "\n")
            if blobs is None:
                raise Failure("git hash-object failed; cannot fingerprint the code")
            for name, blob in zip(files, blobs.split()):
                digest.update(f"{name}\0{blob}\n".encode())
    else:
        targets = [root / p for p in paths] if paths else [root]
        seen = 0
        for target in targets:
            candidates = [target] if target.is_file() else sorted(target.rglob("*"))
            for path in candidates:
                rel = path.relative_to(root)
                if _ignored(rel.parts) or not path.is_file():
                    continue
                seen += 1
                if seen > 50000:
                    raise Failure("too many files to fingerprint; pass --paths to the check")
                digest.update(f"{rel.as_posix()}\0".encode())
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + digest.hexdigest()[:24]


class Fingerprints:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.cache: Dict[Tuple[str, ...], str] = {}

    def get(self, paths: List[str]) -> str:
        key = tuple(paths)
        if key not in self.cache:
            self.cache[key] = fingerprint(self.store, paths)
        return self.cache[key]


# --------------------------------------------------------------------------- state

class State:
    """Everything derived from the journal. Nothing here is stored separately."""

    def __init__(self, events: List[Event]) -> None:
        self.events = events
        self.by_id = {e["id"]: e for e in events}
        starts = [e for e in events if e["kind"] == "session_start"]
        self.session: Optional[Event] = starts[-1] if starts else None
        first = self.session["id"] if self.session else 0
        self.session_events = [e for e in events if e["id"] >= first]

        self.checks: Dict[str, Event] = {}
        self.check_history: Dict[str, List[Event]] = {}
        self.runs: Dict[str, Event] = {}
        self.effects: Dict[int, Dict[str, Any]] = {}
        self.said_idents = set()
        for e in events:
            kind = e["kind"]
            if kind == "check_added":
                self.checks[e["name"]] = e
                self.check_history.setdefault(e["name"], []).append(e)
            elif kind == "check_removed":
                self.checks.pop(e["name"], None)
            elif kind == "check_run":
                self.runs[e["name"]] = e
            elif kind == "effect_begin":
                self.effects[e["id"]] = {"begin": e, "end": None, "verify": None,
                                         "resolve": None}
            elif kind in ("effect_end", "effect_verify", "effect_resolve"):
                eff = self.effects.get(int(e.get("effect") or 0))
                if eff is not None:
                    eff[kind.split("_", 1)[1]] = e
            elif kind == "user_said" and e.get("ident"):
                self.said_idents.add(e["ident"])

        hidden = {e["target"] for e in self.session_events if e["kind"] == "retract"}
        hidden |= {e["replaces"] for e in self.session_events
                   if e["kind"] in ("note", "user_said") and e.get("replaces")}
        self.hidden = hidden
        self.notes = [e for e in self.session_events
                      if e["kind"] == "note" and e["id"] not in hidden]
        self.said = [e for e in self.session_events
                     if e["kind"] == "user_said" and e["id"] not in hidden]
        self.checkpoints = [e for e in self.session_events if e["kind"] == "checkpoint"]
        self.tasks: Dict[int, Dict[str, Any]] = {}
        for e in self.session_events:
            if e["kind"] == "task_added":
                self.tasks[e["id"]] = {"task": e, "done": None}
            elif e["kind"] == "task_done" and e.get("task") in self.tasks:
                self.tasks[e["task"]]["done"] = e
            elif e["kind"] == "task_reopened" and e.get("task") in self.tasks:
                self.tasks[e["task"]]["done"] = None

    # -- effects
    @staticmethod
    def effect_status(eff: Dict[str, Any]) -> str:
        if eff["resolve"]:
            return "done (user decided)" if eff["resolve"]["done"] else "not done (user decided)"
        if eff["verify"] and eff["verify"].get("confirmed"):
            return "done (verified)"
        if eff["end"] and eff["end"]["outcome"] in ("done", "failed"):
            return str(eff["end"]["outcome"])
        return "UNKNOWN"

    def unknown_effects(self) -> List[Dict[str, Any]]:
        return [eff for eff in self.effects.values() if self.effect_status(eff) == "UNKNOWN"]

    # -- checks
    def check_status(self, name: str, fps: Fingerprints) -> Tuple[str, Optional[Event]]:
        """passed | stale | failed | changed | never_run (``changed``: edited since run)."""
        check = self.checks[name]
        run = self.runs.get(name)
        if run is None:
            return "never_run", None
        if run.get("check") != check["id"]:
            return "changed", run
        if not run["passed"]:
            return "failed", run
        if run["fingerprint"] != fps.get(check.get("paths") or []):
            return "stale", run
        return "passed", run


# --------------------------------------------------------------------------- transcripts

_INJECTED_TAGS = ("system-reminder", "environment_context", "user_instructions",
                  "user-prompt-submit-hook", "command-message", "command-name", "command-args",
                  "local-command-stdout", "local-command-stderr", "local-command-caveat",
                  "ide_selection", "ide_opened_file", "ide_diagnostics", "INSTRUCTIONS",
                  "turn_aborted", "task-notification", "bash-input", "bash-stdout",
                  "bash-stderr")
_INJECTED_BLOCK = re.compile(
    r"<(" + "|".join(re.escape(t) for t in _INJECTED_TAGS) + r")(?:\s[^>]*)?>.*?</\1>", re.S)
_INJECTED_PREFIXES = ("Caveat: The messages below were generated", "# AGENTS.md instructions",
                      "[Request interrupted by user", "<environment_context>",
                      "This session is being continued from a previous conversation",
                      "<user_instructions>")


def clean_user_text(text: str) -> str:
    text = _INJECTED_BLOCK.sub("", text).strip()
    return "" if any(text.startswith(p) for p in _INJECTED_PREFIXES) else text


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    if any(isinstance(p, dict) and p.get("type") in ("tool_result", "function_call_output")
           for p in content):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str) and \
                part.get("type", "text") in ("text", "input_text"):
            parts.append(part["text"])
    return "\n".join(parts)


def user_messages(path: Path) -> Iterator[Tuple[str, Optional[str], str]]:
    """(ident, timestamp, text) of each message the person typed, from a JSONL transcript.

    Understands Claude Code (``type: user``), Codex rollouts (``response_item``) and plain
    ``{"role": "user", "content": ...}`` lines. Unknown lines are skipped, never fatal.
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isMeta") or rec.get("isSidechain") \
                    or rec.get("isCompactSummary"):
                continue
            msg: Any = None
            if rec.get("type") == "user" and isinstance(rec.get("message"), dict):
                msg = rec["message"]
            elif rec.get("type") == "response_item" and isinstance(rec.get("payload"), dict):
                msg = rec["payload"]
            elif "role" in rec:
                msg = rec
            if not isinstance(msg, dict) or msg.get("role") not in ("user", "human"):
                continue
            text = clean_user_text(_content_text(msg.get("content")))
            if not text:
                continue
            ident = rec.get("uuid") or hashlib.sha256(
                f"{path.name}:{lineno}:{text}".encode()).hexdigest()[:32]
            yield str(ident), rec.get("timestamp"), text


def _claude_transcripts(root: Path) -> List[Path]:
    base = Path.home() / ".claude" / "projects"
    folder = base / re.sub(r"[^A-Za-z0-9]", "-", str(root))
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


def _codex_transcripts(root: Path, since: float) -> List[Path]:
    base = Path.home() / ".codex" / "sessions"
    if not base.is_dir():
        return []
    found = []
    for path in sorted(base.rglob("rollout-*.jsonl"))[-300:]:
        if path.stat().st_mtime < since:
            continue
        with contextlib.suppress(OSError, ValueError):
            with path.open(encoding="utf-8", errors="replace") as fh:
                head = json.loads(fh.readline() or "{}")
            cwd = (head.get("payload") or {}).get("cwd") if isinstance(head, dict) else None
            if cwd and Path(cwd).resolve() == root.resolve():
                found.append(path)
    return found


# --------------------------------------------------------------------------- brief

def _ts_epoch(ts: Optional[str]) -> float:
    if not ts:
        return 0.0
    with contextlib.suppress(ValueError):
        return _dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    return 0.0


def _where_is_the_code(store: Store, state: State) -> List[str]:
    if not store.is_git:
        return ["- not a git repository; check results are bound to file contents"]
    root = store.root
    lines = []
    branch = (run_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "?").strip()
    head = (run_git(root, "log", "-1", "--format=%h %s") or "(no commits)").strip()
    lines.append(f"- branch `{branch}`, HEAD {head}")
    status = (run_git(root, "status", "--porcelain") or "").splitlines()
    if status:
        shown = ", ".join(s[3:] for s in status[:15])
        more = f" (+{len(status) - 15} more)" if len(status) > 15 else ""
        lines.append(f"- uncommitted: {shown}{more}")
    start = state.session.get("commit") if state.session else None
    if start:
        log = (run_git(root, "log", "--format=%h %s", f"{start}..HEAD") or "").splitlines()
        if log:
            lines.append(f"- {len(log)} commit(s) this session: " + "; ".join(log[:8])
                         + (" …" if len(log) > 8 else ""))
        changed = (run_git(root, "diff", "--name-only", start) or "").split()
        if changed:
            lines.append("- files changed this session: " + ", ".join(changed[:25])
                         + (f" (+{len(changed) - 25} more)" if len(changed) > 25 else ""))
    return lines


def build_brief(store: Store, state: State, budget: int, full: bool) -> str:
    fps = Fingerprints(store)
    head: List[str] = []
    name = store.root.name
    head.append(f"# Continuity brief: {name}")
    if state.session:
        s = state.session
        head.append(f"Goal: {s.get('goal') or '(not set)'}  ")
        head.append(f"Session #{s['id']} started {s['ts']} by {s.get('agent') or 'unknown agent'}.")
    else:
        head.append("No session started yet (`continuity.py start --goal ...`).")
    head.append("Facts = observed by this tool (checks, git, effect ledger). "
                "Notes and checkpoints = claims by earlier agents; confirm before relying.")

    unknown = state.unknown_effects()
    essential: List[str] = []
    if unknown:
        essential.append("\n## ⚠ UNKNOWN EXTERNAL EFFECTS: STOP")
        essential.append("These actions may or may not have happened. Do NOT repeat them and do "
                         "NOT start any other external action. Verify with `effect verify ID` "
                         "or ask the user to decide (`effect resolve`).")
        for eff in unknown:
            b = eff["begin"]
            end = eff["end"]
            detail = f"; reported: {one_line(end.get('result') or '', 160)}" if end else \
                "; never finished (session ended, crashed or timed out)"
            verify = f"; verify with `{b['verify']}`" if b.get("verify") else ""
            essential.append(f"- effect #{b['id']} `{one_line(b['what'], 160)}` "
                             f"({b['ts']}){detail}{verify}")

    reqs = [n for n in state.notes if n["note_kind"] == "requirement"]
    essential.append("\n## What the user asked (their words; a higher # wins a conflict)")
    if not reqs and not state.said:
        essential.append("- nothing recorded. Ask the user, or run `capture`.")
    for n in reqs:
        essential.append(f"- [#{n['id']}] {n['text']}")

    nexts = [n for n in state.notes if n["note_kind"] == "next_step"]
    if nexts:
        essential.append("\n## Next step (from the last agent)")
        essential.append(f"- [#{nexts[-1]['id']}] {nexts[-1]['text']}")
        for n in nexts[-4:-1]:
            essential.append(f"- earlier: [#{n['id']}] {one_line(n['text'])}")

    if state.tasks:
        essential.append("\n## Tasks")
        for tid, t in state.tasks.items():
            task = t["task"]
            checks = task.get("checks") or []
            statuses = []
            all_passed = True
            for c in checks:
                st = state.check_status(c, fps)[0] if c in state.checks else "missing"
                statuses.append(f"{c}: {st}")
                all_passed = all_passed and st == "passed"
            if t["done"] and checks and not all_passed:
                mark = "[!] done earlier, NEEDS RE-VERIFICATION (code or check changed)"
            elif t["done"]:
                mark = "[x] done" + ("" if checks else " (no checks: unverified claim)")
            else:
                mark = "[ ] open"
            suffix = f" ({', '.join(statuses)})" if statuses else ""
            essential.append(f"- #{tid} {mark}: {task['title']}{suffix}")

    if state.checks:
        essential.append("\n## Checks (re-evaluated now against the current code)")
        for cname, check in state.checks.items():
            st, run = state.check_status(cname, fps)
            label = {"passed": "PASSED on the current code",
                     "stale": "passed earlier, code changed since: re-run",
                     "failed": "FAILED", "changed": "check was changed since its last run",
                     "never_run": "never run"}[st]
            line = f"- `{cname}` = `{one_line(check['cmd'], 120)}`: {label}"
            if run:
                line += f" (run #{run['id']}, {run['ts']}, exit {run['exit']})"
            if len(state.check_history.get(cname, [])) > 1:
                line += f" (edited {len(state.check_history[cname]) - 1}x)"
            essential.append(line)
            if st == "failed" and run:
                tail = run.get("tail", "").strip().splitlines()[-6:]
                essential.extend("    " + one_line(t, 160) for t in tail)

    decisions = [n for n in state.notes if n["note_kind"] == "decision"]
    blockers = [n for n in state.notes if n["note_kind"] == "blocker"]
    for title, items in (("Decisions", decisions), ("Blockers", blockers)):
        if items:
            essential.append(f"\n## {title}")
            for n in items:
                why = f" (because: {n['reason']})" if n.get("reason") else ""
                essential.append(f"- [#{n['id']}] {n['text']}{why}")

    essential.append("\n## Where the code is")
    essential.extend(_where_is_the_code(store, state))
    if state.checkpoints:
        cp = state.checkpoints[-1]
        essential.append(f"\n## Last checkpoint (#{cp['id']}, {cp['ts']}, "
                         f"{cp.get('agent') or 'unknown agent'})")
        essential.append(cp["summary"])

    done_effects = [eff for eff in state.effects.values() if eff not in unknown]
    if done_effects:
        essential.append("\n## External effects already recorded (do not repeat what is done)")
        for eff in done_effects[-8:]:
            b = eff["begin"]
            key = f" key={b['key']}" if b.get("key") else ""
            essential.append(f"- #{b['id']} {state.effect_status(eff)}: "
                             f"`{one_line(b['what'], 120)}`{key}")

    # Optional sections, trimmed to the budget: captured messages, then context notes.
    optional_said: List[str] = []
    if state.said:
        optional_said.append("\n## Messages captured from the chats (oldest first)")
        for e in state.said:
            text = e["text"] if full or len(e["text"]) <= MESSAGE_CAP else (
                e["text"][:MESSAGE_CAP] + f"… [cut; full text: `show {e['id']}`]")
            optional_said.append(f"- [#{e['id']}] ({e.get('source', 'chat')}"
                                 f"{', ' + e['at'] if e.get('at') else ''}) {text}")
    context = [n for n in state.notes if n["note_kind"] == "context"]
    optional_ctx: List[str] = []
    if context:
        optional_ctx = ["\n## Context notes"] + [f"- [#{n['id']}] {n['text']}" for n in context]

    out = head + essential
    used = sum(len(x) + 1 for x in out)
    if full:
        return "\n".join(out + optional_said + optional_ctx) + "\n"
    for section in (optional_said, optional_ctx):
        if not section:
            continue
        title, entries = section[0], section[1:]
        kept: List[str] = []
        room = budget - used - len(title) - 80
        # keep the first item (usually the original goal) and as many of the newest as fit
        first: Optional[str] = entries[0] if entries else None
        rest = list(reversed(entries[1:]))
        if first is not None and len(first) < room:
            room -= len(first) + 1
        else:
            first = None
        for item in rest:
            if len(item) + 1 > room:
                break
            kept.append(item)
            room -= len(item) + 1
        kept.reverse()
        omitted = len(entries) - len(kept) - (1 if first else 0)
        block = [title] + ([first] if first else [])
        if omitted:
            block.append(f"- … {omitted} item(s) omitted for size; `search WORDS` or "
                         f"`resume --full` finds them")
        block += kept
        if len(block) > 1:
            out += block
            used += sum(len(x) + 1 for x in block)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- commands

def cmd_start(store: Store, state: State, args: argparse.Namespace) -> int:
    commit = (run_git(store.root, "rev-parse", "HEAD") or "").strip() if store.is_git else ""
    e = store.append("session_start", args.agent, goal=redact(args.goal), commit=commit or None)
    print(f"Session #{e['id']} started. Record the user's requirements with "
          f"`note requirement \"<their exact words>\"`.")
    return 0


def cmd_note(store: Store, state: State, args: argparse.Namespace) -> int:
    if args.replaces and args.replaces not in state.by_id:
        raise Failure(f"#{args.replaces} does not exist")
    data: Dict[str, Any] = {"note_kind": args.note_kind, "text": redact(args.text)}
    if args.reason:
        data["reason"] = redact(args.reason)
    if args.replaces:
        data["replaces"] = args.replaces
    e = store.append("note", args.agent, **data)
    print(f"Noted #{e['id']} ({args.note_kind}).")
    return 0


def cmd_retract(store: Store, state: State, args: argparse.Namespace) -> int:
    target = state.by_id.get(args.id)
    if not target or target["kind"] not in ("note", "user_said"):
        raise Failure(f"#{args.id} is not a note or captured message")
    store.append("retract", args.agent, target=args.id, reason=redact(args.reason or ""))
    print(f"#{args.id} is no longer shown as current (still searchable).")
    return 0


def cmd_checkpoint(store: Store, state: State, args: argparse.Namespace) -> int:
    e = store.append("checkpoint", args.agent, summary=redact(args.summary))
    print(f"Checkpoint #{e['id']} recorded.")
    return 0


def cmd_check_add(store: Store, state: State, args: argparse.Namespace) -> int:
    parts = list(args.cmd_args)
    if parts[:1] == ["--"]:
        parts = parts[1:]
    # one quoted string is used as written; separate words are re-quoted for the shell
    cmd = (parts[0] if len(parts) == 1 else shlex.join(parts)).strip()
    if not cmd:
        raise Failure("give the command after the name, e.g. `check add tests -- pytest -q`")
    prev = state.checks.get(args.name)
    e = store.append("check_added", args.agent, name=args.name, cmd=cmd,
                     paths=args.paths or [], by=args.by)
    verb = "changed" if prev else "added"
    print(f"Check `{args.name}` {verb} (#{e['id']}). Run it with `check run {args.name}`.")
    return 0


def cmd_check_remove(store: Store, state: State, args: argparse.Namespace) -> int:
    if args.name not in state.checks:
        raise Failure(f"no check named `{args.name}`")
    store.append("check_removed", args.agent, name=args.name, by=args.by)
    print(f"Check `{args.name}` removed.")
    return 0


def cmd_check_run(store: Store, state: State, args: argparse.Namespace) -> int:
    names = args.names or list(state.checks)
    if not names:
        raise Failure("no checks registered; the user adds them with `check add`")
    worst = 0
    for name in names:
        check = state.checks.get(name)
        if check is None:
            raise Failure(f"no check named `{name}`")
        fp_before = fingerprint(store, check.get("paths") or [])
        started = time.monotonic()
        try:
            proc = subprocess.run(check["cmd"], shell=True, cwd=str(store.root),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=args.timeout)
            exit_code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            out = exc.stdout or ""
            output = (out.decode("utf-8", "replace") if isinstance(out, bytes) else out) + \
                f"\n[timed out after {args.timeout}s]"
        duration = round(time.monotonic() - started, 2)
        fp_after = fingerprint(store, check.get("paths") or [])
        output = redact(output)
        passed = exit_code == 0
        e = store.append("check_run", args.agent, name=name, check=check["id"], exit=exit_code,
                         passed=passed, fingerprint=fp_after, seconds=duration,
                         tail=output[-OUTPUT_TAIL:])
        (store.dir / "outputs" / f"{e['id']}.log").write_text(output, encoding="utf-8")
        if fp_before != fp_after:
            print(f"`{name}`: note: project files changed while it ran; the result is bound "
                  f"to the files as they are now.")
        print(f"`{name}`: {'PASSED' if passed else 'FAILED'} (exit {exit_code}, {duration}s, "
              f"run #{e['id']}; full output: {STORE_DIR}/outputs/{e['id']}.log)")
        if not passed:
            worst = 1
            print("\n".join(output.strip().splitlines()[-15:]))
    return worst


def cmd_task_add(store: Store, state: State, args: argparse.Namespace) -> int:
    checks = [c for c in (args.checks or "").split(",") if c]
    missing = [c for c in checks if c not in state.checks]
    if missing:
        raise Failure(f"unknown check(s): {', '.join(missing)}")
    e = store.append("task_added", args.agent, title=redact(args.title), checks=checks)
    print(f"Task #{e['id']} added.")
    return 0


def cmd_task_done(store: Store, state: State, args: argparse.Namespace) -> int:
    t = state.tasks.get(args.id)
    if t is None:
        raise Failure(f"no task #{args.id} in this session")
    fps = Fingerprints(store)
    evidence = []
    for name in t["task"].get("checks") or []:
        if name not in state.checks:
            raise Refused(f"check `{name}` no longer exists; ask the user")
        status, run = state.check_status(name, fps)
        if status != "passed" or run is None:
            raise Refused(f"task #{args.id} not done: check `{name}` is {status} on the "
                          f"current code. Run `check run {name}` and fix the code, never "
                          f"the check.")
        evidence.append({"check": name, "run": run["id"], "fingerprint": run["fingerprint"]})
    store.append("task_done", args.agent, task=args.id, evidence=evidence,
                 note=redact(args.note or ""))
    print(f"Task #{args.id} done" + (" (verified by checks)." if evidence else
                                     " (no checks: recorded as an unverified claim)."))
    return 0


def cmd_task_reopen(store: Store, state: State, args: argparse.Namespace) -> int:
    if args.id not in state.tasks:
        raise Failure(f"no task #{args.id} in this session")
    store.append("task_reopened", args.agent, task=args.id, reason=redact(args.reason or ""))
    print(f"Task #{args.id} reopened.")
    return 0


def _unknown_text(state: State) -> str:
    return "; ".join(f"#{eff['begin']['id']} `{one_line(eff['begin']['what'], 80)}`"
                     for eff in state.unknown_effects())


def cmd_guard(store: Store, state: State, args: argparse.Namespace) -> int:
    if state.unknown_effects():
        raise Refused("unknown external effect(s): " + _unknown_text(state) +
                      ". Do not start external actions. Verify (`effect verify ID`) or ask "
                      "the user.")
    print("OK: no unknown external effects.")
    return 0


def cmd_effect_begin(store: Store, state: State, args: argparse.Namespace) -> int:
    if state.unknown_effects() and not args.user_override:
        raise Refused("refused: unknown external effect(s) " + _unknown_text(state) +
                      ". Resolve them first, or pass --user-override \"<the user's words>\" "
                      "only if the user explicitly told you to proceed.")
    if args.key:
        for eff in state.effects.values():
            b = eff["begin"]
            status = state.effect_status(eff)
            if b.get("key") == args.key and status.startswith("done"):
                raise Refused(f"refused: `{args.key}` was already done as effect #{b['id']} "
                              f"({b['ts']}). It must not be repeated.")
    data: Dict[str, Any] = {"what": redact(args.what)}
    if args.key:
        data["key"] = args.key
    if args.verify:
        data["verify"] = redact(args.verify)
    if args.expect:
        data["expect"] = args.expect
    if args.user_override:
        data["user_override"] = redact(args.user_override)
    e = store.append("effect_begin", args.agent, **data)
    print(f"Effect #{e['id']} begun. Run the action now, then record the outcome with "
          f"`effect end {e['id']} --outcome done|failed|unknown`.")
    return 0


def _effect(state: State, eid: int) -> Dict[str, Any]:
    eff = state.effects.get(eid)
    if eff is None:
        raise Failure(f"no effect #{eid}")
    return eff


def cmd_effect_end(store: Store, state: State, args: argparse.Namespace) -> int:
    eff = _effect(state, args.id)
    if eff["end"] is not None:
        raise Failure(f"effect #{args.id} already has an outcome; use `effect verify`")
    store.append("effect_end", args.agent, effect=args.id, outcome=args.outcome,
                 result=redact(args.result or ""))
    if args.outcome == "unknown":
        print(f"Effect #{args.id} is UNKNOWN. External actions are now blocked until it is "
              f"verified or the user decides.")
    else:
        print(f"Effect #{args.id}: {args.outcome}.")
    return 0


def cmd_effect_verify(store: Store, state: State, args: argparse.Namespace) -> int:
    eff = _effect(state, args.id)
    b = eff["begin"]
    cmd = args.cmd or b.get("verify")
    expect = args.expect if args.expect is not None else b.get("expect")
    if not cmd:
        raise Failure("no verify command; pass --cmd \"<read-only command>\"")
    try:
        proc = subprocess.run(cmd, shell=True, cwd=str(store.root), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=300)
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        code, output = 124, "[timed out]"
    output = redact(output)
    confirmed = code == 0 and (expect is None or expect in output)
    store.append("effect_verify", args.agent, effect=args.id, cmd=redact(cmd), exit=code,
                 expect=expect, confirmed=confirmed, tail=output[-OUTPUT_TAIL:])
    if confirmed:
        print(f"Effect #{args.id} confirmed DONE by evidence (exit 0"
              + (f", found {expect!r}" if expect else "") + "). Do not repeat it.")
        return 0
    print(f"Effect #{args.id} NOT confirmed (exit {code}). It stays unknown: a check that "
          f"finds nothing is not proof the action did not happen (results can lag). "
          f"Ask the user.")
    return EXIT_REFUSED


def cmd_effect_resolve(store: Store, state: State, args: argparse.Namespace) -> int:
    _effect(state, args.id)
    store.append("effect_resolve", args.agent, effect=args.id, done=args.done,
                 user_said=redact(args.user_said))
    print(f"Effect #{args.id} resolved as {'done' if args.done else 'not done'} "
          f"by the user's decision.")
    return 0


def cmd_capture(store: Store, state: State, args: argparse.Namespace) -> int:
    since = _ts_epoch(state.session["ts"]) - 3600 if state.session and not args.all else 0.0
    if args.transcript:
        sources = [(Path(p).expanduser(), "transcript") for p in args.transcript]
    else:
        sources = [(p, "claude-code") for p in _claude_transcripts(store.root)
                   if p.stat().st_mtime >= since]
        sources += [(p, "codex") for p in _codex_transcripts(store.root, since)]
    if not sources:
        print("No chat transcripts found for this project. Record the user's requirements "
              "with `note requirement \"<their exact words>\"` instead.")
        return 0
    added = 0
    for path, source in sources:
        if not path.is_file():
            raise Failure(f"no such transcript: {path}")
        for ident, at, text in user_messages(path):
            if ident in state.said_idents:
                continue
            store.append("user_said", args.agent, text=redact(text), ident=ident,
                         source=source, at=at, transcript=path.name)
            state.said_idents.add(ident)
            added += 1
    print(f"Captured {added} new user message(s) from {len(sources)} transcript(s).")
    return 0


def cmd_resume(store: Store, state: State, args: argparse.Namespace) -> int:
    sys.stdout.write(build_brief(store, state, args.budget, args.full))
    return 0


def cmd_handoff(store: Store, state: State, args: argparse.Namespace) -> int:
    with contextlib.suppress(Failure):
        cmd_capture(store, state, argparse.Namespace(transcript=None, all=False,
                                                     agent=args.agent))
    store.append("checkpoint", args.agent, summary=redact(args.summary))
    store.append("note", args.agent, note_kind="next_step", text=redact(args.next))
    unknown = State(store.events()).unknown_effects()
    print("Handoff recorded. The next model runs `resume` to continue.")
    if unknown:
        print("WARNING: there are unknown external effects; tell the user before stopping.")
    return 0


def cmd_search(store: Store, state: State, args: argparse.Namespace) -> int:
    terms = [t.lower() for t in args.words]
    hits = 0
    for e in state.events:
        blob = json.dumps(e, ensure_ascii=False).lower()
        log = store.dir / "outputs" / f"{e['id']}.log"
        if e["kind"] == "check_run" and log.exists():
            blob += log.read_text(encoding="utf-8", errors="replace").lower()
        if all(t in blob for t in terms):
            hits += 1
            text = e.get("text") or e.get("summary") or e.get("what") or e.get("goal") or \
                e.get("title") or e.get("cmd") or e.get("tail") or ""
            gone = " (retracted/replaced)" if e["id"] in state.hidden else ""
            print(f"#{e['id']} {e['ts']} {e['kind']}{gone}: {one_line(str(text), 240)}")
            if hits >= args.limit:
                break
    if not hits:
        print("No matches.")
    return 0


def cmd_show(store: Store, state: State, args: argparse.Namespace) -> int:
    e = state.by_id.get(args.id)
    if e is None:
        raise Failure(f"no event #{args.id}")
    print(json.dumps(e, ensure_ascii=False, indent=2))
    log = store.dir / "outputs" / f"{args.id}.log"
    if log.exists():
        print("\n--- full output ---\n" + log.read_text(encoding="utf-8", errors="replace"))
    return 0


def cmd_status(store: Store, state: State, args: argparse.Namespace) -> int:
    fps = Fingerprints(store)
    goal = state.session.get("goal") if state.session else None
    print(f"goal: {goal or '(no session)'}")
    open_tasks = sum(1 for t in state.tasks.values() if not t["done"])
    print(f"tasks: {len(state.tasks)} ({open_tasks} open)")
    for name in state.checks:
        print(f"check {name}: {state.check_status(name, fps)[0]}")
    unknown = state.unknown_effects()
    print(f"unknown external effects: {len(unknown)}" + (" (BLOCKED)" if unknown else ""))
    return 0


# --------------------------------------------------------------------------- CLI

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="continuity.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--repo", default=".", help="project folder (default: current folder)")
    p.add_argument("--agent", default=os.environ.get("CONTINUITY_AGENT"),
                   help="who is acting: your exact model id, or 'user' (env CONTINUITY_AGENT)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("start", help="start a session with the user's goal")
    s.add_argument("--goal", required=True)
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("note", help="record a requirement, decision, context, blocker or "
                                    "next step")
    s.add_argument("note_kind", choices=NOTE_KINDS)
    s.add_argument("text")
    s.add_argument("--reason", help="why (for decisions)")
    s.add_argument("--replaces", type=int, help="id of the note this one supersedes")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("retract", help="stop showing a note or captured message as current")
    s.add_argument("id", type=int)
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_retract)

    s = sub.add_parser("checkpoint", help="record what changed and the state it is in")
    s.add_argument("summary")
    s.set_defaults(fn=cmd_checkpoint)

    check = sub.add_parser("check", help="register and run checks").add_subparsers(
        dest="check_command", required=True)
    s = check.add_parser("add", help="register a check (only when the user asks)")
    s.add_argument("name")
    s.add_argument("cmd_args", nargs=argparse.REMAINDER, help="-- <shell command>")
    s.add_argument("--paths", nargs="*", help="files/folders the check depends on "
                                               "(default: the whole project)")
    s.add_argument("--by", default="user", help="who asked for this check (default: user)")
    s.set_defaults(fn=cmd_check_add)
    s = check.add_parser("remove", help="remove a check (only when the user asks)")
    s.add_argument("name")
    s.add_argument("--by", default="user")
    s.set_defaults(fn=cmd_check_remove)
    s = check.add_parser("run", help="run checks and bind each result to the current code")
    s.add_argument("names", nargs="*")
    s.add_argument("--timeout", type=int, default=CHECK_TIMEOUT)
    s.set_defaults(fn=cmd_check_run)

    task = sub.add_parser("task", help="tasks verified by checks").add_subparsers(
        dest="task_command", required=True)
    s = task.add_parser("add")
    s.add_argument("title")
    s.add_argument("--checks", help="comma-separated check names that prove it")
    s.set_defaults(fn=cmd_task_add)
    s = task.add_parser("done", help="refused unless every check passes on the current code")
    s.add_argument("id", type=int)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_task_done)
    s = task.add_parser("reopen")
    s.add_argument("id", type=int)
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_task_reopen)

    s = sub.add_parser("guard", help="exit 3 if any external effect is unknown")
    s.set_defaults(fn=cmd_guard)

    eff = sub.add_parser("effect", help="ledger of external effects").add_subparsers(
        dest="effect_command", required=True)
    s = eff.add_parser("begin", help="record intent BEFORE an external action")
    s.add_argument("what", help="the exact action, e.g. 'git push origin main'")
    s.add_argument("--key", help="business identity, e.g. 'release-v1.2.0'; a done key "
                                 "can never run again")
    s.add_argument("--verify", help="read-only command that succeeds only if it happened")
    s.add_argument("--expect", help="text the verify output must contain")
    s.add_argument("--user-override", help="the user's words allowing this despite unknown "
                                           "effects")
    s.set_defaults(fn=cmd_effect_begin)
    s = eff.add_parser("end", help="record the outcome AFTER the action")
    s.add_argument("id", type=int)
    s.add_argument("--outcome", choices=OUTCOMES, required=True)
    s.add_argument("--result")
    s.set_defaults(fn=cmd_effect_end)
    s = eff.add_parser("verify", help="confirm an effect by a read-only check")
    s.add_argument("id", type=int)
    s.add_argument("--cmd")
    s.add_argument("--expect")
    s.set_defaults(fn=cmd_effect_verify)
    s = eff.add_parser("resolve", help="the USER's decision on an unknown effect")
    s.add_argument("id", type=int)
    group = s.add_mutually_exclusive_group(required=True)
    group.add_argument("--done", dest="done", action="store_true")
    group.add_argument("--not-done", dest="done", action="store_false")
    s.add_argument("--user-said", required=True, help="the user's words deciding it")
    s.set_defaults(fn=cmd_effect_resolve)

    s = sub.add_parser("capture", help="save the user's messages from local chat transcripts")
    s.add_argument("--transcript", nargs="*", help="explicit JSONL transcript path(s)")
    s.add_argument("--all", action="store_true", help="all transcripts, not just this session")
    s.set_defaults(fn=cmd_capture)

    s = sub.add_parser("resume", help="print the brief for the next model")
    s.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="max characters")
    s.add_argument("--full", action="store_true", help="no size limit")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("handoff", help="capture chats, checkpoint and next step in one go")
    s.add_argument("--summary", required=True)
    s.add_argument("--next", required=True)
    s.set_defaults(fn=cmd_handoff)

    s = sub.add_parser("search", help="search the whole journal and check outputs")
    s.add_argument("words", nargs="+")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("show", help="show one event in full")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("status", help="one-screen summary")
    s.set_defaults(fn=cmd_status)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    root, is_git = find_root(Path(args.repo).expanduser())
    store = Store(root, is_git)
    try:
        return int(args.fn(store, State(store.events()), args))
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Failure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
