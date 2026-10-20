# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""App hooks: journal the conversation and restore memory in *any* coding agent app.

One command serves every app: ``continuum hook <app> [payload]``. The app passes its
hook payload as JSON on stdin (Claude Code, Gemini CLI, Codex hooks) or as the last
argument (Codex ``notify``). The payload is read by meaning, not by one app's schema:

* ``prompt``, ``input-messages`` / ``input_messages``  -> the user's words
* ``last-assistant-message`` / ``last_assistant_message`` -> the agent's report
* ``transcript_path``                                    -> the whole chat, re-imported
  idempotently (so a missed hook call is caught up at the next one)
* ``cwd``                                                -> which repository
* ``session_id`` / ``thread-id`` / ``conversation_id``   -> which chat
* ``hook_event_name == SessionStart``                    -> print the brief, which the
  app adds to the new chat's context (memory restored before the model's first turn)

Tool calls (D-042): ``PreToolUse``/``PostToolUse``/``PostToolUseFailure`` (Claude Code,
Codex), ``BeforeTool``/``AfterTool`` (Gemini CLI), Cursor's shell, MCP and file-edit hooks.
Each is normalized by ``activity.from_hook``. Before and after a call that can write, the
working copy's content is snapshotted, so the journal holds exactly what changed.

A hook must never break or slow down the agent. It does not wait for the database: it
redacts the payload, writes it to a local spool and starts a background drainer
(``spool``), so it costs milliseconds and loses nothing when the database is down. Only
``SessionStart`` waits (briefly) to print the brief. One deliberate exception to "never
interfere": while a call that could change something outside the repository has an
unknown outcome, ``PreToolUse`` denies new external calls until a person resolves it
(AGENTS.md invariant 8). Failures are logged to ``$CONTINUUM_HOME/hooks.log`` and the
command still exits 0. ``CONTINUUM_HOOKS=off`` disables it. No model is called.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

from continuum.coding import activity
from continuum.coding import spool as spool_mod

if TYPE_CHECKING:   # the hot path does not load the transcript parser
    from continuum.coding.transcript import Message

CHAT_KEYS = ("session_id", "sessionId", "thread-id", "thread_id", "conversation_id",
             "conversationId")
USER_KEYS = ("input-messages", "input_messages")
AGENT_KEYS = ("last-assistant-message", "last_assistant_message", "prompt_response")
TRANSCRIPT_KEYS = ("transcript_path", "transcriptPath")
CWD_KEYS = ("cwd", "workspace", "workspace_root", "project_dir")
HOOK_DB_TIMEOUT = "3"  # seconds; a hook waits at most this long for the database
# At the end of a turn only the newest part of the transcript is read (the prompt itself
# was captured when it was submitted), so a long chat does not slow every turn down. At
# compaction and session end the whole transcript is re-read to catch up anything missed.
TURN_TAIL_BYTES = 2 << 20
TURN_EVENTS = frozenset({"stop", "agent-turn-complete", "afteragent", "sessionend",
                         "stop_hook"})
SESSION_START_DRAIN_SECONDS = 8.0   # SessionStart waits at most this long for the spool
SNAPSHOT_BUDGET_SECONDS = 5.0       # a snapshot gives up after this (apps kill slow hooks)
# Cursor blocks a permission hook's action when its output is not valid JSON
CURSOR_PERMISSION_EVENTS = frozenset({"pretooluse", "beforeshellexecution",
                                      "beforemcpexecution", "beforereadfile",
                                      "subagentstart"})

CONTEXT_HEADER = (
    "Continuum: this repository has an active coding session. The brief below is the "
    "work so far, kept outside any chat (it survives a change of model, chat or app). "
    "Continue from it. Call coding_resume(repo_path, agent=<your model id>) to join "
    "the session and coding_recall(query) to search older history.\n\n")


STDIN_WAIT = 5.0  # seconds; an inherited stdin that never closes must not hang the app


def _read_stdin(stdin: Any) -> str | None:
    """Read the payload without risking a hang (works on every OS: a reader thread)."""
    import threading

    box: list[str] = []
    reader = threading.Thread(target=lambda: box.append(stdin.read()), daemon=True)
    reader.start()
    reader.join(STDIN_WAIT)
    return box[0] if box else None


def parse_payload(arg: str | None, stdin: Any) -> dict[str, Any]:
    raw = arg
    if raw is None and stdin is not None and not stdin.isatty():
        raw = _read_stdin(stdin)
    if not raw or not raw.strip():
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def family(app: str) -> str:
    """Which hook protocol an app speaks (output formats differ; inputs are mapped in
    ``activity.from_hook``)."""
    name = app.lower()
    for prefix, fam in (("claude", "claude"), ("qwen", "claude"), ("codex", "codex"),
                        ("gemini", "gemini"), ("cursor", "cursor")):
        if name.startswith(prefix):
            return fam
    return "generic"


