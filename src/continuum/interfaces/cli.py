"""``continuum`` command-line interface. All output is JSON."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from typing import Any

import psycopg
from psycopg_pool import PoolTimeout

from continuum.core.canonical import canonical_json
from continuum.core.errors import ContinuumError
from continuum.core.redaction import Redactor
from continuum.core.runtime import RunResult
from continuum.core.tracing import configure_logging
from continuum.interfaces.sdk import Continuum


def _print(value: Any) -> None:
    if isinstance(value, RunResult):
        value = {**asdict(value), "status": value.status.value}
    json.dump(value, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="continuum", description=__doc__)
    p.add_argument("--log-level", default="WARNING", type=str.upper,
                   choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                   help="JSON trace log level on stderr (e.g. INFO for every event)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("db-init", help="create the PostgreSQL schema")
    sub.add_parser("doctor", help="check database, schema, tasks and model profiles")
    sub.add_parser("tasks", help="list pre-registered tasks")
    sub.add_parser("runs", help="list run ids")
    run = sub.add_parser("run", help="start a pre-registered task")
    run.add_argument("task_id")
    run.add_argument("--model", dest="model_alias",
                     help="alias from config/models.yaml or a provider/model-id")
    res = sub.add_parser("resume", help="resume a run (never a live replay)")
    res.add_argument("run_id")
    res.add_argument("--model", dest="model_alias",
                     help="continue on this alias or provider/model-id "
                          "(e.g. anthropic/claude-opus-5-5); verified work carries over")
    st = sub.add_parser("status", help="show durable run status")
    st.add_argument("run_id")
    ins = sub.add_parser("inspect", help="side-effect-free diagnostic replay")
    ins.add_argument("run_id")
    tr = sub.add_parser("trace", help="export the run's JSON-lines trace (read-only)")
    tr.add_argument("run_id")
    tr.add_argument("--out", help="write to this file instead of stdout")
    rv = sub.add_parser("resolve", help="record a human decision for an action")
    rv.add_argument("run_id")
    rv.add_argument("action_id")
    rv.add_argument("decision", choices=["confirmed", "abort", "retry_at_risk", "approve",
                                         "reject"])
    rv.add_argument("--actor", required=True)
    rv.add_argument("--reason", required=True)
    rv.add_argument("--evidence", dest="evidence_ref")
    cn = sub.add_parser("cancel", help="stop new dispatches for a run")
    cn.add_argument("run_id")
    cn.add_argument("--actor", required=True)
    cn.add_argument("--reason", required=True)
    sv = sub.add_parser("serve", help="minimal loopback HTTP API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sub.add_parser("mcp", help="local stdio MCP server for coding agents")

    coding = sub.add_parser("coding", help="coding-session continuity across models and chats")
    csub = coding.add_subparsers(dest="coding_cmd", required=True)
    checks = csub.add_parser("checks", help="human-only: register or list repository checks")
    chsub = checks.add_subparsers(dest="checks_cmd", required=True)
    add = chsub.add_parser("add", help="register a check: continuum coding checks add REPO "
                                       "NAME --actor ME -- pytest -q")
    add.add_argument("repo")
    add.add_argument("name")
    add.add_argument("--actor", required=True, help="your (human) id")
    add.add_argument("--timeout", type=int, default=600)
    # The check's own command follows `--`; it is split off before parsing (main()).
    ls = chsub.add_parser("list")
    ls.add_argument("repo")
    st2 = csub.add_parser("start", help="start a coding session for a repository")
    st2.add_argument("repo")
    st2.add_argument("--goal", required=True)
    st2.add_argument("--agent", default="human")
    br = csub.add_parser("brief", help="print the handoff brief")
    br.add_argument("--session")
    br.add_argument("--repo")
    br.add_argument("--json", action="store_true", help="JSON instead of markdown")
    ss = csub.add_parser("sessions")
    ss.add_argument("--repo")
    cl = csub.add_parser("close")
    cl.add_argument("session")
    cl.add_argument("--reason", required=True)
    cl.add_argument("--actor", required=True)
    # Agent operations for shell-only agents (same protocol as the MCP coding_* tools).
    rs = csub.add_parser("resume", help="join the repo's session as AGENT and print the brief")
    rs.add_argument("--repo", default=".")
    rs.add_argument("--session")
    rs.add_argument("--agent", required=True, help="your exact model id")
    rs.add_argument("--json", action="store_true")
    nt = csub.add_parser("note", help="record decision|context|next_step|blocker")
    nt.add_argument("session")
    nt.add_argument("kind", choices=["decision", "context", "next_step", "blocker"])
    nt.add_argument("text")
    nt.add_argument("--agent", required=True)
    cp = csub.add_parser("checkpoint", help="record what changed (state observed by Continuum)")
    cp.add_argument("session")
    cp.add_argument("--summary", required=True)
    cp.add_argument("--agent", required=True)
    rc = csub.add_parser("check", help="run a human-registered check and record the result")
    rc.add_argument("session")
    rc.add_argument("name")
    rc.add_argument("--agent", required=True)
    tk = csub.add_parser("task", help="add, complete or update tasks")
    tsub = tk.add_subparsers(dest="task_cmd", required=True)
    ta = tsub.add_parser("add")
    ta.add_argument("session")
    ta.add_argument("title")
    ta.add_argument("--check", action="append", default=[], dest="checks")
    ta.add_argument("--agent", required=True)
    td = tsub.add_parser("done", help="complete (refused unless checks pass on current code)")
    td.add_argument("session")
    td.add_argument("task_id")
    td.add_argument("--agent", required=True)
    td.add_argument("--note", default="")
    tst = tsub.add_parser("status")
    tst.add_argument("session")
    tst.add_argument("task_id")
    tst.add_argument("status", choices=["open", "in_progress", "blocked", "dropped"])
    tst.add_argument("--agent", required=True)
    tst.add_argument("--note", default="")
    return p


def _utf8_output() -> None:
    """Agents read our output through pipes. On Windows a pipe defaults to the ANSI code
    page (e.g. cp1252), which cannot encode the brief's symbols and would crash."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    raw = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    # `coding checks add … -- cmd args`: everything after `--` is the check's argv, never
    # Continuum options. Other commands keep argparse's normal `--` escape.
    if "--" in raw:
        cut = raw.index("--")
        head = raw[:cut]
        if any(head[i:i + 3] == ["coding", "checks", "add"] for i in range(len(head))):
            raw, command = head, raw[cut + 1:]
    args = build_parser().parse_args(raw)
    args.argv = command
    configure_logging(args.log_level)
    sdk: Continuum | None = None
    try:
        sdk = Continuum()
        if args.cmd not in ("db-init", "doctor"):
            sdk.ensure_schema()  # first use on a fresh database just works
        match args.cmd:
            case "db-init":
                sdk.init_db()
                _print({"ok": True})
            case "doctor":
                report = sdk.doctor()
                _print(report)
                return 0 if report["ok"] else 1
            case "tasks":
                _print(sdk.tasks())
            case "runs":
                _print(sdk.runs())
            case "run":
                _print(sdk.start(args.task_id, model_alias=args.model_alias))
            case "resume":
                _print(sdk.resume(args.run_id, model_alias=args.model_alias))
            case "status":
                _print(sdk.status(args.run_id))
            case "inspect":
                _print(sdk.inspect(args.run_id))
            case "trace":
                lines = "".join(canonical_json(r) + "\n" for r in sdk.trace(args.run_id))
                if args.out:
                    with open(args.out, "w", encoding="utf-8") as fh:
                        fh.write(lines)
                    _print({"ok": True, "out": args.out})
                else:
                    sys.stdout.write(lines)
            case "resolve":
                _print(sdk.resolve_action(args.run_id, args.action_id, args.decision,
                                          actor=args.actor, reason=args.reason,
                                          evidence_ref=args.evidence_ref))
            case "cancel":
                _print(sdk.cancel(args.run_id, actor=args.actor, reason=args.reason))
            case "serve":
                # Optional extras ([http], [mcp]) are imported only when used.
                from continuum.interfaces.http_api import serve

                serve(sdk, host=args.host, port=args.port)
            case "mcp":
                from continuum.interfaces.mcp_facade import run_stdio

                run_stdio(sdk)
            case "coding":
                return _coding(sdk, args)
    except ContinuumError as exc:
        _print({"error": str(exc), "code": exc.code})
        return 2
    except (psycopg.OperationalError, PoolTimeout) as exc:
        _print({"error": f"database unavailable ({type(exc).__name__}); check "
                         "CONTINUUM_DATABASE_URL and that PostgreSQL is running",
                "code": "database_unavailable"})
        return 2
    except Exception as exc:  # anything else: one masked line, never a raw traceback
        message = Redactor.from_environment().text(str(exc))[:300]
        _print({"error": f"internal error ({type(exc).__name__}): {message}",
                "code": "internal",
                "hint": "re-run with --log-level DEBUG for details"})
        logging.getLogger(__name__).debug("internal error", exc_info=True)
        return 2
    finally:
        if sdk is not None:
            sdk.close()
    return 0


