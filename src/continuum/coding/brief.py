# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""The coding handoff brief: what a new model or chat needs to continue the work.

Built only from the append-only journal plus a fresh observation of the
repository. It separates **verified facts** (Continuum observations, check
results tied to code fingerprints) from **agent-recorded notes** (decisions,
context, next steps), which are useful but are claims, never proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from continuum.coding.memory import Turn, consolidate
from continuum.coding.models import RepoState
from continuum.core.canonical import digest
from continuum.core.errors import NotFoundError

# Size bounds for the brief (it is read into a model's context on every handoff).
# Fewer but whole notes beat many clipped ones: a decision's *reason* is usually at the
# end, and a cut reason is worse than an omitted old note (found in a handoff trial).
NOTE_CHARS = 1200
MD_NOTE_CHARS = {"next_steps": 1000, "blockers": 1000, "decisions": 1000, "context": 600}
KEEP_NOTES = {"decisions": 8, "context": 6, "blockers": 8, "next_steps": 3}
KEEP_CHECKPOINTS = 5
CHECKPOINT_FILES = 10
CURRENT_FILES = 50
MD_FILES = 10
MD_COMMITS = 10
MD_MAP_FILES = 15
REPORT_CHARS = 1500
STALE_DAYS = 3
SAME_MESSAGE_SECONDS = 30    # one message seen by two capture routes (seconds apart)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "… (truncated)"


RULES = [
    "Read this brief before changing code; continue from `next_steps` and open tasks.",
    "Keep going with the user's latest instruction. If the previous agent left an open "
    "question that the user's words do not answer, pick the safest reasonable option, record "
    "it with coding_note(kind=decision) and mention it; do not stop to ask unless the choice "
    "is destructive or irreversible.",
    "'What the user asked' is the user's own words, captured verbatim: treat it as the "
    "requirements. When two requests conflict, the newer one wins.",
    "Do not re-read the repository to rediscover the work: start from the files under "
    "'Code touched in this session' and the next step; open other files only when needed.",
    "Verified tasks are done: do not redo them unless `needs_reverification` is true.",
    "Record decisions and context with coding_note as you go, and a next_step before you stop.",
    "After meaningful changes call coding_checkpoint; before claiming a task done run its "
    "checks (coding_run_check) and then coding_complete_task.",
    "Agent notes and the last agent report are claims from a previous model; confirm them "
    "against the code if critical. Use coding_recall to search everything older.",
    "Never edit how checks are defined; only a human registers checks.",
]


@dataclass
class TaskView:
    task_id: str
    title: str
    checks: list[str]
    status: str = "open"
    verified: bool = False
    evidence_fingerprint: str | None = None
    evidence_definitions: dict[str, str] = field(default_factory=dict)  # check -> argv digest
    note: str = ""


