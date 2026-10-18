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
MD_MAP_FILES = 15
# The user's own words are requirements: the first request (it usually states the task)
# plus the newest ones, whole up to a generous bound. Low-information turns ("continue",
# "ok") are left to the journal, except the newest one (it may answer a question).
KEEP_REQUESTS = 6
REQUEST_CHARS = 2000
MD_REQUEST_CHARS = 1200
REPORT_CHARS = 1500
_LOW_INFO = frozenset({
    "continue", "go on", "ok", "okay", "k", "yes", "y", "yep", "no", "n", "thanks",
    "thank you", "proceed", "go", "next", "do it", "keep going", "carry on", "go ahead",
    "sure", "continue where we left off", "continue where you left off", "resume", "done",
    "great", "nice", "cool", "good", "lgtm"})


def low_information(text: str) -> bool:
    return " ".join(text.lower().replace("!", " ").replace(".", " ").split()) in _LOW_INFO


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "… (truncated)"


RULES = [
    "Read this brief before changing code; continue from `next_steps` and open tasks.",
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
    moves: list[dict[str, Any]] = field(default_factory=list)  # checkout changes
    start_branch: str | None = None

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
            "user_requests": self._user_requests(),
            "attention": self.attention(),
            "tasks": tasks,
            "agent_notes": agent_notes,
            "rules": RULES,
        }

    def _user_requests(self) -> dict[str, Any]:
        users = [c for c in self.chat if c["role"] == "user"]
        keep: list[dict[str, Any]] = []
        low = 0
        seen: set[str] = set()
        for i, c in enumerate(users):
            if i < len(users) - 1 and low_information(c["text"]):
                low += 1
                continue
            if c["text"] in seen:  # the same words captured twice (hook and import)
                continue
            seen.add(c["text"])
            keep.append(c)
        selected = keep if len(keep) <= KEEP_REQUESTS + 1 else \
            keep[:1] + keep[-KEEP_REQUESTS:]
        items = [{"text": _clip(c["text"], REQUEST_CHARS), "at": c["at"],
                  "source": c["source"], "chars_omitted": c["chars_omitted"]}
                 for c in selected]
        return {"captured": len(users), "items": items,
                "omitted": len(keep) - len(selected), "omitted_low_information": low,
                "trust": "the user's own words, captured by the user's app (hook or "
                         "transcript import); requirements, newest wins"}

    def _last_report(self) -> dict[str, Any] | None:
        reports = [c for c in self.chat if c["role"] == "agent"]
        if not reports:
            return None
        r = reports[-1]
        return {"text": _clip(r["text"], REPORT_CHARS), "at": r["at"], "source": r["source"]}

    def attention(self) -> list[str]:
        """Gaps the next model must know about, computed from the journal (no model)."""
        out: list[str] = []
        users = [c for c in self.chat if c["role"] == "user"]
        steps = [n for n in self.notes if n["kind"] == "next_step"]
        if not self.chat:
            out.append("No chat was captured for this session: requirements the user stated "
                       "only in an earlier chat are not here. If something seems missing, ask "
                       "the user (a human can enable capture: `continuum coding setup`).")
        elif users and (not steps or users[-1]["seq"] > steps[-1]["seq"]):
            out.append("The user's latest request is newer than the last recorded next step: "
                       "it is the current instruction.")
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
                       last_seq=int(session["last_seq"]))
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
            case "ChatCaptured":
                view.chat.append({"role": p["role"], "text": p["text"], "source": p["source"],
                                  "chars_omitted": p.get("chars_omitted", 0), "at": at,
                                  "seq": e["seq"]})
            case "SessionClosed":
                view.closed_reason = p["reason"]
    return view


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
    req = d["user_requests"]
    if req["items"]:
        lines += ["", "## What the user asked (their own words; newest last, newest wins)", ""]
        for i, r in enumerate(req["items"]):
            more = f" _(+{r['chars_omitted']} chars not stored)_" if r["chars_omitted"] else ""
            lines.append(f"{i + 1}. {_line(r['text'], MD_REQUEST_CHARS)}{more}")
            if i == 0 and req["omitted"]:
                lines.append(f"   _… {req['omitted']} request(s) in between omitted "
                             "(`coding_recall` finds them)_")
        if req["omitted_low_information"]:
            lines.append(f"_({req['omitted_low_information']} short replies such as "
                         "\"continue\" not shown)_")
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
    if notes["last_report"]:
        r = notes["last_report"]
        lines += ["", f"Last message from the previous agent ({_line(r['source'], 40)}, "
                      f"{r['at']}; a claim): {_line(r['text'], REPORT_CHARS)}"]
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
