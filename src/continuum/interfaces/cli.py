# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""``continuum`` command-line interface. All output is JSON."""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path
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
    hk = sub.add_parser("hook", help="app hook: capture the chat, show the brief at session "
                                     "start (continuum hook claude-code|codex|gemini-cli ...)")
    hk.add_argument("app")
    hk.add_argument("payload", nargs="?")

    adm = sub.add_parser("admin", help="operator only: give people their own isolated "
                                       "coding memory in one database (D-040)")
    asub = adm.add_subparsers(dest="admin_cmd", required=True)
    for name, text in (("grant", "let a PostgreSQL role use Continuum (sees only its own)"),
                       ("revoke", "remove a role's access (its sessions stay)")):
        a = asub.add_parser(name, help=text)
        a.add_argument("role")
    ra = asub.add_parser("reassign", help="move sessions and checks to another role")
    ra.add_argument("old_role")
    ra.add_argument("new_role")
    asub.add_parser("users", help="roles with sessions or access (no content)")
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
    su = csub.add_parser("setup", help="write AGENTS.md/CLAUDE.md rules, Claude Code hooks "
                                       "and .mcp.json for a repository (idempotent)")
    su.add_argument("repo", nargs="?", default=".")
    su.add_argument("--dry-run", action="store_true")
    cpt = csub.add_parser("capture", help="record a chat message verbatim (for apps without "
                                          "hooks); text from the argument or stdin")
    cpt.add_argument("text", nargs="?")
    cpt.add_argument("--repo", default=".")
    cpt.add_argument("--role", choices=["user", "agent"], default="user")
    cpt.add_argument("--source", default="cli")
    cpt.add_argument("--chat")
    imp = csub.add_parser("import", help="import a chat transcript (Claude Code, Codex, "
                                         "Gemini CLI or any JSON/JSONL chat) idempotently")
    imp.add_argument("transcript")
    imp.add_argument("--repo", default=".")
    imp.add_argument("--source", default="transcript")
    imp.add_argument("--chat")
    fg = csub.add_parser("forget", help="human-only: erase a session completely (chat, notes, "
                                        "check output), or every closed session older than "
                                        "N days; leaves a content-free tombstone")
    fg.add_argument("session", nargs="?")
    fg.add_argument("--older-than-days", type=int)
    fg.add_argument("--include-active", action="store_true",
                    help="with --older-than-days: also erase sessions still active")
    fg.add_argument("--actor", required=True, help="your (human) id")
    fg.add_argument("--reason", required=True)
    fg.add_argument("--yes", action="store_true", help="confirm: erasure cannot be undone")
    er = csub.add_parser("erasures", help="list erasure tombstones (no content)")
    er.add_argument("--limit", type=int, default=50)
    rcl = csub.add_parser("recall", help="search everything the session remembers")
    rcl.add_argument("query")
    rcl.add_argument("--repo", default=".")
    rcl.add_argument("--session")
    rcl.add_argument("--all", action="store_true", help="every session of this repository")
    rcl.add_argument("--limit", type=int, default=8)
    rcl.add_argument("--since", help="ISO date or time")
    rcl.add_argument("--until", help="ISO date or time")
    shw = csub.add_parser("show", help="one journal event ([#N] in the brief) or a stored "
                                       "tool output / diff")
    shw.add_argument("session")
    shw.add_argument("--seq", type=int)
    shw.add_argument("--blob")
    shw.add_argument("--offset", type=int, default=0)
    mem = csub.add_parser("memory", help="the model-written working memory (D-048): "
                                         "update it now, or show it")
    mem.add_argument("--repo", default=".")
    mem.add_argument("--session")
    mem.add_argument("--update", action="store_true", help="bring it up to date now")
    med = csub.add_parser("media", help="save an image or document from the chat or a tool "
                                        "result ([image #...] in the brief) to a file")
    med.add_argument("session")
    med.add_argument("image")
    med.add_argument("--out", required=True, help="file to write")
    unr = csub.add_parser("unresolved", help="calls whose outcome is unknown")
    unr.add_argument("session")
    rsv = csub.add_parser("resolve", help="human-only: record what happened to a call whose "
                                          "outcome was unknown (unblocks external actions)")
    rsv.add_argument("session")
    rsv.add_argument("call", help="the call id the brief shows (or a unique prefix)")
    rsv.add_argument("--outcome", required=True,
                     choices=["applied", "not_applied", "accepted_unknown"])
    rsv.add_argument("--actor", required=True)
    rsv.add_argument("--reason", required=True)
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
    try:
        code = _main(argv)
        sys.stdout.flush()  # buffered output reaches a closed pipe here, not at exit
        return code
    except OSError as exc:
        # the reader went away (e.g. `continuum coding brief | head`): not an error.
        # Windows reports a write into a closed pipe as EINVAL, not EPIPE.
        if not (isinstance(exc, BrokenPipeError)
                or (os.name == "nt" and exc.errno == errno.EINVAL)):
            raise
        # Point stdout at devnull so the interpreter's final flush cannot raise again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