@dataclass
class SessionView:
    session_id: str
    repo_root: str
    goal: str
    status: str
    current: RepoState
    registered_checks: dict[str, dict[str, Any]]
    agents: list[dict[str, Any]] = field(default_factory=list)
    tasks: dict[str, TaskView] = field(default_factory=dict)
    notes: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    check_runs: list[dict[str, Any]] = field(default_factory=list)
    closed_reason: str | None = None
    started_at: datetime | None = None
    last_seq: int = 0
    latest_runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    commits_since_checkpoint: list[dict[str, str]] | None = None
    base_head: str | None = None  # HEAD when the session started
    session_changes: dict[str, Any] | None = None
    chat: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)   # D-042 activity events
    moves: list[dict[str, Any]] = field(default_factory=list)  # checkout changes
    start_branch: str | None = None
    updated_at: datetime | None = None   # the session's last write
    now: datetime | None = None          # when this view was built

    def task(self, task_id: str) -> TaskView:
        if task_id not in self.tasks:
            raise NotFoundError(f"task {task_id} not in session {self.session_id}")
        return self.tasks[task_id]

    def latest_check(self, name: str) -> dict[str, Any] | None:
        return self.latest_runs.get(name)

    def definition_digest(self, name: str) -> str | None:
        check = self.registered_checks.get(name)
        return digest(list(check["argv_json"])) if check else None

    def check_valid_now(self, run: dict[str, Any] | None) -> bool:
        """Passed, on the current code, with the check's current definition."""
        return bool(run and run["passed"] and run["fingerprint"] == self.current.fingerprint
                    and run["argv_sha256"] == self.definition_digest(run["name"]))

    def needs_reverification(self, task: TaskView) -> bool:
        if not task.verified:
            return False
        if task.evidence_fingerprint != self.current.fingerprint:
            return True
        return any(self.definition_digest(name) != used
                   for name, used in task.evidence_definitions.items())

    def to_dict(self) -> dict[str, Any]:
        last_cp = self.checkpoints[-1] if self.checkpoints else None
        tasks = []
        for t in self.tasks.values():
            tasks.append({
                "task_id": t.task_id, "title": t.title, "status": t.status,
                "verified": t.verified, "checks": t.checks,
                "needs_reverification": self.needs_reverification(t),
                "note": t.note})
        latest = {}
        for name, run in sorted(self.latest_runs.items()):
            latest[name] = {"passed": run["passed"], "at": run["at"],
                            "modified_tree": run.get("modified_tree", False),
                            "on_current_code": run["fingerprint"] == self.current.fingerprint,
                            "definition_unchanged":
                                run["argv_sha256"] == self.definition_digest(name),
                            "valid_now": self.check_valid_now(run),
                            "output_tail": run["output_tail"][-600:]}

        # The brief goes into the next model's context: keep it bounded however long the
        # session runs. Newest items win; every omission is counted, never silent.
        omitted: dict[str, int] = {}

        def notes(kind: str, key: str) -> list[dict[str, Any]]:
            items = [n for n in self.notes if n["kind"] == kind]
            keep = KEEP_NOTES[key]
            if len(items) > keep:
                omitted[key] = len(items) - keep
            limit = min(NOTE_CHARS, MD_NOTE_CHARS[key])
            return [{"kind": n["kind"], "text": _clip(n["text"], limit), "by": n["by"],
                     "at": n["at"]} for n in items[-keep:]]

        checkpoints = [{**cp, "summary": _clip(cp["summary"], NOTE_CHARS),
                        "changed_files": cp["changed_files"][:CHECKPOINT_FILES]}
                       for cp in self.checkpoints[-KEEP_CHECKPOINTS:]]
        if len(self.checkpoints) > KEEP_CHECKPOINTS:
            omitted["checkpoints"] = len(self.checkpoints) - KEEP_CHECKPOINTS
        current = self.current.model_dump()
        current["changed_files_total"] = len(current["changed_files"])
        current["changed_files"] = current["changed_files"][:CURRENT_FILES]
        agent_notes: dict[str, Any] = {
            "decisions": notes("decision", "decisions"),
            "context": notes("context", "context"),
            "blockers": notes("blocker", "blockers"),
            "next_steps": notes("next_step", "next_steps"),
            "checkpoints": checkpoints,
        }
        agent_notes["last_report"] = self._last_report()
        memory = self._user_requests()
        agent_notes["reported_decisions"] = memory.pop("agent_decisions")
        agent_notes["omitted"] = omitted
        agent_notes["trust"] = "recorded by previous agents; claims, not proof"
        return {
            "session_id": self.session_id, "repo_root": self.repo_root, "goal": self.goal,
            "status": self.status, "closed_reason": self.closed_reason,
            "agents": self.agents if len(self.agents) <= 10 else
            self.agents[:1] + self.agents[-9:],
            "agents_total": len(self.agents),
            "verified_facts": {
                "current_code": current,
                "changed_since_last_checkpoint": bool(
                    last_cp and last_cp["fingerprint"] != self.current.fingerprint),
                # observed state only; the agent's summary is a claim (agent_notes)
                "last_checkpoint": None if last_cp is None else {
                    "at": last_cp["at"], "fingerprint": last_cp["fingerprint"],
                    "head": last_cp["head"],
                    "changed_files": last_cp["changed_files"][:CHECKPOINT_FILES]},
                # observed from git; None when unknown (no checkpoint, history rewritten)
                "commits_since_last_checkpoint": self.commits_since_checkpoint,
                # where the session's code is: read these instead of exploring the repo
                "session_changes": self.session_changes,
                "latest_checks": latest,
                "registered_checks": {
                    name: {"argv": c["argv_json"], "registered_by": c["registered_by"],
                           "registered_at": _iso(c["registered_at"]),
                           "changed_during_session": self._registered_after_start(c)}
                    for name, c in sorted(self.registered_checks.items())},
            },
            "user_requests": memory,
            "work_log": self.work_log(),
            "attention": self.attention(memory),
            "tasks": tasks,
            "agent_notes": agent_notes,
            "rules": RULES,
        }

    def _user_requests(self) -> dict[str, Any]:
        """The conversation's memory for the next model (``memory.consolidate``)."""
        turns: list[Turn] = []
        routes: dict[str, list[tuple[tuple[str, str | None], datetime]]] = {}
        for c in self.chat:
            if c["role"] not in ("user", "agent"):
                continue
            if c["role"] == "user":
                # One message captured by two routes (a hook and an import) has the same
                # words at the same moment: keep the first. The same words typed later, in
                # another app ("yes" to a new question), are a new message.
                route, when = (c["source"], c["chat"]), c["when"]
                earlier = routes.setdefault(c["text"], [])
                if any(r != route and abs((when - w).total_seconds()) <= SAME_MESSAGE_SECONDS
                       for r, w in earlier):
                    continue
                earlier.append((route, when))
            turns.append(Turn(c["seq"], c["role"], c["text"], c["at"], c["source"], c["chat"]))
        body = consolidate(turns)
        body["trust"] = ("the user's own words, captured by the user's app (hook or "
                         "transcript import): requirements; a newer message wins")
        return body

    def _relative(self, path: str) -> str:
        root = self.repo_root.rstrip("/\\")
        for prefix in (root + "/", root + "\\"):
            if path.startswith(prefix):
                return path[len(prefix):]
        return path

    def work_log(self) -> dict[str, Any]:
        """What the previous agents did (D-042): their tool calls as the user's app recorded
        them, with results, and the file changes Continuum observed. Recorded and observed,
        not claimed; the checks and the code map still say what the code is now."""
        from continuum.coding import worklog

        return worklog.build(self.tools + self._legacy_actions(), self._relative)

    def _legacy_actions(self) -> list[dict[str, Any]]:
        """Sessions recorded by v1.0.0rc1 kept tool calls as one-line chat 'actions'
        (D-041): shown through the same summary."""
        out: list[dict[str, Any]] = []
        for c in self.chat:
            if c["role"] != "action":
                continue
            verb, _, what = c["text"].partition(": ")
            key = f"legacy:{c['seq']}"
            tool, kind, effect, data = {
                "run": ("Bash", "shell", "write", {"command": what}),
                "edit": ("Edit", "builtin", "write", {"file_path": what}),
                "read": ("Read", "builtin", "read", {"file_path": what}),
            }.get(verb, (verb or "tool", "builtin", "external", {"name": what}))
            out.append({"seq": c["seq"], "type": "ToolCallStarted", "at": c["at"], "p": {
                "call_key": key, "tool": tool, "kind": kind, "effect": effect, "input": data,
                "source": c["source"], "skill": None}})
            out.append({"seq": c["seq"], "type": "ToolCallFinished", "at": c["at"], "p": {
                "call_key": key, "tool": tool, "status": "error" if c.get("failed") else "ok",
                "exit_code": None, "output_excerpt": "", "output_blob": None}})
            if verb == "edit" and not _absolute(self._relative(what)):
                out.append({"seq": c["seq"], "type": "FileImpactObserved", "at": c["at"],
                            "p": {"call_key": key, "files": [{
                                "path": self._relative(what), "status": "modified",
                                "added": 0, "deleted": 0}]}})
        return out

    def _last_report(self) -> dict[str, Any] | None:
        reports = [c for c in self.chat if c["role"] == "agent"]
        if not reports:
            return None
        r = reports[-1]
        return {"text": _clip(r["text"], REPORT_CHARS), "at": r["at"], "source": r["source"]}

    def attention(self, memory: dict[str, Any] | None = None) -> list[str]:
        """Gaps the next model must know about, computed from the journal (no model)."""
        memory = memory if memory is not None else self._user_requests()
        out: list[str] = []
        users = [c for c in self.chat if c["role"] == "user"]
        steps = [n for n in self.notes if n["kind"] == "next_step"]
        if not self.chat:
            out.append("No chat was captured for this session: requirements the user stated "
                       "only in an earlier chat are not here. If something seems missing, ask "
                       "the user (a human can enable capture: `continuum coding setup`).")
        elif users and memory["latest"] and memory["latest"].get("ends_chat"):
            out.append(f"The user's most recent message (#{memory['latest']['n']}) ended the "
                       "previous chat: its \"stop\" was addressed to the previous agent, not "
                       "to you. The work it leaves for later (and every requirement below) is "
                       "yours to continue now.")
        elif users and (not steps or users[-1]["seq"] > steps[-1]["seq"]):
            out.append("The user's most recent message is newer than the last recorded next "
                       "step: it says what to do now.")
        work = self.work_log()
        external = [u for u in work["unresolved"] if u["effect"] == "external"]
        local = [u for u in work["unresolved"] if u["effect"] != "external"]
        for u in external[:3]:
            out.append(f"⛔ Outcome UNKNOWN: `{_line(u['tool'], 80)}` "
                       f"{_line(u['target'], 160)} [#{u['seq']}] could have changed something "
                       "outside this repository and its result was never recorded. New "
                       "external actions are blocked until a person checks and records what "
                       f"happened: `continuum coding resolve {self.session_id} {u['short']} "
                       "--outcome applied|not_applied|accepted_unknown --actor NAME "
                       "--reason TEXT`. Local work may continue; do not repeat the call.")
        if local:
            shown = "; ".join(f"`{_line(u['tool'], 60)}` {_line(u['target'], 100)} "
                              f"[#{u['seq']}]" for u in local[:3])
            out.append(f"The result of {len(local)} local call(s) was never recorded: "
                       f"{shown}. Compare the files under 'Files changed' and the code map "
                       "with what they were meant to do before continuing.")
        if work["failures"]:
            shown = "; ".join(f"`{_line(f['target'] or f['tool'], 120)}` [#{f['seq']}]"
                              for f in work["failures"])
            out.append(f"Recent call(s) failed and were not re-run successfully: {shown}. "
                       "Check whether each failure is still open before building on that "
                       "work (the output is under 'Recent failures').")
        last_cp = self.checkpoints[-1] if self.checkpoints else None
        mapped = bool(self.session_changes and self.session_changes.get("files"))
        if mapped and (last_cp is None or last_cp["fingerprint"] != self.current.fingerprint):
            out.append("Some code changes are not described by any checkpoint: read the files "
                       "under 'Code touched in this session' before continuing.")
        stale = [t.task_id for t in self.tasks.values() if self.needs_reverification(t)]
        if stale:
            out.append(f"{len(stale)} verified task(s) need re-verification (the code or a "
                       f"check changed after they passed): {', '.join(stale[:5])}.")
        failing = [n for n, r in self.latest_runs.items()
                   if not r["passed"] and r["fingerprint"] == self.current.fingerprint]
        if failing:
            out.append(f"Failing on the current code: {', '.join(sorted(failing))}.")
        if self.now and self.updated_at and \
                (self.now - self.updated_at).total_seconds() > STALE_DAYS * 86400:
            days = int((self.now - self.updated_at).total_seconds() // 86400)
            out.append(f"Nothing was recorded for {days} days. Confirm with the user that the "
                       "latest request is still what they want before continuing.")
        if self.moves:
            m = self.moves[-1]
            out.append(f"This session moved to another checkout ({m['to']}) at {m['at']}; "
                       "facts above are observed here.")
        if self.start_branch and self.current.branch and \
                self.current.branch != self.start_branch:
            out.append(f"The branch is now `{self.current.branch}`; the session started on "
                       f"`{self.start_branch}`.")
        return out


    def _registered_after_start(self, check: dict[str, Any]) -> bool:
        at = check["registered_at"]
        if not isinstance(at, datetime) or self.started_at is None:
            return False
        return at > self.started_at


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def build_view(session: dict[str, Any], events: list[dict[str, Any]], current: RepoState,
               checks: list[dict[str, Any]]) -> SessionView:
    view = SessionView(session_id=session["session_id"], repo_root=session["repo_root"],
                       goal=session["goal"], status=session["status"], current=current,
                       registered_checks={c["name"]: c for c in checks},
                       started_at=session["created_at"]
                       if isinstance(session["created_at"], datetime) else None,
                       last_seq=int(session["last_seq"]),
                       updated_at=session.get("updated_at")
                       if isinstance(session.get("updated_at"), datetime) else None)
    alias_times: dict[str, list[Any]] = {}
    for e in events:
        p, at, actor = e["payload_json"], _iso(e["occurred_at"]), e["actor"]
        match e["type"]:
            case "SessionStarted" | "AgentJoined":
                if e["type"] == "SessionStarted":
                    view.base_head = p["state"].get("head")
                    view.start_branch = p["state"].get("branch")
                elif p.get("repo_root"):
                    view.moves.append({"to": p["repo_root"], "at": at})
                view.agents.append({"agent": p["agent"], "at": at,
                                    "event": "started" if e["type"] == "SessionStarted"
                                    else "joined"})
            case "TaskAdded":
                view.tasks[p["task_id"]] = TaskView(p["task_id"], p["title"], p["checks"])
            case "TaskStatusChanged":
                t = view.tasks[p["task_id"]]
                t.status, t.verified, t.note = p["status"], p["verified"], p["note"]
                t.evidence_fingerprint = p["fingerprint"] if p["verified"] else None
                t.evidence_definitions = {name: ev.get("argv_sha256", "")
                                          for name, ev in p.get("evidence", {}).items()}
            case "NoteRecorded":
                view.notes.append({"kind": p["kind"], "text": p["text"], "by": actor, "at": at,
                                   "seq": e["seq"]})
            case "CheckpointCaptured":
                state = p["state"]
                view.checkpoints.append({"summary": p["summary"], "by": actor, "at": at,
                                         "fingerprint": state["fingerprint"],
                                         "head": state.get("head"),
                                         "changed_files": state.get("changed_files", [])[:50]})
            case "CheckRan":
                run = {**p, "seq": e["seq"], "at": at, "by": actor}
                view.check_runs.append(run)
                view.latest_runs[p["name"]] = run
            case "ChatCaptured" if p.get("alias"):
                # the transcript's copy of a message captured live: only its time is new
                base = str(p.get("key", "")).split("@", 1)[0]
                alias_times.setdefault(base, []).append(p.get("sent_at"))
            case "ChatCaptured":
                view.chat.append({"role": p["role"], "text": p["text"], "source": p["source"],
                                  "chat": p.get("chat_id"),
                                  "chars_omitted": p.get("chars_omitted", 0), "at": at,
                                  "seq": e["seq"], "key": str(p.get("key", "")),
                                  "sent_at": p.get("sent_at"),
                                  "failed": bool(p.get("failed", False)),
                                  "occurred_at": e["occurred_at"]})
            case ("ToolCallStarted" | "ToolCallFinished" | "ToolCallUnresolved"
                  | "ToolCallResolved" | "FileImpactObserved" | "CompactionObserved"
                  | "SubagentReported"):
                view.tools.append({"seq": e["seq"], "type": e["type"], "p": p, "at": at})
            case "SessionClosed":
                view.closed_reason = p["reason"]
    # Messages recovered from a transcript after a missed hook are stored late. Order by
    # the transcript's own timestamps: a live message takes the time of its transcript copy
    # (matched by words and occurrence), so one clock orders the whole chat.
    for c in view.chat:
        base, _, n = c["key"].partition("#")
        times = alias_times.get(base, [])
        index = int(n) - 1 if n.isdigit() else 0
        sent = c["sent_at"] or (times[index] if index < len(times) else None)
        c["when"] = _when(sent, c.pop("occurred_at"))
    view.chat.sort(key=lambda c: (c["when"], c["seq"]))
    return view


def _when(sent_at: Any, occurred_at: Any) -> datetime:
    if isinstance(sent_at, str):
        try:
            return datetime.fromisoformat(sent_at)
        except ValueError:
            pass
    if isinstance(occurred_at, datetime):
        return occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=UTC)
    return datetime.min.replace(tzinfo=UTC)


