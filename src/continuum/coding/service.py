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

import re
import subprocess
import time
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from continuum.coding.brief import SessionView, build_view, render_markdown
from continuum.coding.models import (
    _P,
    TYPE_OF,
    AgentJoined,
    CheckpointCaptured,
    CheckRan,
    NoteKind,
    NoteRecorded,
    SessionClosed,
    SessionStarted,
    TaskAdded,
    TaskStatusChanged,
)
from continuum.coding.repo import observe, resolve_repo
from continuum.core.canonical import digest, sha256_hex
from continuum.core.clock import Clock
from continuum.core.errors import InvalidTransitionError, NotFoundError, SpecValidationError
from continuum.core.ids import IdGenerator
from continuum.core.redaction import Redactor
from continuum.core.store import Store

MAX_TEXT = 4000
MAX_OUTPUT_TAIL = 4000
_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _clean_agent(agent: str) -> str:
    agent = " ".join(agent.split())[:120]
    return agent or "unknown-agent"


class CodingContinuity:
    def __init__(self, store: Store, *, clock: Clock, ids: IdGenerator,
                 redactor: Redactor | None = None) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids
        self.redactor = redactor or Redactor.from_environment()

    # ===== journal =====================================================================

    def _text(self, value: str, field: str) -> str:
        value = self.redactor.text(value.strip())
        if not value:
            raise SpecValidationError(f"{field} must not be empty")
        return value[:MAX_TEXT]

    def _append(self, session_id: str, payload: _P, actor: str, *,
                status: str | None = None) -> int:
        etype = TYPE_OF[type(payload)]
        now = self.clock.now()
        with self.store.connection() as conn:
            try:
                row = conn.execute(
                    b"SELECT last_seq, status FROM coding_sessions WHERE session_id = %s "
                    b"FOR UPDATE", (session_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"coding session {session_id} not found")
                if row["status"] == "closed":
                    raise InvalidTransitionError(f"coding session {session_id} is closed")
                seq = row["last_seq"] + 1
                conn.execute(
                    b"INSERT INTO coding_events (session_id, seq, type, actor, payload_json, "
                    b"occurred_at) VALUES (%s, %s, %s, %s, %s, %s)",
                    (session_id, seq, etype.value, actor, Jsonb(payload.model_dump(mode="json")),
                     now))
                conn.execute(
                    b"UPDATE coding_sessions SET last_seq = %s, updated_at = %s, "
                    b"status = COALESCE(%s, status) WHERE session_id = %s",
                    (seq, now, status, session_id))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return int(seq)

    def _events(self, session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self.store.read() as q:
            session = q._one("SELECT * FROM coding_sessions WHERE session_id = %s",
                             (session_id,))
            if session is None:
                raise NotFoundError(f"coding session {session_id} not found")
            events = q._all("SELECT * FROM coding_events WHERE session_id = %s ORDER BY seq",
                            (session_id,))
        return session, events

    def _view(self, session_id: str) -> SessionView:
        session, events = self._events(session_id)
        with self.store.read() as q:
            checks = q._all("SELECT name, argv_json, timeout_seconds, registered_by, "
                            "registered_at FROM coding_checks WHERE repo_root = %s "
                            "ORDER BY name", (session["repo_root"],))
        current = observe(resolve_repo(session["repo_root"]))
        return build_view(session, events, current, checks)

    # ===== agent operations ===========================================================

    def start(self, repo_path: str, goal: str, *, agent: str,
              tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        root = resolve_repo(repo_path)
        state = observe(root)
        session_id = self.ids.new("cs")
        now = self.clock.now()
        with self.store.connection() as conn:
            conn.execute(
                b"INSERT INTO coding_sessions (session_id, repo_root, goal, status, last_seq, "
                b"created_at, updated_at) VALUES (%s, %s, %s, 'active', 0, %s, %s)",
                (session_id, str(root), self._text(goal, "goal"), now, now))
            conn.commit()
        self._append(session_id, SessionStarted(repo_root=str(root), goal=self._text(goal, "goal"),
                                                agent=_clean_agent(agent), state=state),
                     _clean_agent(agent))
        for task in tasks or []:
            self.add_task(session_id, task["title"], checks=list(task.get("checks", [])),
                          agent=agent)
        return self.brief(session_id)

    def resume(self, *, agent: str, session_id: str | None = None,
               repo_path: str | None = None) -> dict[str, Any]:
        """Join a session as a (possibly different) model or chat and get the brief."""
        sid = session_id or self.latest_active(repo_path)
        state = observe(resolve_repo(self._events(sid)[0]["repo_root"]))
        self._append(sid, AgentJoined(agent=_clean_agent(agent), state=state),
                     _clean_agent(agent))
        return self.brief(sid)

    def latest_active(self, repo_path: str | None) -> str:
        if repo_path is None:
            raise SpecValidationError("give session_id or repo_path")
        root = str(resolve_repo(repo_path))
        with self.store.read() as q:
            row = q._one("SELECT session_id FROM coding_sessions WHERE repo_root = %s AND "
                         "status = 'active' ORDER BY updated_at DESC LIMIT 1", (root,))
        if row is None:
            raise NotFoundError(f"no active coding session for {root}; call coding_start")
        return str(row["session_id"])

    def add_task(self, session_id: str, title: str, *, checks: list[str],
                 agent: str) -> dict[str, Any]:
        for name in checks:
            if not _NAME.match(name):
                raise SpecValidationError(f"invalid check name {name!r}")
        task_id = self.ids.new("task")
        self._append(session_id, TaskAdded(task_id=task_id, title=self._text(title, "title"),
                                           checks=sorted(set(checks))), _clean_agent(agent))
        return {"task_id": task_id, "checks": sorted(set(checks))}

    def set_task_status(self, session_id: str, task_id: str, status: str, *, agent: str,
                        note: str = "") -> dict[str, Any]:
        if status not in ("open", "in_progress", "blocked", "dropped"):
            raise SpecValidationError("use complete_task to mark a task done")
        view = self._view(session_id)
        task = view.task(task_id)
        self._append(session_id, TaskStatusChanged(
            task_id=task.task_id, status=status,
            verified=False, fingerprint=view.current.fingerprint,
            note=self.redactor.text(note)[:MAX_TEXT]), _clean_agent(agent))
        return {"task_id": task_id, "status": status}

    def complete_task(self, session_id: str, task_id: str, *, agent: str,
                      note: str = "") -> dict[str, Any]:
        """Mark done. Verified only with passing checks on the current code."""
        view = self._view(session_id)
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
        self._append(session_id, TaskStatusChanged(
            task_id=task_id, status="done", verified=verified,
            fingerprint=view.current.fingerprint, evidence=evidence,
            note=self.redactor.text(note)[:MAX_TEXT]), _clean_agent(agent))
        return {"status": "done", "task_id": task_id, "verified": verified,
                "warning": None if verified else
                "no checks named for this task: recorded as an unverified claim"}

    def note(self, session_id: str, kind: NoteKind, text: str, *, agent: str) -> dict[str, Any]:
        seq = self._append(session_id, NoteRecorded(kind=kind, text=self._text(text, "text")),
                           _clean_agent(agent))
        return {"seq": seq, "kind": kind}

    def checkpoint(self, session_id: str, summary: str, *, agent: str) -> dict[str, Any]:
        root = resolve_repo(self._events(session_id)[0]["repo_root"])
        state = observe(root)
        seq = self._append(session_id, CheckpointCaptured(
            summary=self._text(summary, "summary"), state=state), _clean_agent(agent))
        return {"seq": seq, "fingerprint": state.fingerprint, "head": state.head,
                "changed_files": state.changed_files}

    def run_check(self, session_id: str, name: str, *, agent: str) -> dict[str, Any]:
        session, _ = self._events(session_id)
        root = resolve_repo(session["repo_root"])
        with self.store.read() as q:
            check = q._one("SELECT argv_json, timeout_seconds FROM coding_checks "
                           "WHERE repo_root = %s AND name = %s", (str(root), name))
        if check is None:
            raise NotFoundError(f"check {name!r} is not registered for {root}; a human must "
                                "register it with `continuum coding checks add`")
        argv: list[str] = list(check["argv_json"])
        before = observe(root)
        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(argv, cwd=root, capture_output=True,  # noqa: S603 - human-registered argv, no shell
                                  timeout=check["timeout_seconds"], check=False)
            output, exit_code = proc.stdout + proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            output = (exc.stdout or b"") + (exc.stderr or b"")
            exit_code = None
        except FileNotFoundError as exc:
            output, exit_code = str(exc).encode(), 127
        duration_ms = int((time.monotonic() - started) * 1000)
        text = self.redactor.text(output.decode("utf-8", errors="replace"))
        passed = exit_code == 0 and not timed_out
        after = observe(root)
        if after.fingerprint != before.fingerprint:
            text += "\n[continuum] note: this check modified the working tree."
        payload = CheckRan(name=name, argv_sha256=digest(argv), exit_code=exit_code,
                           passed=passed, timed_out=timed_out, duration_ms=duration_ms,
                           output_tail=text[-MAX_OUTPUT_TAIL:],
                           output_sha256="sha256:" + sha256_hex(text),
                           fingerprint=before.fingerprint)
        seq = self._append(session_id, payload, _clean_agent(agent))
        return {"seq": seq, "name": name, "passed": passed, "exit_code": exit_code,
                "timed_out": timed_out, "duration_ms": duration_ms,
                "output_tail": payload.output_tail[-1500:]}

    def close(self, session_id: str, reason: str, *, agent: str) -> dict[str, Any]:
        self._append(session_id, SessionClosed(reason=self._text(reason, "reason")),
                     _clean_agent(agent), status="closed")
        return {"session_id": session_id, "status": "closed"}

    # ===== read-only ==================================================================

    def brief(self, session_id: str) -> dict[str, Any]:
        view = self._view(session_id)
        body = view.to_dict()
        body["markdown"] = render_markdown(view)
        return body

    def sessions(self, repo_path: str | None = None) -> list[dict[str, Any]]:
        with self.store.read() as q:
            if repo_path:
                rows = q._all("SELECT * FROM coding_sessions WHERE repo_root = %s "
                              "ORDER BY updated_at DESC", (str(resolve_repo(repo_path)),))
            else:
                rows = q._all("SELECT * FROM coding_sessions ORDER BY updated_at DESC "
                              "LIMIT 50", ())
        return [{"session_id": r["session_id"], "repo_root": r["repo_root"], "goal": r["goal"],
                 "status": r["status"], "updated_at": _iso(r["updated_at"])} for r in rows]

    def checks(self, repo_path: str) -> list[dict[str, Any]]:
        with self.store.read() as q:
            rows = q._all("SELECT * FROM coding_checks WHERE repo_root = %s ORDER BY name",
                          (str(resolve_repo(repo_path)),))
        return [{"name": r["name"], "argv": r["argv_json"],
                 "timeout_seconds": r["timeout_seconds"],
                 "registered_by": r["registered_by"]} for r in rows]

    # ===== human-only =================================================================

    def register_check(self, repo_path: str, name: str, argv: list[str], *, actor: str,
                       timeout_seconds: int = 600) -> dict[str, Any]:
        """Human operators only (CLI). Never exposed to agents over MCP."""
        if not _NAME.match(name):
            raise SpecValidationError(f"invalid check name {name!r}")
        if not argv or not all(isinstance(a, str) and a for a in argv):
            raise SpecValidationError("argv must be a non-empty list of strings")
        if not actor:
            raise SpecValidationError("a human actor id is required")
        root = resolve_repo(repo_path)
        with self.store.connection() as conn:
            conn.execute(
                b"INSERT INTO coding_checks (repo_root, name, argv_json, timeout_seconds, "
                b"registered_by, registered_at) VALUES (%s, %s, %s, %s, %s, %s) "
                b"ON CONFLICT (repo_root, name) DO UPDATE SET argv_json = EXCLUDED.argv_json, "
                b"timeout_seconds = EXCLUDED.timeout_seconds, "
                b"registered_by = EXCLUDED.registered_by, "
                b"registered_at = EXCLUDED.registered_at",
                (str(root), name, Jsonb(argv), timeout_seconds, f"human:{actor}",
                 self.clock.now()))
            conn.commit()
        return {"repo_root": str(root), "name": name, "argv": argv}


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)