def deny_output(app: str, reason: str) -> str:
    """The app's own way of refusing a tool call (each app documents one)."""
    fam = family(app)
    if fam == "cursor":
        return json.dumps({"permission": "deny", "user_message": reason,
                           "agent_message": reason})
    if fam == "gemini":
        return json.dumps({"decision": "deny", "reason": reason})
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}})


def allow_output(app: str, event: str) -> str:
    """What a hook prints when it lets a call through: nothing, except for Cursor's
    permission hooks, which must answer with valid JSON."""
    if family(app) == "cursor" and event in CURSOR_PERMISSION_EVENTS:
        return json.dumps({"permission": "allow"})
    if family(app) == "cursor" and event == "beforesubmitprompt":
        return json.dumps({"continue": True})
    return ""


def context_output(app: str, text: str) -> str:
    """The brief, in the form the app adds to a new chat's context."""
    fam = family(app)
    if fam == "cursor":
        return json.dumps({"additional_context": text})
    if fam in ("gemini", "codex"):       # Gemini: stdout must be JSON; Codex: JSON context
        return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                  "additionalContext": text}})
    return text


def _cwd(payload: dict[str, Any]) -> str:
    """The project directory of a hook call (Cursor sends ``workspace_roots`` only)."""
    found = _first_str(payload, CWD_KEYS)
    if found:
        return found
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str) and roots[0].strip():
        return roots[0]
    return os.getcwd()


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _event(payload: dict[str, Any]) -> str:
    return str(payload.get("hook_event_name") or payload.get("hookEventName")
               or payload.get("type") or "").lower()


def _read(payload: dict[str, Any], app: str, chat: str | None
          ) -> tuple[list[Message], list[dict[str, Any]]]:
    path = _first_str(payload, TRANSCRIPT_KEYS)
    if not path or not Path(path).expanduser().is_file():
        return [], []
    from continuum.coding.transcript import read_all

    tail = TURN_TAIL_BYTES if _event(payload) in TURN_EVENTS else None
    return read_all(Path(path).expanduser(), app=app, chat=chat,
                    **({"tail_bytes": tail} if tail else {}))


def messages_from(payload: dict[str, Any], history: list[Message] | None = None
                  ) -> tuple[list[Message], list[Message]]:
    """What a hook payload carries: (the chat transcript in order, this turn's messages).

    The transcript is re-imported idempotently (occurrences are counted within it). This
    turn's fields (``prompt``, ``input-messages``, ``last-assistant-message``) are new
    messages, except when the transcript already ends with them."""
    from continuum.coding.transcript import Message, clean_user_text

    if history is None:
        history, _ = _read(payload, "transcript", None)
    turn: list[Message] = []
    # what the app injects into a prompt (reminders, background-task notices) is not the
    # user's words, whichever route it arrives by
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and clean_user_text(prompt):
        turn.append(Message("user", clean_user_text(prompt)))
    for key in USER_KEYS:
        items = payload.get(key)
        if isinstance(items, list):
            turn += [Message("user", clean_user_text(m)) for m in items
                     if isinstance(m, str) and clean_user_text(m)]
    agent = _first_str(payload, AGENT_KEYS)
    if not agent and _event(payload) == "afteragentresponse":     # Cursor
        agent = _first_str(payload, ("text",))
    if agent:
        turn.append(Message("agent", agent))
    recent = history[-3:]
    turn = [m for m in turn if not any(
        r.role == m.role and r.text.strip() == m.text.strip() for r in recent)]
    return history, turn