def _agents_line(agents: list[dict[str, Any]], keep: int = 8) -> str:
    """Who worked on the session, bounded: a long session has hundreds of joins."""
    shown = agents if len(agents) <= keep else agents[:1] + agents[-(keep - 1):]
    text = ", ".join(f"{_line(a['agent'], 120)} ({a['event']})" for a in shown)
    return text + (f" (+{len(agents) - len(shown)} more joins)" if len(agents) > keep else "")


def _words(text: Any, limit: int) -> str:
    """One line, cut at a word boundary."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0]
    return cut + " …"


def _line(text: Any, limit: int = 500) -> str:
    """Agent-written text on one line, so it can never forge headings or list items."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def render_markdown(view: SessionView, d: dict[str, Any] | None = None) -> str:
    d = view.to_dict() if d is None else d
    facts = d["verified_facts"]
    cur = facts["current_code"]
    by_capture = bool(view.agents) and str(view.agents[0]["agent"]).startswith("capture:")
    goal = (f"Goal (the user's first message; full text below): {_words(view.goal, 160)}"
            if by_capture else f"Goal (from the agent that started it): {_line(view.goal)}")
    lines = ["# Continuum coding brief", "", goal, "",
             f"Session `{view.session_id}` in `{view.repo_root}` ({view.status}).",
             "Agents so far: " + _agents_line(view.agents)]
    if d["attention"]:
        lines += ["", "## Attention", ""] + [f"- ⚠ {_line(a, 400)}" for a in d["attention"]]
    lines += _render_memory(d["user_requests"])
    lines += ["", "## Verified facts (observed by Continuum)", "",
             f"- Code now: {cur['vcs']} `{(cur.get('head') or '')[:12]}` on "
             f"`{_line(cur.get('branch'), 120)}`; {cur['changed_files_total']} changed file(s)"
             + (f" ({', '.join(_line(f, 80) for f in cur['changed_files'][:MD_FILES])}"
                f"{', …' if cur['changed_files_total'] > MD_FILES else ''})"
                if cur["changed_files"] else "")
             + f"; fingerprint `{cur['fingerprint'][:19]}…`",
             "- Changed since last checkpoint: " + (
                 str(facts["changed_since_last_checkpoint"]) if facts["last_checkpoint"]
                 else "no checkpoint recorded yet")]
    commits = facts["commits_since_last_checkpoint"]
    if commits:
        lines.append(f"- {len(commits)} commit(s) since the last checkpoint: " + "; ".join(
            f"`{c['sha']}` {_line(c['subject'], 100)}" for c in commits[:MD_COMMITS]))
    for name, c in facts["latest_checks"].items():
        state = "passed" if c["passed"] else "FAILED"
        where = "on current code" if c["on_current_code"] else "on OLDER code (re-run)"
        if not c["definition_unchanged"]:
            where += "; check definition CHANGED since (re-run)"
        if c["modified_tree"]:
            where += "; ⚠ the check modified the working tree"
        lines.append(f"- Check `{name}`: {state} {where}")
    for name, c in facts["registered_checks"].items():
        if c["changed_during_session"]:
            lines.append(f"- ⚠ Check `{name}` was (re)registered during this session by "
                         f"{_line(c['registered_by'], 120)} at {c['registered_at']}")
    changes = facts["session_changes"]
    if changes and changes["files"]:
        lines += ["", f"### Code touched in this session ({changes['total_files']} file(s); "
                      "read these first instead of exploring the repository)", ""]
        for f in changes["files"][:MD_MAP_FILES]:
            detail = {"untracked": f"new, {f['added']} lines",
                      "changed": f"changed, {f['added']} lines"}.get(
                f["status"], f"+{f['added']}/-{f['deleted']}")
            names = ", ".join(_line(n, 60) for n in f["defines"])
            near = "; ".join(_line(n, 60) for n in f["near"][:3])
            lines.append(f"- `{_line(f['path'], 120)}` ({detail})"
                         + (f" — defines: {names}" if names else "")
                         + (f" — near: {near}" if near else ""))
        if changes["total_files"] > MD_MAP_FILES:
            lines.append(f"- … {changes['total_files'] - MD_MAP_FILES} more "
                         "(`detail=\"full\"`)")
    from continuum.coding import worklog

    lines += worklog.render(d["work_log"], view.session_id, _line)
    lines += ["", "## Tasks", ""]
    if not d["tasks"]:
        lines.append("_No tasks recorded. Plan them with coding_add_task._")
    for t in d["tasks"]:
        mark = "✅ verified" if t["verified"] else ("☑ done (unverified)" if t["status"] == "done"
                                                   else t["status"])
        if t["needs_reverification"]:
            mark += " ⚠ needs re-verification (code changed)"
        checks = f" — checks: {', '.join(t['checks'])}" if t["checks"] else ""
        lines.append(f"- [{mark}] `{t['task_id']}`: {_line(t['title'])}{checks}")
    notes = d["agent_notes"]
    lines += ["", "## Agent notes (claims from previous agents)", ""]
    if not any(notes[k] for k in ("next_steps", "blockers", "decisions", "context")):
        lines.append("_No notes recorded by previous agents._")
    for label, key in (("Next steps", "next_steps"), ("Blockers", "blockers"),
                       ("Decisions", "decisions"), ("Context", "context")):
        if notes[key]:
            lines.append(f"**{label}**")
            lines += [f"- {_line(n['text'], MD_NOTE_CHARS[key])}  _(by {_line(n['by'], 120)})_"
                      for n in notes[key]]
            if notes["omitted"].get(key):
                lines.append(f"- _({notes['omitted'][key]} older {label.lower()} notes "
                             "omitted; `coding_brief(detail=\"full\")` shows the bounded "
                             "JSON, the journal keeps all of them)_")
    if notes["checkpoints"]:
        cp = notes["checkpoints"][-1]
        lines += ["", f"Last checkpoint: {_line(cp['summary'], MD_NOTE_CHARS['context'])} "
                      f"(by {_line(cp['by'], 120)})"]
    if notes["reported_decisions"]:
        lines += ["", "**Choices and caveats the previous agent reported** (claims):"]
        lines += [f"- {_line(x['text'], 300)}" for x in notes["reported_decisions"]]
    if notes["last_report"]:
        r = notes["last_report"]
        lines += ["", f"Last message from the previous agent ({_line(r['source'], 40)}, "
                      f"{r['at']}; a claim): {_line(r['text'], REPORT_CHARS)}"]
    lines += ["", "## Rules", ""] + [f"- {r}" for r in RULES]
    return "\n".join(lines) + "\n"


