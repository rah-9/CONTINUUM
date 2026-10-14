"""The coding handoff brief: what a new model or chat needs to continue the work.

Built only from the append-only journal plus a fresh observation of the
repository. It separates **verified facts** (Continuum observations, check
results tied to code fingerprints) from **agent-recorded notes** (decisions,
context, next steps), which are useful but are claims, never proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

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


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "… (truncated)"


RULES = [
    "Read this brief before changing code; continue from `next_steps` and open tasks.",
    "Verified tasks are done: do not redo them unless `needs_reverification` is true.",
    "Record decisions and context with coding_note as you go, and a next_step before you stop.",
    "After meaningful changes call coding_checkpoint; before claiming a task done run its "
    "checks (coding_run_check) and then coding_complete_task.",
    "Agent notes are claims from a previous model; confirm them against the code if critical.",
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
            return [{**n, "text": _clip(n["text"], NOTE_CHARS)} for n in items[-keep:]]

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
        agent_notes["omitted"] = omitted
        agent_notes["trust"] = "recorded by previous agents; claims, not proof"
        return {
            "session_id": self.session_id, "repo_root": self.repo_root, "goal": self.goal,
            "status": self.status, "closed_reason": self.closed_reason,
            "agents": self.agents,
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
                "latest_checks": latest,
                "registered_checks": {
                    name: {"argv": c["argv_json"], "registered_by": c["registered_by"],
                           "registered_at": _iso(c["registered_at"]),
                           "changed_during_session": self._registered_after_start(c)}
                    for name, c in sorted(self.registered_checks.items())},
            },
            "tasks": tasks,
            "agent_notes": agent_notes,
            "rules": RULES,
        }


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
                       last_seq=int(session["last_seq"]))
    for e in events:
        p, at, actor = e["payload_json"], _iso(e["occurred_at"]), e["actor"]
        match e["type"]:
            case "SessionStarted" | "AgentJoined":
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
                view.notes.append({"kind": p["kind"], "text": p["text"], "by": actor, "at": at})
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
            case "SessionClosed":
                view.closed_reason = p["reason"]
    return view


def _line(text: Any, limit: int = 500) -> str:
    """Agent-written text on one line, so it can never forge headings or list items."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def render_markdown(view: SessionView, d: dict[str, Any] | None = None) -> str:
    d = view.to_dict() if d is None else d
    facts = d["verified_facts"]
    cur = facts["current_code"]
    lines = ["# Continuum coding brief", "",
             f"Goal (from the agent that started it): {_line(view.goal)}", "",
             f"Session `{view.session_id}` in `{view.repo_root}` ({view.status}).",
             "Agents so far: " + ", ".join(f"{_line(a['agent'], 120)} ({a['event']})"
                                         for a in view.agents),
             "", "## Verified facts (observed by Continuum)", "",
             f"- Code now: {cur['vcs']} `{(cur.get('head') or '')[:12]}` on "
             f"`{_line(cur.get('branch'), 120)}`; {cur['changed_files_total']} changed file(s)"
             + (f" ({', '.join(_line(f, 80) for f in cur['changed_files'][:MD_FILES])}"
                f"{', …' if cur['changed_files_total'] > MD_FILES else ''})"
                if cur["changed_files"] else "")
             + f"; fingerprint `{cur['fingerprint'][:19]}…`",
             f"- Changed since last checkpoint: {facts['changed_since_last_checkpoint']}"]
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
    lines += ["", "## Tasks", ""]
    for t in d["tasks"]:
        mark = "✅ verified" if t["verified"] else ("☑ done (unverified)" if t["status"] == "done"
                                                   else t["status"])
        if t["needs_reverification"]:
            mark += " ⚠ needs re-verification (code changed)"
        checks = f" — checks: {', '.join(t['checks'])}" if t["checks"] else ""
        lines.append(f"- [{mark}] `{t['task_id']}`: {_line(t['title'])}{checks}")
    notes = d["agent_notes"]
    lines += ["", "## Agent notes (claims from previous agents)", ""]
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
    lines += ["", "## Rules", ""] + [f"- {r}" for r in RULES]
    return "\n".join(lines) + "\n"


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