def _coding(sdk: Continuum, args: argparse.Namespace) -> int:
    coding = sdk.coding
    match args.coding_cmd:
        case "checks" if args.checks_cmd == "add":
            _print(coding.register_check(args.repo, args.name, args.argv, actor=args.actor,
                                         timeout_seconds=args.timeout))
        case "checks":
            _print(coding.checks(args.repo))
        case "start":
            _print({"session_id": coding.start(args.repo, args.goal,
                                               agent=args.agent)["session_id"]})
        case "brief":
            session = args.session or coding.latest_active(args.repo)
            brief = coding.brief(session)
            if args.json:
                _print({k: v for k, v in brief.items() if k != "markdown"})
            else:
                sys.stdout.write(brief["markdown"])
        case "sessions":
            _print(coding.sessions(args.repo))
        case "close":
            _print(coding.close(args.session, args.reason, agent=args.actor, human=True))
        case "resume":
            brief = coding.resume(agent=args.agent, session_id=args.session,
                                  repo_path=None if args.session else args.repo)
            if args.json:
                _print({k: v for k, v in brief.items() if k != "markdown"})
            else:
                sys.stdout.write(brief["markdown"])
        case "note":
            _print(coding.note(args.session, args.kind, args.text, agent=args.agent))
        case "checkpoint":
            _print(coding.checkpoint(args.session, args.summary, agent=args.agent))
        case "check":
            result = coding.run_check(args.session, args.name, agent=args.agent)
            _print(result)
            return 0 if result["passed"] else 1
        case "task" if args.task_cmd == "add":
            _print(coding.add_task(args.session, args.title, checks=args.checks,
                                   agent=args.agent))
        case "task" if args.task_cmd == "done":
            result = coding.complete_task(args.session, args.task_id, agent=args.agent,
                                          note=args.note)
            _print(result)
            return 0 if result["status"] == "done" else 1
        case "task":
            _print(coding.set_task_status(args.session, args.task_id, args.status,
                                          agent=args.agent, note=args.note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
