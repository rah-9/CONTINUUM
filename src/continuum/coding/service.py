# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Coding-session continuity service.

Rules (enforced here, not by convention):

* The journal is append-only; repository state is *observed* by Continuum.
* Agents may record goals, tasks, notes, checkpoints and run checks; they may
  **not** register or change checks. Checks are commands a human registered
  for the repository and they run without a shell, with a timeout.
* A task is ``done`` + ``verified`` only if every check it names passed on the
  repository's **current** fingerprint. A task without checks may be marked done,
  but it is recorded as an unverified claim. Later edits make verified tasks show
  ``needs_reverification`` in the brief.
* Free text is redacted and bounded before it is stored.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from continuum.coding.brief import SessionView, build_view, render_markdown
from continuum.coding.models import (
    _P,
    TYPE_OF,
    AgentJoined,
    ChatCaptured,
    CheckpointCaptured,
    CheckRan,
    NoteKind,
    NoteRecorded,
    RepoState,
    SessionClosed,
    SessionStarted,
    TaskAdded,
    TaskStatusChanged,
)
from continuum.coding.repo import (
    canonical_root,
    commits_since,
    current_branch,
    files_changed_since,
    observe,
    repo_key,
    resolve_repo,
    session_changes,
)
from continuum.coding.runner import run_check_process
from continuum.coding.transcript import Message
from continuum.core.canonical import digest, sha256_hex
from continuum.core.clock import Clock
from continuum.core.errors import (
    ContinuumError,
    InvalidTransitionError,
    NotFoundError,
    SpecValidationError,
)
from continuum.core.ids import IdGenerator
from continuum.core.redaction import Redactor
from continuum.core.store import Store


class StaleViewError(ContinuumError):
    code = "stale_view"


MAX_TEXT = 4000
MAX_OUTPUT_TAIL = 4000
MAX_CAPTURE = 16000  # a captured chat message (pasted logs beyond this are clipped)
MAX_SOURCE = 40
RECALL_SNIPPET = 600
_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


HUMAN_PREFIX = "human:"


def _clean(text: str) -> str:
    """PostgreSQL text/JSONB cannot hold U+0000 (binary output, pasted bytes)."""
    return text.replace("\x00", "\ufffd")


def _validated_tasks(tasks: list[Any] | None) -> list[tuple[str, list[str]]]:
    """Validate every initial task before anything is written."""
    out: list[tuple[str, list[str]]] = []
    for task in tasks or []:
        if not isinstance(task, dict) or set(task) - {"title", "checks"}:
            raise SpecValidationError("each task is {title: str, checks?: [str]}")
        title, checks = task.get("title"), task.get("checks", [])
        if not isinstance(title, str) or not title.strip():
            raise SpecValidationError("each task needs a non-empty title")
        if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
            raise SpecValidationError("task checks must be a list of check names")
        out.append((title, _check_names(checks)))
    return out


def _check_names(checks: list[str]) -> list[str]:
    for name in checks:
        if not _NAME.match(name):
            raise SpecValidationError(f"invalid check name {name!r}")
    return sorted(set(checks))


# Lookups by repository return only the caller's own sessions and checks (D-040). Row-level
# security enforces this for every role; the filter also scopes the schema owner, whom
# RLS does not restrict, so an operator's own coding never joins someone else's session.
_MINE = "owner_role = current_user"


