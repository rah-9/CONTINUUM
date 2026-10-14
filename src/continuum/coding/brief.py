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
    started_at: str = ""

    def task(self, task_id: str) -> TaskView:
        if task_id not in self.tasks:
            raise NotFoundError(f"task {task_id} not in session {self.session_id}")
        return self.tasks[task_id]

    def latest_check(self, name: str) -> dict[str, Any] | None:
        runs = [r for r in self.check_runs if r["name"] == name]
        return runs[-1] if runs else None

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
        for name in sorted({r["name"] for r in self.check_runs}):
            run = self.latest_check(name)
            assert run is not None
            latest[name] = {"passed": run["passed"], "at": run["at"],
                            "on_current_code": run["fingerprint"] == self.current.fingerprint,
                            "definition_unchanged":
                                run["argv_sha256"] == self.definition_digest(name),
                            "valid_now": self.check_valid_now(run),
                            "output_tail": run["output_tail"][-600:]}

        def notes(kind: str) -> list[dict[str, Any]]:
            return [n for n in self.notes if n["kind"] == kind]

        return {
            "session_id": self.session_id, "repo_root": self.repo_root, "goal": self.goal,
            "status": self.status, "closed_reason": self.closed_reason,
            "agents": self.agents,
            "verified_facts": {
                "current_code": self.current.model_dump(),
                "changed_since_last_checkpoint": bool(
                    last_cp and last_cp["fingerprint"] != self.current.fingerprint),
                "last_checkpoint": last_cp,
                "latest_checks": latest,
                "registered_checks": {
                    name: {"argv": c["argv_json"], "registered_by": c["registered_by"],
                           "registered_at": _iso(c["registered_at"]),
                           "changed_during_session": _iso(c["registered_at"]) > self.started_at}
                    for name, c in sorted(self.registered_checks.items())},
            },
            "tasks": tasks,
            "agent_notes": {
                "decisions": notes("decision"), "context": notes("context")[-20:],
                "blockers": notes("blocker"), "next_steps": notes("next_step")[-5:],
                "trust": "recorded by previous agents; claims, not proof",
            },
            "checkpoints": self.checkpoints[-10:],
            "rules": RULES,
        }


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def build_view(session: dict[str, Any], events: list[dict[str, Any]], current: RepoState,
               checks: list[dict[str, Any]]) -> SessionView:
    view = SessionView(session_id=session["session_id"], repo_root=session["repo_root"],
                       goal=session["goal"], status=session["status"], current=current,
                       registered_checks={c["name"]: c for c in checks},
                       started_at=_iso(session["created_at"]))
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
                view.check_runs.append({**p, "seq": e["seq"], "at": at, "by": actor})
            case "SessionClosed":
                view.closed_reason = p["reason"]
    return view


def render_markdown(view: SessionView) -> str:
    d = view.to_dict()
    facts = d["verified_facts"]
    cur = facts["current_code"]
    lines = [f"# Continuum coding brief: {view.goal}", "",
             f"Session `{view.session_id}` in `{view.repo_root}` ({view.status}).",
             "Agents so far: " + ", ".join(f"{a['agent']} ({a['event']})" for a in view.agents),
             "", "## Verified facts (observed by Continuum)", "",
             f"- Code now: {cur['vcs']} `{(cur.get('head') or '')[:12]}` on "
             f"`{cur.get('branch')}`; {len(cur['changed_files'])} changed file(s); "
             f"fingerprint `{cur['fingerprint'][:19]}…`",
             f"- Changed since last checkpoint: {facts['changed_since_last_checkpoint']}"]
    for name, c in facts["latest_checks"].items():
        state = "passed" if c["passed"] else "FAILED"
        where = "on current code" if c["on_current_code"] else "on OLDER code (re-run)"
        if not c["definition_unchanged"]:
            where += "; check definition CHANGED since (re-run)"
        lines.append(f"- Check `{name}`: {state} {where}")
    for name, c in facts["registered_checks"].items():
        if c["changed_during_session"]:
            lines.append(f"- ⚠ Check `{name}` was (re)registered during this session by "
                         f"{c['registered_by']} at {c['registered_at']}")
    lines += ["", "## Tasks", ""]
    for t in d["tasks"]:
        mark = "✅ verified" if t["verified"] else ("☑ done (unverified)" if t["status"] == "done"
                                                   else t["status"])
        if t["needs_reverification"]:
            mark += " ⚠ needs re-verification (code changed)"
        checks = f" — checks: {', '.join(t['checks'])}" if t["checks"] else ""
        lines.append(f"- [{mark}] {t['title']} (`{t['task_id']}`){checks}")
    notes = d["agent_notes"]
    lines += ["", "## Agent notes (claims from previous agents)", ""]
    for label, key in (("Next steps", "next_steps"), ("Blockers", "blockers"),
                       ("Decisions", "decisions"), ("Context", "context")):
        if notes[key]:
            lines.append(f"**{label}**")
            lines += [f"- {n['text']}  _(by {n['by']})_" for n in notes[key]]
    if facts["last_checkpoint"]:
        cp = facts["last_checkpoint"]
        lines += ["", f"Last checkpoint: {cp['summary']} (by {cp['by']})"]
    lines += ["", "## Rules", ""] + [f"- {r}" for r in RULES]
    return "\n".join(lines) + "\n"