def handle(app: str, payload: dict[str, Any], coding: Any,
           snap: dict[str, Any] | None = None) -> str:
    """Apply one hook call to the journal; returns what to print (context, or nothing).

    Called by the drainer for spooled payloads (and directly in tests)."""
    from continuum.core.errors import NotFoundError

    cwd = _cwd(payload)
    event = _event(payload)
    chat = _first_str(payload, CHAT_KEYS)
    if event == "sessionstart":
        try:
            sid = coding.follow(cwd, agent=f"capture:{app}")
        except NotFoundError:
            return ""
        coding.record_activity(cwd, [], source=app, reconcile="session_start", chat_id=chat)
        _update_blocks(coding, sid)
        return CONTEXT_HEADER + str(coding.brief(sid)["markdown"])
    env = activity.from_hook(app, payload)
    if env is not None:
        if snap:
            env["snapshot"] = snap
        if payload.get("continuum_denied"):
            env = {**env, "phase": "start_finish", "status": "denied",
                   "output": str(payload["continuum_denied"])}
        envs = [env]
        if env["phase"] == "subagent_stop":
            envs = [*_subagent_activity(env, app), env]
        if envs:
            out = coding.record_activity(cwd, envs, source=app)
            # a late result clears an unknown outcome: refresh the deny list at once
            if out.get("session_id") and env["phase"] != "start" \
                    and spool_mod.blocks_for(cwd):
                _update_blocks(coding, out["session_id"])
        return ""
    history, tools = _read(payload, app, chat)
    _, turn = messages_from(payload, history)
    # this turn first, then the transcript: the transcript's copy of a message captured
    # live is then recognised as the same message (an alias), never a second one
    if turn:
        coding.capture(cwd, turn, source=app, chat_id=chat)
    if history:
        coding.capture(cwd, history, source=app, chat_id=chat, history=True)
    if snap and event in TURN_EVENTS:
        tools = [*tools, {"v": 1, "app": app, "chat": chat, "phase": "turn_end",
                          "snapshot": snap}]
    if tools or event in TURN_EVENTS:
        out = coding.record_activity(cwd, tools, source=app, chat_id=chat,
                                     reconcile="turn_end" if event in TURN_EVENTS else None)
        if out.get("session_id"):
            _update_blocks(coding, out["session_id"])
    return ""


def _subagent_activity(env: dict[str, Any], app: str) -> list[dict[str, Any]]:
    """A subagent finished: its own tool calls, from its transcript, under its id."""
    from continuum.coding.transcript import read_all

    path = env.get("transcript")
    if not isinstance(path, str) or not Path(path).expanduser().is_file():
        return []
    _, tools = read_all(Path(path).expanduser(), app=app, chat=env.get("chat"))
    for t in tools:
        t["agent_id"] = t.get("agent_id") or env.get("agent_id")
        t["agent_type"] = env.get("agent_type")
    return tools


def _update_blocks(coding: Any, session_id: str) -> None:
    try:
        root = coding.sessions_root(session_id)
        spool_mod.set_blocks(root, coding.unresolved(session_id), session_id)
    except Exception as exc:  # blocks are a convenience copy; the journal is the truth
        _log(f"blocks: {type(exc).__name__}")


def _log(line: str) -> None:
    try:
        home = Path(os.environ.get("CONTINUUM_HOME") or Path.home() / ".continuum")
        home.mkdir(parents=True, exist_ok=True)
        with (home / "hooks.log").open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + line.rstrip() + "\n")
    except OSError:
        pass


DENY_REASON = (
    "Continuum: a previous call that could change something outside this repository has "
    "an unknown outcome ({tools}). New external actions are blocked until a person checks "
    "what happened and records it: `continuum coding resolve {session} {call} --outcome "
    "applied|not_applied|accepted_unknown --actor NAME --reason TEXT`. Do not repeat the "
    "call; ask the user. Local work (reading, editing, tests) is not blocked.")


def deny_reason(blocked: list[dict[str, Any]]) -> str:
    first = blocked[0]
    session = str(first.get("session_id") or "SESSION")
    call = str(first.get("call_key", "CALL")).split(":", 1)[-1][:12]
    return DENY_REASON.format(tools=", ".join(str(b.get("tool")) for b in blocked[:3]),
                              session=session, call=call)