class CodingContinuity:
    def __init__(self, store: Store, *, clock: Clock, ids: IdGenerator,
                 redactor: Redactor | None = None) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids
        self.redactor = redactor or Redactor.from_environment()

    # ===== journal =====================================================================

    def _actor(self, agent: str, *, human: bool = False) -> str:
        """Agent labels are free text from a model: redacted, bounded, never 'human:'."""
        label = " ".join(_clean(self.redactor.text(agent)).split())[:120] or "unknown-agent"
        if label.lower().startswith(HUMAN_PREFIX):
            raise SpecValidationError("agent labels may not start with 'human:'")
        return HUMAN_PREFIX + label if human else label

    def _text(self, value: str, field: str) -> str:
        value = _clean(self.redactor.text(value.strip()))
        if not value:
            raise SpecValidationError(f"{field} must not be empty")
        return value[:MAX_TEXT]

    def _append(self, session_id: str, payload: _P, actor: str, *,
                status: str | None = None, expect_seq: int | None = None,
                repo: tuple[str, str | None] | None = None) -> int:
        """One event, one transaction. ``expect_seq`` rejects a journal that moved on.
        ``repo`` (root, key) moves the session to another checkout in the same commit."""
        with self.store.connection() as conn:
            try:
                row = conn.execute(
                    b"SELECT last_seq, status FROM coding_sessions WHERE session_id = %s "
                    b"FOR UPDATE", (session_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"coding session {session_id} not found")
                if row["status"] == "closed":
                    raise InvalidTransitionError(f"coding session {session_id} is closed")
                if expect_seq is not None and row["last_seq"] != expect_seq:
                    raise StaleViewError(f"coding session {session_id} changed while this "
                                         "request was being evaluated; retry")
                seq = self._insert(conn, session_id, row["last_seq"] + 1, payload, actor)
                conn.execute(
                    b"UPDATE coding_sessions SET last_seq = %s, updated_at = %s, "
                    b"status = COALESCE(%s, status) WHERE session_id = %s",
                    (seq, self.clock.now(), status, session_id))
                if repo is not None:
                    conn.execute(b"UPDATE coding_sessions SET repo_root = %s, "
                                 b"repo_key = COALESCE(%s, repo_key) WHERE session_id = %s",
                                 (repo[0], repo[1], session_id))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return seq

    def _insert(self, conn: Any, session_id: str, seq: int, payload: _P, actor: str) -> int:
        conn.execute(
            b"INSERT INTO coding_events (session_id, seq, type, actor, payload_json, "
            b"occurred_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (session_id, seq, TYPE_OF[type(payload)].value, actor,
             Jsonb(payload.model_dump(mode="json")), self.clock.now()))
        return seq

    def _require_open(self, session: dict[str, Any]) -> None:
        if session["status"] == "closed":
            raise InvalidTransitionError(f"coding session {session['session_id']} is closed")

    def _events(self, session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self.store.read() as q:
            session = q._one("SELECT * FROM coding_sessions WHERE session_id = %s",
                             (session_id,))
            if session is None:
                raise NotFoundError(f"coding session {session_id} not found")
            events = q._all("SELECT * FROM coding_events WHERE session_id = %s ORDER BY seq",
                            (session_id,))
        return session, events

    def _view(self, session_id: str, current: RepoState | None = None) -> SessionView:
        session, events = self._events(session_id)
        checks = list(self._checks_for(session["repo_root"], session.get("repo_key")).values())
        root = resolve_repo(session["repo_root"])
        if current is None:
            current = observe(root)
        view = build_view(session, events, current, checks)
        view.now = self.clock.now()
        if current.vcs == "git":
            view.session_changes = session_changes(root, view.base_head)
        elif isinstance(session["created_at"], datetime):
            view.session_changes = files_changed_since(root, session["created_at"].timestamp())
        last = view.checkpoints[-1] if view.checkpoints else None
        if last and last["head"] and current.vcs == "git" and current.head:
            view.commits_since_checkpoint = (
                [] if last["head"] == current.head else commits_since(root, last["head"]))
        return view

    # ===== agent operations ===========================================================

    def start(self, repo_path: str, goal: str, *, agent: str,
              tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Session row, SessionStarted and initial tasks commit together or not at all."""
        planned = _validated_tasks(tasks)
        actor = self._actor(agent)
        clean_goal = self._text(goal, "goal")
        root = canonical_root(repo_path)
        state = observe(root)
        key = repo_key(root) if state.vcs == "git" else None
        events: list[_P] = [SessionStarted(repo_root=str(root), goal=clean_goal, agent=actor,
                                           state=state)]
        events += [TaskAdded(task_id=self.ids.new("task"), title=self._text(title, "title"),
                             checks=checks) for title, checks in planned]
        session_id = self.ids.new("cs")
        now = self.clock.now()
        with self.store.connection() as conn:
            try:
                conn.execute(
                    b"INSERT INTO coding_sessions (session_id, repo_root, repo_key, goal, "
                    b"status, last_seq, created_at, updated_at) "
                    b"VALUES (%s, %s, %s, %s, 'active', %s, %s, %s)",
                    (session_id, str(root), key, clean_goal, len(events), now, now))
                for seq, payload in enumerate(events, start=1):
                    self._insert(conn, session_id, seq, payload, actor)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return self.brief(session_id, current=state)

    def resume(self, *, agent: str, session_id: str | None = None,
               repo_path: str | None = None) -> dict[str, Any]:
        """Join a session as a (possibly different) model, chat or app and get the brief.

        From another checkout of the same repository (found by remote and branch), the
        session moves to this checkout: recorded in ``AgentJoined.repo_root``."""
        actor = self._actor(agent)
        if session_id is not None:
            session = self._events(session_id)[0]
            sid, root, moved = session_id, resolve_repo(session["repo_root"]), False
            if repo_path is not None and canonical_root(repo_path) != root:
                # explicit: continue this session in another checkout of the same repo
                here = canonical_root(repo_path)
                if session.get("repo_key") is None or repo_key(here) != session["repo_key"]:
                    raise SpecValidationError(
                        f"{here} is not a checkout of the repository of session "
                        f"{session_id} (different or missing remote)")
                root, moved = here, True
        else:
            sid, root, moved = self._locate(repo_path)
        state = observe(root)
        key = repo_key(root) if moved and state.vcs == "git" else None
        self._append(sid, AgentJoined(agent=actor, state=state,
                                      repo_root=str(root) if moved else None), actor,
                     repo=(str(root), key) if moved else None)
        return self.brief(sid, current=state)

    def latest_active(self, repo_path: str | None) -> str:
        return self._locate(repo_path)[0]

    def follow(self, repo_path: str, *, agent: str) -> str:
        """Locate the session and, if it was found in another checkout, move it here (an
        ``AgentJoined`` with the new root), so what follows observes *this* checkout.
        Used by hooks, which may run in a new checkout before any agent resumes."""
        sid, root, moved = self._locate(repo_path)
        if moved:
            actor = self._actor(agent)
            state = observe(root)
            self._append(sid, AgentJoined(agent=actor, state=state, repo_root=str(root)),
                         actor, repo=(str(root), repo_key(root)))
        return sid

    def _locate(self, repo_path: str | None) -> tuple[str, Path, bool]:
        """The active session for a repository: (session id, root to observe, moved?).

        1. By path: the git top level (so ``repo/src`` finds ``repo``'s session), or the
           exact path for sessions recorded before v0.8.
        2. Otherwise by identity: the same normalized remote **and** the same branch, in
           another checkout (another machine, a container, a re-clone). Never by a guess:
           if the branch differs, the candidates are named and nothing is joined."""
        if repo_path is None:
            raise SpecValidationError("give session_id or repo_path")
        given = resolve_repo(repo_path)
        root = canonical_root(repo_path)
        with self.store.read() as q:
            row = q._one("SELECT session_id FROM coding_sessions WHERE repo_root = ANY(%s) "
                         f"AND {_MINE} AND status = 'active' ORDER BY (repo_root = %s) DESC, "
                         "updated_at DESC LIMIT 1",
                         (list({str(root), str(given)}), str(root)))
        if row is not None:
            return str(row["session_id"]), root, False
        key = repo_key(root)
        if key is not None:
            with self.store.read() as q:
                rows = q._all("SELECT session_id, repo_root FROM coding_sessions WHERE "
                              f"repo_key = %s AND {_MINE} AND status = 'active' "
                              "ORDER BY updated_at DESC",
                              (key,))
            if rows:
                branch = current_branch(root)
                for r in rows:
                    if branch is not None and self._last_branch(r["session_id"]) == branch:
                        return str(r["session_id"]), root, True
                found = "; ".join(f"{r['session_id']} at {r['repo_root']} (branch "
                                  f"{self._last_branch(r['session_id'])})" for r in rows[:5])
                raise NotFoundError(
                    f"no active coding session for {root} on branch {branch}. Sessions for "
                    f"the same repository elsewhere: {found}. Check out that branch, or "
                    "pass session_id to continue one of them here, or call coding_start")
        raise NotFoundError(f"no active coding session for {root}; call coding_start")

    def _last_branch(self, session_id: str) -> str | None:
        """The branch last observed in a session (from its newest event with a state)."""
        with self.store.read() as q:
            row = q._one("SELECT payload_json->'state'->>'branch' AS branch FROM "
                         "coding_events WHERE session_id = %s AND payload_json ? 'state' "
                         "ORDER BY seq DESC LIMIT 1", (session_id,))
        return None if row is None else row["branch"]

    def _checks_for(self, root: str, key: str | None) -> dict[str, dict[str, Any]]:
        """Checks registered for this checkout; for names it lacks, checks a human
        registered for the same repository (same remote) in another checkout."""
        with self.store.read() as q:
            rows = q._all(f"SELECT * FROM coding_checks WHERE {_MINE} AND (repo_root = %s OR "
                          "(%s::text IS NOT NULL AND repo_key = %s)) "
                          "ORDER BY (repo_root = %s) DESC, registered_at DESC",
                          (root, key, key, root))
        found: dict[str, dict[str, Any]] = {}
        for r in rows:
            found.setdefault(r["name"], r)
        return dict(sorted(found.items()))

    def add_task(self, session_id: str, title: str, *, checks: list[str],
                 agent: str) -> dict[str, Any]:
        names = _check_names(checks)
        actor = self._actor(agent)
        task_id = self.ids.new("task")
        self._append(session_id, TaskAdded(task_id=task_id, title=self._text(title, "title"),
                                           checks=names), actor)
        return {"task_id": task_id, "checks": names}

    def set_task_status(self, session_id: str, task_id: str, status: str, *, agent: str,
                        note: str = "") -> dict[str, Any]:
        if status not in ("open", "in_progress", "blocked", "dropped"):
            raise SpecValidationError("use complete_task to mark a task done")
        actor = self._actor(agent)
        view = self._view(session_id)
        task = view.task(task_id)
        self._append(session_id, TaskStatusChanged(
            task_id=task.task_id, status=status,
            verified=False, fingerprint=view.current.fingerprint,
            note=_clean(self.redactor.text(note))[:MAX_TEXT]), actor)
        return {"task_id": task_id, "status": status}

    def complete_task(self, session_id: str, task_id: str, *, agent: str,
                      note: str = "") -> dict[str, Any]:
        """Mark done. Verified only with passing checks on the current code.

        The evidence is evaluated on a snapshot; the append is rejected if the journal
        moved on meanwhile (e.g. another agent recorded a failing run), and the code is
        re-observed right before committing so an edit in between is not certified."""
        actor = self._actor(agent)
        view = self._view(session_id)
        if view.status == "closed":
            raise InvalidTransitionError(f"coding session {session_id} is closed")
        task = view.task(task_id)
        missing = [c for c in task.checks if c not in view.registered_checks]
        if missing:
            return {"status": "refused", "task_id": task_id,
                    "reason": f"checks not registered for this repo: {missing}; ask a human "
                              "to run `continuum coding checks add`"}
        stale = []
        evidence: dict[str, Any] = {}
        for name in task.checks:
            run = view.latest_check(name)
            if run is None or not view.check_valid_now(run):
                stale.append(name)
            else:
                evidence[name] = {"seq": run["seq"], "output_sha256": run["output_sha256"],
                                  "argv_sha256": run["argv_sha256"]}
        if stale:
            return {"status": "refused", "task_id": task_id,
                    "reason": f"run these checks on the current code first: {stale}"}
        verified = bool(task.checks)
        if verified and observe(resolve_repo(view.repo_root)).fingerprint != \
                view.current.fingerprint:
            return {"status": "refused", "task_id": task_id,
                    "reason": "the code changed while completing; re-run the checks"}
        try:
            self._append(session_id, TaskStatusChanged(
                task_id=task_id, status="done", verified=verified,
                fingerprint=view.current.fingerprint, evidence=evidence,
                note=_clean(self.redactor.text(note))[:MAX_TEXT]), actor, expect_seq=view.last_seq)
        except StaleViewError:
            return {"status": "refused", "task_id": task_id,
                    "reason": "the session changed while completing (another agent wrote "
                              "to it); call coding_brief and retry"}
        return {"status": "done", "task_id": task_id, "verified": verified,
                "warning": None if verified else
                "no checks named for this task: recorded as an unverified claim"}

    def note(self, session_id: str, kind: NoteKind, text: str, *, agent: str) -> dict[str, Any]:
        seq = self._append(session_id, NoteRecorded(kind=kind, text=self._text(text, "text")),
                           self._actor(agent))
        return {"seq": seq, "kind": kind}

    def checkpoint(self, session_id: str, summary: str, *, agent: str) -> dict[str, Any]:
        actor = self._actor(agent)
        root = resolve_repo(self._events(session_id)[0]["repo_root"])
        state = observe(root)
        seq = self._append(session_id, CheckpointCaptured(
            summary=self._text(summary, "summary"), state=state), actor)
        return {"seq": seq, "fingerprint": state.fingerprint, "head": state.head,
                "changed_files": state.changed_files}

    def run_check(self, session_id: str, name: str, *, agent: str) -> dict[str, Any]:
        actor = self._actor(agent)
        session, _ = self._events(session_id)
        self._require_open(session)  # a closed session never runs a command
        root = resolve_repo(session["repo_root"])
        check = self._checks_for(str(root), session.get("repo_key")).get(name)
        if check is None:
            raise NotFoundError(f"check {name!r} is not registered for {root}; a human must "
                                "register it with `continuum coding checks add`")
        argv: list[str] = list(check["argv_json"])
        before = observe(root)
        started = time.monotonic()
        result = run_check_process(argv, root, check["timeout_seconds"])
        timed_out, exit_code = result.timed_out, result.exit_code
        duration_ms = int((time.monotonic() - started) * 1000)
        # binary output (NUL bytes) cannot be stored in PostgreSQL text: replace it
        text = _clean(self.redactor.text(result.output.decode("utf-8", errors="replace")))
        passed = exit_code == 0 and not timed_out
        modified = observe(root).fingerprint != before.fingerprint
        payload = CheckRan(name=name, argv_sha256=digest(argv), exit_code=exit_code,
                           passed=passed, timed_out=timed_out, duration_ms=duration_ms,
                           output_tail=text[-MAX_OUTPUT_TAIL:],
                           output_sha256="sha256:" + sha256_hex(text),
                           fingerprint=before.fingerprint, modified_tree=modified)
        seq = self._append(session_id, payload, actor)
        return {"seq": seq, "name": name, "passed": passed, "exit_code": exit_code,
                "timed_out": timed_out, "duration_ms": duration_ms, "modified_tree": modified,
                "output_tail": payload.output_tail[-1500:]}

    def close(self, session_id: str, reason: str, *, agent: str,
              human: bool = False) -> dict[str, Any]:
        """``human=True`` only from the human CLI; agents can never record 'human:'."""
        self._append(session_id, SessionClosed(reason=self._text(reason, "reason")),
                     self._actor(agent, human=human), status="closed")
        return {"session_id": session_id, "status": "closed"}

    # ===== chat capture (hooks, transcript import; never an MCP tool) =================

    def capture(self, repo_path: str, messages: list[Message], *, source: str,
                chat_id: str | None = None, autostart: bool = True,
                history: bool = False) -> dict[str, Any]:
        """Record chat messages verbatim (redacted, bounded) in the repository's session.

        Called by an app's hooks or a transcript import, not by a model: user messages
        are the user's own words. Idempotent: a message already captured for the same
        source, chat, role and text is skipped, so a whole transcript can be re-imported
        after every turn. With no active session and ``autostart``, the first user
        message starts one, so the work is journaled even if no agent ever called
        ``coding_start`` (inside a git repository only: a hook installed for every project
        must not journal arbitrary directories). Only the newest agent message is kept
        (the turn's report).

        The same words can legitimately recur ("yes", "continue"), so a message's key also
        counts its occurrence: ``history=True`` means ``messages`` is a chat transcript in
        order (occurrences are counted within it, so re-importing it is idempotent);
        otherwise the messages are new turns (counted after what is already stored)."""
        label = " ".join(_clean(self.redactor.text(source)).split())[:MAX_SOURCE] or "cli"
        chat = None if chat_id is None else _clean(self.redactor.text(chat_id))[:120]
        prepared: list[ChatCaptured] = []
        idents: list[str | None] = []
        # A transcript is kept whole: every user message, every agent text message and
        # every tool call as a one-line action (tool output is not kept). New turns
        # without a transcript keep the turn's final agent message.
        last_agent = max((i for i, m in enumerate(messages) if m.role == "agent"), default=-1)
        for i, msg in enumerate(messages):
            if msg.role == "agent" and not history and i != last_agent:
                continue
            if msg.role == "action" and not history:   # tool calls come from transcripts
                continue
            text = _clean(self.redactor.text(msg.text.strip()))
            if not text:
                continue
            key = "sha256:" + hashlib.sha256(
                "\0".join((label, chat or "", msg.role, text)).encode()).hexdigest()
            prepared.append(ChatCaptured(role=msg.role, text=text[:MAX_CAPTURE], source=label,
                                         chat_id=chat, key=key,
                                         chars_omitted=max(0, len(text) - MAX_CAPTURE),
                                         sent_at=_timestamp(msg.at),
                                         failed=msg.role == "action" and msg.failed))
            idents.append(msg.ident if history else None)
        if not prepared:
            return {"captured": 0, "skipped": 0, "session_id": None}
        try:
            sid = self.follow(repo_path, agent=f"capture:{label}")
        except NotFoundError:
            first = next((m.text for m in prepared if m.role == "user"), None)
            if not autostart or first is None or current_branch(canonical_root(repo_path)) \
                    is None:
                return {"captured": 0, "skipped": len(prepared), "session_id": None,
                        "reason": "no active coding session for this repository"}
            goal = " ".join(first.split())[:300]
            sid = self.start(repo_path, goal, agent=f"capture:{label}")["session_id"]
        return self._append_captured(sid, prepared, idents, f"capture:{label}",
                                     history=history)

    def _append_captured(self, session_id: str, items: list[ChatCaptured],
                         idents: list[str | None], actor: str, *,
                         history: bool) -> dict[str, Any]:
        """All new messages in one transaction under the session lock (gapless seq).

        Keys (``payload.key``): ``<text key>`` and ``<text key>#n`` for messages without a
        transcript id (the n-th occurrence of the same words in that chat), and
        ``<text key>@<id hash>`` for transcript messages with an id. A transcript message
        whose words were already captured live (by a prompt or notify hook, without an id)
        is stored as an ``alias`` of that message: never shown twice, and remembered so a
        re-import stays idempotent."""
        with self.store.connection() as conn:
            try:
                row = conn.execute(
                    b"SELECT last_seq, status FROM coding_sessions WHERE session_id = %s "
                    b"FOR UPDATE", (session_id,)).fetchone()
                if row is None or row["status"] == "closed":
                    conn.rollback()
                    return {"captured": 0, "skipped": len(items), "session_id": session_id,
                            "reason": "session not active"}
                bases = list({i.key for i in items})
                stored = [r["k"] for r in conn.execute(
                    b"SELECT payload_json->>'key' AS k FROM coding_events WHERE "
                    b"session_id = %s AND type = 'ChatCaptured' AND "
                    b"split_part(split_part(payload_json->>'key', '#', 1), '@', 1) = ANY(%s)",
                    (session_id, bases)).fetchall()]
                seen = set(stored)
                live: dict[str, int] = {}      # captured without an id, per text key
                aliases: dict[str, int] = {}   # transcript copies of live messages
                for k in stored:
                    if "@" in k:
                        if k.endswith("!"):
                            base = k.split("@", 1)[0]
                            aliases[base] = aliases.get(base, 0) + 1
                    else:
                        base = k.split("#", 1)[0]
                        live[base] = live.get(base, 0) + 1
                count: dict[str, int] = {} if history else dict(live)
                seq = row["last_seq"]
                fresh = []
                for item, ident in zip(items, idents, strict=True):
                    base = item.key
                    if ident is not None:
                        key = f"{base}@{hashlib.sha256(ident.encode()).hexdigest()[:24]}"
                        if key in seen or key + "!" in seen:
                            continue
                        # an unmatched live copy of these words exists: this is it
                        alias = aliases.get(base, 0) < live.get(base, 0)
                        if alias:
                            aliases[base] = aliases.get(base, 0) + 1
                            key += "!"
                        item = item.model_copy(update={"key": key, "alias": alias})
                    else:
                        n = count.get(base, 0) + 1
                        count[base] = n
                        key = base if n == 1 else f"{base}#{n}"
                        if key in seen:
                            continue
                        item = item.model_copy(update={"key": key})
                    seen.add(key)
                    seq = self._insert(conn, session_id, seq + 1, item, actor)
                    if not item.alias:
                        fresh.append(item)
                if seq != row["last_seq"]:
                    conn.execute(b"UPDATE coding_sessions SET last_seq = %s, updated_at = %s "
                                 b"WHERE session_id = %s", (seq, self.clock.now(), session_id))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {"captured": len(fresh), "skipped": len(items) - len(fresh),
                "session_id": session_id}

    # ===== read-only ==================================================================

    def recall(self, query: str, *, session_id: str | None = None,
               repo_path: str | None = None, all_sessions: bool = False,
               limit: int = 8) -> dict[str, Any]:
        """Search everything the journal remembers, deterministically (no LLM, no index
        to build): goals, tasks, notes, checkpoints, captured chat, check names and
        output. The brief is bounded; nothing is forgotten, it is found here.

        Ranking: number of distinct query words found, then newest first."""
        words = [w for w in re.findall(r"[\w./:-]+", query.lower()) if len(w) > 1][:12]
        if not words:
            raise SpecValidationError("query needs at least one word")
        if session_id is not None:
            ids = [session_id]
            root = self._events(session_id)[0]["repo_root"]
        else:
            sid = None
            try:
                sid, located, _ = self._locate(repo_path)
                root = str(located)
            except NotFoundError:
                if not all_sessions:
                    raise
                root = str(canonical_root(repo_path or "."))
            ids = [sid] if sid else []
        if all_sessions:
            with self.store.read() as q:
                ids = [r["session_id"] for r in q._all(
                    f"SELECT session_id FROM coding_sessions WHERE repo_root = %s AND {_MINE} "
                    "ORDER BY updated_at DESC LIMIT 50", (root,))] or ids
        hits: list[tuple[int, str, dict[str, Any]]] = []
        for sid in ids:
            _, events = self._events(sid)
            for e in events:
                text = _searchable(e)
                if not text:
                    continue
                low = text.lower()
                score = sum(1 for w in words if w in low)
                if score:
                    hits.append((score, _iso(e["occurred_at"]), {
                        "session_id": sid, "seq": e["seq"], "type": e["type"],
                        "kind": e["payload_json"].get("kind") or e["payload_json"].get("role"),
                        "by": e["actor"], "at": _iso(e["occurred_at"]),
                        "matched": [w for w in words if w in low],
                        "text": _snippet(text, low, words)}))
        hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
        limit = max(1, min(limit, 30))
        return {"query": query, "sessions_searched": len(ids), "total_matches": len(hits),
                "matches": [h[2] for h in hits[:limit]],
                "trust": "user-role chat is the user's words; notes and agent chat are "
                         "claims; check output is observed"}


    def brief(self, session_id: str, *, current: RepoState | None = None) -> dict[str, Any]:
        view = self._view(session_id, current)
        body = view.to_dict()
        body["markdown"] = render_markdown(view, body)
        return body

    def sessions(self, repo_path: str | None = None) -> list[dict[str, Any]]:
        with self.store.read() as q:
            if repo_path:
                rows = q._all("SELECT * FROM coding_sessions WHERE repo_root = ANY(%s) "
                              f"AND {_MINE} ORDER BY updated_at DESC",
                              (list({str(canonical_root(repo_path)),
                                     str(resolve_repo(repo_path))}),))
            else:
                rows = q._all(f"SELECT * FROM coding_sessions WHERE {_MINE} ORDER BY updated_at "
                              "DESC LIMIT 50", ())
        return [{"session_id": r["session_id"], "repo_root": r["repo_root"], "goal": r["goal"],
                 "status": r["status"], "updated_at": _iso(r["updated_at"])} for r in rows]

    def checks(self, repo_path: str) -> list[dict[str, Any]]:
        root = canonical_root(repo_path)
        rows = self._checks_for(str(root), repo_key(root)).values()
        return [{"name": r["name"], "argv": r["argv_json"],
                 "timeout_seconds": r["timeout_seconds"],
                 "registered_by": r["registered_by"],
                 "registered_for": r["repo_root"]} for r in rows]

    # ===== human-only: erasure and retention (never MCP) ===============================

    def forget(self, session_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        """Erase a coding session completely: every journal event (captured chat, notes,
        check output) and the session row, in one transaction. A content-free tombstone
        records who erased which session, when and why (D-039).

        Human-only (CLI); the journal is otherwise append-only, enforced by a trigger that
        admits deletes only inside a transaction that set ``continuum.erasure``."""
        if not actor.strip() or not reason.strip():
            raise SpecValidationError("erasure needs a human actor id and a reason")
        who = HUMAN_PREFIX + " ".join(actor.split())[:120]
        why = _clean(self.redactor.text(" ".join(reason.split())))[:MAX_TEXT]
        with self.store.connection() as conn:
            try:
                row = conn.execute(
                    b"SELECT repo_root, owner_role FROM coding_sessions WHERE session_id = %s "
                    b"FOR UPDATE",
                    (session_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"coding session {session_id} not found")
                conn.execute(b"SELECT set_config('continuum.erasure', 'on', true)")
                erased = conn.execute(b"DELETE FROM coding_events WHERE session_id = %s",
                                      (session_id,)).rowcount
                conn.execute(b"DELETE FROM coding_sessions WHERE session_id = %s",
                             (session_id,))
                erasure_id = self.ids.new("erase")
                conn.execute(
                    b"INSERT INTO coding_erasures (erasure_id, session_id, repo_digest, "
                    b"events_erased, erased_by, reason, erased_at, owner_role) "
                    b"VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (erasure_id, session_id, "sha256:" + sha256_hex(row["repo_root"]),
                     erased, who, why, self.clock.now(), row["owner_role"]))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {"erasure_id": erasure_id, "session_id": session_id, "events_erased": erased}

    def forget_older_than(self, days: int, *, actor: str, reason: str,
                          include_active: bool = False) -> dict[str, Any]:
        """Retention: erase sessions whose last write is older than ``days``. Closed
        sessions only, unless ``include_active``. Each is erased like ``forget``."""
        if days < 1:
            raise SpecValidationError("retention must be at least 1 day")
        cutoff = self.clock.now() - timedelta(days=days)
        statuses = ["closed", "active"] if include_active else ["closed"]
        with self.store.read() as q:
            rows = q._all("SELECT session_id FROM coding_sessions WHERE updated_at < %s "
                          "AND status = ANY(%s) ORDER BY updated_at", (cutoff, statuses))
        done = [self.forget(r["session_id"], actor=actor, reason=reason) for r in rows]
        return {"sessions_erased": len(done),
                "events_erased": sum(d["events_erased"] for d in done),
                "erasures": [d["erasure_id"] for d in done]}

    def erasures(self, limit: int = 50) -> list[dict[str, Any]]:
        """The tombstones: what was erased, by whom, when and why (no content)."""
        with self.store.read() as q:
            rows = q._all("SELECT * FROM coding_erasures ORDER BY erased_at DESC LIMIT %s",
                          (max(1, min(limit, 500)),))
        return [{**r, "erased_at": _iso(r["erased_at"])} for r in rows]

    # ===== human-only =================================================================

    def register_check(self, repo_path: str, name: str, argv: list[str], *, actor: str,
                       timeout_seconds: int = 600) -> dict[str, Any]:
        """Human operators only (CLI). Never exposed to agents over MCP."""
        if not _NAME.match(name):
            raise SpecValidationError(f"invalid check name {name!r}")
        if not argv or not all(isinstance(a, str) and a and "\x00" not in a for a in argv):
            raise SpecValidationError("argv must be a non-empty list of strings")
        if not actor.strip():
            raise SpecValidationError("a human actor id is required")
        if not 1 <= timeout_seconds <= 3600:
            raise SpecValidationError("timeout_seconds must be between 1 and 3600")
        root = canonical_root(repo_path)
        with self.store.connection() as conn:
            conn.execute(
                b"INSERT INTO coding_checks (repo_root, repo_key, name, argv_json, "
                b"timeout_seconds, registered_by, registered_at) "
                b"VALUES (%s, %s, %s, %s, %s, %s, %s) "
                b"ON CONFLICT (owner_role, repo_root, name) DO UPDATE SET "
                b"argv_json = EXCLUDED.argv_json, "
                b"repo_key = EXCLUDED.repo_key, "
                b"timeout_seconds = EXCLUDED.timeout_seconds, "
                b"registered_by = EXCLUDED.registered_by, "
                b"registered_at = EXCLUDED.registered_at",
                (str(root), repo_key(root), name, Jsonb(argv), timeout_seconds,
                 HUMAN_PREFIX + " ".join(actor.split())[:120],
                 self.clock.now()))
            conn.commit()
        return {"repo_root": str(root), "name": name, "argv": argv}


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _timestamp(value: str | None) -> str | None:
    """A transcript timestamp, normalized to UTC ISO-8601; None if absent or unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def _searchable(event: dict[str, Any]) -> str:
    p = event["payload_json"]
    match event["type"]:
        case "SessionStarted":
            return str(p.get("goal", ""))
        case "TaskAdded":
            return str(p.get("title", ""))
        case "TaskStatusChanged":
            return f"{p.get('status', '')}: {p.get('note', '')}" if p.get("note") else ""
        case "NoteRecorded":
            return str(p.get("text", ""))
        case "ChatCaptured":
            return "" if p.get("alias") else str(p.get("text", ""))
        case "CheckpointCaptured":
            return str(p.get("summary", ""))
        case "CheckRan":
            return f"check {p.get('name')} {'passed' if p.get('passed') else 'failed'}\n" \
                   f"{p.get('output_tail', '')}"
        case "SessionClosed":
            return str(p.get("reason", ""))
    return ""


def _snippet(text: str, low: str, words: list[str]) -> str:
    """The text around the first match, bounded."""
    if len(text) <= RECALL_SNIPPET:
        return text
    first = min(i for i in (low.find(w) for w in words) if i >= 0)
    start = max(0, first - RECALL_SNIPPET // 3)
    piece = text[start:start + RECALL_SNIPPET]
    return ("…" if start else "") + piece + ("…" if start + RECALL_SNIPPET < len(text) else "")