def _absolute(path: str) -> bool:
    return path.startswith(("/", "~")) or (len(path) > 2 and path[1] == ":")


def _maybe(item: dict[str, Any]) -> str:
    n = item.get("maybe_replaced_by")
    return f" _(#{n} may replace this; check)_" if n else ""


def _render_memory(m: dict[str, Any]) -> list[str]:
    """The user's side of the conversation: numbered, in order, newest wins."""
    if not m["messages"]:
        return []
    chats = ", ".join(f"{_line(c['source'], 40)} ({c['messages']})" for c in m["chats"])
    lines = ["", f"## What the user asked ({m['messages']} message(s); chats: {chats})", "",
             "_The user's own words, numbered in order. They are the requirements; when two "
             "conflict, the higher number wins._", ""]
    first, latest = m["first"], m["latest"]
    if first:
        if first.get("replaced_by"):
            lines.append(f"- ~~**#{first['n']} (first request)**~~ (partly replaced by "
                         f"#{first['replaced_by']}): {_line(first['text'], 800)}")
        else:
            lines.append(f"- **#{first['n']} (first request):** {_line(first['text'], 800)}"
                         + _maybe(first)
                         + (" _(clipped; coding_recall has it all)_" if first["clipped"]
                            else ""))
    entries: list[tuple[int, int, str]] = []
    for item in m["ledger"]:
        if item.get("replaced_by"):
            entries.append((item["n"], item["pos"], f"- ~~#{item['n']}~~ (replaced by "
                                                     f"#{item['replaced_by']}): "
                                                     f"~~{_line(item['text'], 160)}~~"))
            continue
        tag = {"revision": " (changes an earlier decision)", "answer": " (answer)"}.get(
            item["kind"], "")
        entries.append((item["n"], item["pos"], f"- #{item['n']}{tag}: "
                                                 f"{_line(item['text'], 400)}{_maybe(item)}"))
    for r in m["recent"]:
        if r.get("replaced_by"):
            entries.append((r["n"], 0, f"- #{r['n']} (partly replaced by "
                                       f"#{r['replaced_by']}): {_line(r['text'], 600)}"))
        else:
            entries.append((r["n"], 0, f"- #{r['n']}: {_line(r['text'], 600)}{_maybe(r)}"))
    lines += [text for _, _, text in sorted(entries)]   # conversation order
    if latest:
        lines.append(f"- **#{latest['n']} (the most recent message):** "
                     f"{_line(latest['text'], 2100)}")
        if latest.get("in_reply_to"):
            asked = _line(latest["in_reply_to"], 400)
            lines.append(f"  - in reply to the agent asking: \"{asked}\"")
    left = m["omitted_messages"]
    if left or m["omitted_items"]:
        lines.append(f"- _{left} other message(s) carried no requirement (small talk, steps "
                     "already done, pasted output)"
                     + (f"; {m['omitted_items']} older requirement(s) beyond the budget"
                        if m["omitted_items"] else "")
                     + ". `coding_recall` searches all of them._")
    return lines


def compact(body: dict[str, Any]) -> dict[str, Any]:
    """What an agent needs to continue, once: the Markdown brief plus the ids it must
    pass to other tools. The full JSON stays available (``detail="full"``)."""
    return {
        "session_id": body["session_id"], "status": body["status"], "goal": body["goal"],
        "tasks": [{k: t[k] for k in ("task_id", "title", "status", "verified",
                                     "needs_reverification", "checks")}
                  for t in body["tasks"]],
        "markdown": body["markdown"],
        "detail": "compact; call coding_brief(session_id, detail=\"full\") for all fields",
    }