def fast(app: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """The hook's hot path, without the database: (what to print, entry to spool)."""
    event = _event(payload)
    cwd = _cwd(payload)
    entry: dict[str, Any] = {"app": app, "payload": payload, "snapshot": None}
    env = activity.from_hook(app, payload)
    out = allow_output(app, event)
    from continuum.coding import snapshot

    # what a call changed: snapshots around calls that can change files (edits, shell
    # commands), and at the end of a turn (changes made between tool calls)
    # the deny decision first: it must never wait for (or be lost to) a slow snapshot
    if env is not None and env["phase"] == "start" and env["effect"] == "external":
        blocked = spool_mod.blocks_for(cwd)
        if blocked:
            reason = deny_reason(blocked)
            payload["continuum_denied"] = reason
            out = deny_output(app, reason)
    # what a call changed: snapshots around calls that can change files (edits, shell
    # commands), and at the end of a turn (changes made between tool calls); bounded so
    # the app never kills the hook (the call is still journaled without one)
    can_write = env is not None and env["phase"] in ("start", "finish", "start_finish") \
        and (env["effect"] == "write" or env["kind"] == "shell")
    if can_write or event in TURN_EVENTS:
        try:
            entry["snapshot"] = snapshot.take(cwd, budget=SNAPSHOT_BUDGET_SECONDS)
        except Exception as exc:   # evidence is optional; capturing the call is not
            _log(f"snapshot: {_masked(exc)}")
    return out, entry


def drain(coding: Any, *, deadline: float | None = None) -> int:
    """Apply spooled hook payloads in order. Returns how many were applied."""
    lock = spool_mod.Lock()
    # SessionStart waits for a running drainer to finish, so the brief is complete
    while not lock.acquire():
        if deadline is None or time.monotonic() > deadline:
            return 0
        time.sleep(0.05)
    applied = 0
    try:
        for path, entry in spool_mod.entries():
            if deadline is not None and time.monotonic() > deadline:
                break
            if entry is None or not isinstance(entry.get("payload"), dict):
                spool_mod.quarantine(path)
                continue
            try:
                handle(str(entry.get("app") or "generic")[:40], entry["payload"], coding,
                       entry.get("snapshot"))
            except Exception as exc:
                if _database_down(exc):
                    raise   # keep the file and everything after it; retry later
                # one entry that cannot be applied must not block the ones after it
                _log(f"spool: {path.name} not applied ({_masked(exc)}); moved to failed/")
                if os.environ.get("CONTINUUM_HOOKS_DEBUG"):
                    _log(traceback.format_exc())
                spool_mod.quarantine(path)
                continue
            path.unlink(missing_ok=True)
            applied += 1
            lock.touch()
    finally:
        lock.release()
    return applied


def _database_down(exc: BaseException) -> bool:
    """The database is unreachable (retry later), as opposed to an entry it rejects."""
    import psycopg
    from psycopg_pool import PoolTimeout

    from continuum.core.errors import ContinuumError

    return isinstance(exc, psycopg.OperationalError | PoolTimeout) or (
        isinstance(exc, ContinuumError) and exc.code == "database_unavailable")


def _with_sdk(work: Any) -> Any:
    import logging

    # a database outage is logged to hooks.log, never printed into the user's app
    logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)
    os.environ.setdefault("CONTINUUM_DB_TIMEOUT_SECONDS", HOOK_DB_TIMEOUT)
    from continuum.interfaces.sdk import Continuum

    sdk = Continuum()
    try:
        sdk.ensure_schema()
        return work(sdk.coding)
    finally:
        sdk.close()


def main(argv: list[str]) -> int:
    """``continuum hook APP [PAYLOAD_JSON]`` (or ``--drain``). Exits 0, except when it
    refuses a tool call for an app whose JSON refusal is unknown (exit 2)."""
    if os.environ.get("CONTINUUM_HOOKS", "").lower() in ("off", "0", "false"):
        return 0
    if argv[:1] == ["--drain"]:
        try:
            _with_sdk(drain)
        except Exception as exc:
            _log(f"drain: {_masked(exc)}")
        return 0
    app = (argv[0] if argv else "generic")[:40]
    for stream in (sys.stdout, sys.stderr):    # also when called without the entry point
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8" \
                and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        payload = parse_payload(argv[1] if len(argv) > 1 else None, sys.stdin)
        out, entry = fast(app, payload)
        if _event(payload) == "sessionstart":
            def start(coding: Any) -> str:
                drain(coding, deadline=time.monotonic() + SESSION_START_DRAIN_SECONDS)
                return handle(app, payload, coding)
            brief = _with_sdk(start)
            out = context_output(app, brief) if brief else ""
        elif entry is not None:
            spool_mod.write(entry)
            if os.environ.get("CONTINUUM_HOOKS_SYNC"):
                _with_sdk(drain)
            else:
                spool_mod.start_drainer()
        if out:
            sys.stdout.write(out)
        denied = payload.get("continuum_denied")
        if denied and family(app) == "generic":
            # an app whose JSON we do not know: exit code 2 with the reason on stderr is
            # the refusal every hook-capable app understands
            sys.stderr.write(str(denied))
            return 2
    except Exception as exc:  # a hook never breaks the agent
        _log(f"hook {app}: {_masked(exc)}")
        if os.environ.get("CONTINUUM_HOOKS_DEBUG"):
            _log(traceback.format_exc())
    return 0


def _masked(exc: BaseException) -> str:
    try:
        from continuum.core.redaction import Redactor

        return Redactor.from_environment().text(f"{type(exc).__name__}: {exc}")[:500]
    except Exception:
        return type(exc).__name__


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
