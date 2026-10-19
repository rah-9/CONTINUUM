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

A hook must never break or slow down the agent: every failure is logged to
``$CONTINUUM_HOME/hooks.log`` and the command still exits 0. ``CONTINUUM_HOOKS=off``
disables it. No model is called.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from continuum.coding.transcript import Message, read_transcript

CHAT_KEYS = ("session_id", "sessionId", "thread-id", "thread_id", "conversation_id",
             "conversationId")
USER_KEYS = ("input-messages", "input_messages")
AGENT_KEYS = ("last-assistant-message", "last_assistant_message")
TRANSCRIPT_KEYS = ("transcript_path", "transcriptPath")
CWD_KEYS = ("cwd", "workspace", "workspace_root", "project_dir")
HOOK_DB_TIMEOUT = "3"  # seconds; a hook waits at most this long for the database
# At the end of a turn only the newest part of the transcript is read (the prompt itself
# was captured when it was submitted), so a long chat does not slow every turn down. At
# compaction and session end the whole transcript is re-read to catch up anything missed.
TURN_TAIL_BYTES = 2 << 20
TURN_EVENTS = frozenset({"stop", "subagentstop", "agent-turn-complete", "aftermodel"})

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


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _event(payload: dict[str, Any]) -> str:
    return str(payload.get("hook_event_name") or payload.get("hookEventName")
               or payload.get("type") or "").lower()


def messages_from(payload: dict[str, Any]) -> tuple[list[Message], list[Message]]:
    """What a hook payload carries: (the chat transcript in order, this turn's messages).

    The transcript is re-imported idempotently (occurrences are counted within it). This
    turn's fields (``prompt``, ``input-messages``, ``last-assistant-message``) are new
    messages, except when the transcript already ends with them."""
    history: list[Message] = []
    path = _first_str(payload, TRANSCRIPT_KEYS)
    if path and Path(path).expanduser().is_file():
        tail = TURN_TAIL_BYTES if _event(payload) in TURN_EVENTS else None
        history = read_transcript(Path(path).expanduser(), **(
            {"tail_bytes": tail} if tail else {}))
    turn: list[Message] = []
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        turn.append(Message("user", prompt))
    for key in USER_KEYS:
        items = payload.get(key)
        if isinstance(items, list):
            turn += [Message("user", m) for m in items if isinstance(m, str) and m.strip()]
    agent = _first_str(payload, AGENT_KEYS)
    if agent:
        turn.append(Message("agent", agent))
    recent = history[-3:]
    turn = [m for m in turn if not any(
        r.role == m.role and r.text.strip() == m.text.strip() for r in recent)]
    return history, turn


def handle(app: str, payload: dict[str, Any], coding: Any) -> str:
    """Apply one hook call; returns what to print (context for the app, or nothing)."""
    from continuum.core.errors import NotFoundError

    cwd = _first_str(payload, CWD_KEYS) or os.getcwd()
    event = _event(payload)
    if event == "sessionstart":
        try:
            brief = coding.brief(coding.follow(cwd, agent=f"capture:{app}"))
        except NotFoundError:
            return ""
        return CONTEXT_HEADER + str(brief["markdown"])
    history, turn = messages_from(payload)
    chat = _first_str(payload, CHAT_KEYS)
    if history:
        coding.capture(cwd, history, source=app, chat_id=chat, history=True)
    if turn:
        coding.capture(cwd, turn, source=app, chat_id=chat)
    return ""


def _log(line: str) -> None:
    try:
        home = Path(os.environ.get("CONTINUUM_HOME") or Path.home() / ".continuum")
        home.mkdir(parents=True, exist_ok=True)
        with (home / "hooks.log").open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + line.rstrip() + "\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    """``continuum hook APP [PAYLOAD_JSON]``. Always exits 0."""
    if os.environ.get("CONTINUUM_HOOKS", "").lower() in ("off", "0", "false"):
        return 0
    app = (argv[0] if argv else "generic")[:40]
    try:
        payload = parse_payload(argv[1] if len(argv) > 1 else None, sys.stdin)
        os.environ.setdefault("CONTINUUM_DB_TIMEOUT_SECONDS", HOOK_DB_TIMEOUT)
        from continuum.interfaces.sdk import Continuum

        sdk = Continuum()
        try:
            sdk.ensure_schema()
            out = handle(app, payload, sdk.coding)
        finally:
            sdk.close()
        if out:
            sys.stdout.write(out)
    except Exception as exc:  # a hook never breaks the agent
        try:
            from continuum.core.redaction import Redactor

            message = Redactor.from_environment().text(f"{type(exc).__name__}: {exc}")
        except Exception:
            message = type(exc).__name__
        _log(f"hook {app}: {message[:500]}")
        if os.environ.get("CONTINUUM_HOOKS_DEBUG"):
            _log(traceback.format_exc())
    return 0