def _main(argv: list[str] | None = None) -> int:
    _utf8_output()
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["hook"]:  # app hooks: never fail, never print errors into the chat
        from continuum.coding.hooks import main as hook_main

        return hook_main(raw[1:])
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
    if args.cmd == "coding" and args.coding_cmd == "setup":  # files only, no database
        from continuum.coding.install import setup

        try:
            _print(setup(args.repo, dry_run=args.dry_run))
        except (OSError, ValueError) as exc:
            _print({"error": str(exc), "code": "setup_failed"})
            return 2
        return 0
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
            case "admin":
                from continuum.coding import access

                match args.admin_cmd:
                    case "grant":
                        _print(access.grant(sdk.store, args.role))
                    case "revoke":
                        _print(access.revoke(sdk.store, args.role))
                    case "reassign":
                        _print(access.reassign(sdk.store, args.old_role, args.new_role))
                    case "users":
                        _print(access.users(sdk.store))
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
        case "capture":
            from continuum.coding.transcript import Message

            text = args.text if args.text is not None else sys.stdin.read()
            _print(coding.capture(args.repo, [Message(args.role, text)], source=args.source,
                                  chat_id=args.chat))
        case "import":
            from continuum.coding.transcript import read_all

            chat = args.chat or args.transcript
            messages, tools = read_all(args.transcript, app=args.source, chat=chat)
            out = coding.capture(args.repo, messages, source=args.source, chat_id=chat,
                                 history=True)
            # what the agent did, from the same transcript: tool calls with results,
            # subagents, compaction (D-042); idempotent like the messages
            if tools and out.get("session_id"):
                done = coding.record_activity(args.repo, tools, source=args.source,
                                              chat_id=chat)
                out["tool_events_recorded"] = done.get("recorded", 0)
            _print(out)
        case "forget":
            if (args.session is None) == (args.older_than_days is None):
                _print({"error": "give a SESSION or --older-than-days N", "code": "usage"})
                return 2
            if not args.yes:
                _print({"error": "erasure cannot be undone; repeat with --yes",
                        "code": "confirmation_required"})
                return 2
            if args.session:
                _print(coding.forget(args.session, actor=args.actor, reason=args.reason))
            else:
                _print(coding.forget_older_than(args.older_than_days, actor=args.actor,
                                                reason=args.reason,
                                                include_active=args.include_active))
        case "erasures":
            _print(coding.erasures(args.limit))
        case "recall":
            _print(coding.recall(args.query, session_id=args.session,
                                 repo_path=None if args.session else args.repo,
                                 all_sessions=args.all, limit=args.limit,
                                 since=args.since, until=args.until))
        case "show":
            _print(coding.show(args.session, seq=args.seq, blob=args.blob,
                               offset=args.offset))
        case "memory":
            sid = args.session or coding.latest_active(args.repo)
            if args.update:
                _print(coding.update_working_memory(sid, force=True))
            else:
                brief = coding.brief(sid)
                section = brief["markdown"].split("## Working memory", 1)
                _print({"session_id": sid, "working_memory": brief.get("working_memory"),
                        "markdown": ("## Working memory" + section[1].split("\n## ", 1)[0])
                        if len(section) > 1 else None})
        case "media":
            found = coding.media(args.session, args.image)
            Path(args.out).write_bytes(found.pop("content"))
            _print({**found, "written": args.out})
        case "unresolved":
            _print(coding.unresolved(args.session))
        case "resolve":
            from continuum.coding import spool

            out = coding.resolve_unknown(args.session, args.call, args.outcome,
                                         actor=args.actor, reason=args.reason)
            spool.set_blocks(coding.sessions_root(args.session),
                             coding.unresolved(args.session), args.session)
            _print(out)
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
