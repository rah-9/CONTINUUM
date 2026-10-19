# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""What an agent did, in one vendor-neutral shape (D-042).

Every coding app reports tool calls differently. This module turns each app's hook payload
into the same small records ("envelopes"), so the journal holds one kind of tool call
whatever produced it:

* a **start** (the tool, its input, which agent or subagent called it, what it can affect)
* a **finish** (status, exit code, duration, the full output, error)
* **compaction**, **turn end**, **prompt**, **session start/end**, **subagent stop**

Field maps (documented hook payloads, checked against each app's reference):

* Claude Code and Codex: ``PreToolUse`` / ``PostToolUse`` / ``PostToolUseFailure`` with
  ``tool_name``, ``tool_input``, ``tool_use_id``, ``tool_response``, ``error``,
  ``is_interrupt``, ``duration_ms``, ``agent_id``, ``agent_type``; MCP tools are named
  ``mcp__<server>__<tool>``.
* Gemini CLI: ``BeforeTool`` / ``AfterTool`` with ``tool_response`` holding ``llmContent``,
  ``returnDisplay``, ``error``; MCP tools are ``mcp_<server>_<tool>``.
* Cursor: ``beforeShellExecution`` / ``afterShellExecution`` (``command``, ``output``),
  ``beforeMCPExecution`` / ``afterMCPExecution`` (``tool_name``, ``tool_input``,
  ``result_json``), ``afterFileEdit`` (``file_path``, ``edits``).

Each tool is also classified by what it can affect (``effect``):

* ``read``: reads files, searches, fetches; no lasting effect
* ``write``: changes files in the repository (observed by snapshots, D-042)
* ``external``: can change something outside the repository (a push, a deploy, an MCP
  write, a network request that sends data). A lost result of such a call is an unknown
  effect: new external calls are blocked until a person resolves it (AGENTS.md inv. 8)
* ``delegate``: a subagent; its own calls are recorded under its ``agent_id``

Classification is a fixed, documented table plus a command scanner, never a model; an
unknown tool is ``external`` (the conservative choice). Standard library only: hooks
run before every tool call and must start fast.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shlex
from typing import Any

ENVELOPE_VERSION = 1
INPUT_MAX_CHARS = 16_000      # inline tool input (the full input goes to a blob)

READ_TOOLS = frozenset({
    # Claude Code
    "read", "grep", "glob", "ls", "notebookread", "webfetch", "websearch", "todowrite",
    "todoread", "toolsearch", "exitplanmode", "enterplanmode", "askuserquestion",
    "skill", "listmcpresourcestool", "readmcpresourcetool", "bashoutput", "killshell",
    "taskoutput", "monitor",
    # Codex
    "read_file", "list_dir", "grep_files", "view_image", "update_plan", "web_search",
    # Gemini CLI
    "list_directory", "search_file_content", "read_many_files", "google_web_search",
    "web_fetch", "save_memory",
    # Cursor and others
    "codebase_search", "file_search", "read_lints", "fetch", "view",
})
WRITE_TOOLS = frozenset({
    "edit", "multiedit", "write", "notebookedit", "apply_patch", "write_file", "replace",
    "edit_file", "str_replace_editor", "str_replace_based_edit_tool", "create_file",
    "delete_file", "search_replace", "afterfileedit",
})
SHELL_TOOLS = frozenset({"bash", "shell", "local_shell", "exec_command", "run_shell_command",
                         "container.exec", "unified_exec", "run_terminal_cmd", "terminal",
                         "powershell"})
DELEGATE_TOOLS = frozenset({"task", "agent", "subagent", "spawn_agent"})

# MCP tools: a name that only reads (get_issue, list_files, search_code, ...) is a read;
# anything else may write somewhere outside the repository.
_MCP_READ = re.compile(
    r"^(?:get|list|read|search|fetch|query|describe|show|find|view|lookup|inspect|status|"
    r"check|count|browse|download|retrieve|whoami|me)(?:_|$|[A-Z])", re.I)

# Shell commands that act outside the repository. Matched on each simple command of a
# command line (after `&&`, `;`, `|`, ...). Conservative: when in doubt, external.
_EXTERNAL_COMMANDS = [
    re.compile(p) for p in (
        r"^git\s+(?:push|send-email|svn\s+dcommit)\b",
        r"^gh\s+(?:pr|issue|release|repo|workflow|run|api|secret|variable|gist|label)\s+"
        r"(?!view|list|status|diff|checks|watch)\S+",
        r"^glab\s+\S+\s+(?:create|merge|close|delete|update)\b",
        r"^(?:npm|pnpm|yarn)\s+(?:publish|unpublish|deprecate|dist-tag)\b",
        r"^(?:twine|flit|poetry|hatch|uv)\s+(?:upload|publish)\b",
        r"^cargo\s+(?:publish|yank|owner)\b",
        r"^gem\s+(?:push|yank)\b",
        r"^docker\s+(?:push|login)\b|^docker\s+(?:compose\s+)?(?:run|up|exec)\b.*\b(?:prod|production)\b",
        r"^(?:kubectl|oc)\s+(?:apply|create|delete|patch|replace|scale|rollout|set|edit|"
        r"label|annotate|exec|cordon|drain)\b",
        r"^helm\s+(?:install|upgrade|uninstall|rollback|delete)\b",
        r"^(?:terraform|tofu|pulumi)\s+(?:apply|destroy|import|up|refresh)\b",
        r"^(?:aws|gcloud|az|doctl|flyctl|fly|heroku|vercel|netlify|firebase|wrangler|"
        r"railway|render|supabase)\s+(?!.*\b(?:describe|list|ls|get|show|logs|status|"
        r"whoami|--help|help)\b)",
        r"^(?:ssh|scp|sftp|rsync)\b.*\S+@?\S*:",
        r"^(?:ssh)\s",
        r"^(?:curl|wget|http|https|xh)\b.*(?:\s-X\s*(?:POST|PUT|PATCH|DELETE)|\s--request\s+"
        r"(?:POST|PUT|PATCH|DELETE)|\s-d\b|\s--data|\s-F\b|\s--form|\s-T\b|\s--upload-file|"
        r"\s(?:POST|PUT|PATCH|DELETE)\s)",
        r"^(?:psql|mysql|mariadb|sqlcmd|mongosh|mongo|redis-cli|sqlite3)\b",
        r"^(?:ansible|ansible-playbook|salt|chef-client|puppet)\b",
        r"^(?:sendmail|mail|mailx)\b",
        r"^(?:stripe|twilio|slack)\b",
        r"^(?:make|just|task|npm\s+run|pnpm\s+run|yarn)\s+\S*(?:deploy|release|publish)",
        r"\b(?:deploy|release)\.(?:sh|py|js|ts)\b",
    )
]
_SPLIT = re.compile(r"\|\||&&|;|\||\n")


def _norm(name: str) -> str:
    return name.strip().lower()


def command_of(tool_input: Any) -> str | None:
    """The command line of a shell tool call, from any app's input shape."""
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except ValueError:
            return tool_input or None
    if not isinstance(tool_input, dict):
        return None
    value = tool_input.get("command", tool_input.get("cmd"))
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        if len(value) == 3 and value[0].rsplit("/", 1)[-1] in ("bash", "sh", "zsh") \
                and value[1] in ("-c", "-lc"):
            return str(value[2])
        return shlex.join(value)
    return str(value) if isinstance(value, str) and value.strip() else None


def command_effect(command: str) -> str:
    """``external`` when any simple command in the line acts outside the repository."""
    for part in _SPLIT.split(command):
        part = part.strip()
        while part.startswith(("sudo ", "env ", "time ", "exec ", "nohup ", "(")):
            part = part.split(" ", 1)[1].lstrip("( ") if " " in part else ""
        part = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+", "", part)   # VAR=x cmd
        if any(p.search(part) for p in _EXTERNAL_COMMANDS):
            return "external"
    return "write"


def classify(tool: str, tool_input: Any = None) -> dict[str, Any]:
    """``{"kind", "server", "effect"}`` for a tool name (any app's naming)."""
    name = _norm(tool)
    server = None
    if name.startswith("mcp__"):                         # Claude Code, Codex
        parts = tool.split("__", 2)
        server = parts[1] if len(parts) > 1 else None
        short = parts[2] if len(parts) > 2 else ""
        if server and server.startswith("plugin_"):
            server = server[len("plugin_"):]
        effect = "read" if _MCP_READ.match(short) else "external"
        return {"kind": "mcp", "server": server, "effect": effect}
    if name.startswith("mcp_"):                          # Gemini CLI: mcp_<server>_<tool>
        rest = tool[4:]
        server, _, short = rest.partition("_")
        effect = "read" if _MCP_READ.match(short) else "external"
        return {"kind": "mcp", "server": server or None, "effect": effect}
    if name in DELEGATE_TOOLS:
        return {"kind": "subagent", "server": None, "effect": "delegate"}
    if name == "skill":
        return {"kind": "skill", "server": None, "effect": "read"}
    if name in SHELL_TOOLS or name in ("beforeshellexecution", "aftershellexecution"):
        command = command_of(tool_input)
        effect = command_effect(command) if command else "write"
        return {"kind": "shell", "server": None, "effect": effect}
    if name in WRITE_TOOLS:
        return {"kind": "builtin", "server": None, "effect": "write"}
    if name in READ_TOOLS:
        return {"kind": "builtin", "server": None, "effect": "read"}
    return {"kind": "builtin", "server": None, "effect": "external"}


def skill_of(tool: str, tool_input: Any) -> str | None:
    if _norm(tool) == "skill" and isinstance(tool_input, dict):
        value = tool_input.get("skill", tool_input.get("command", tool_input.get("name")))
        return str(value)[:120] if value else None
    return None


# ---- outputs -----------------------------------------------------------------------

def text_of_output(value: Any) -> str:
    """A tool's output as text, from any app's result shape (strings, MCP content parts,
    Claude Code ``tool_response`` objects, Gemini ``llmContent``)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts = []
        for part in value:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif isinstance(part, str):
                texts.append(part)
            else:
                texts.append(json.dumps(part, ensure_ascii=False, default=str))
        return "\n".join(texts)
    if isinstance(value, dict):
        for key in ("stdout", "output", "aggregated_output", "content", "llmContent",
                    "result", "text", "returnDisplay"):
            inner = value.get(key)
            if isinstance(inner, str | list) and inner:
                head = text_of_output(inner)
                stderr = value.get("stderr")
                if key == "stdout" and isinstance(stderr, str) and stderr.strip():
                    head += "\n[stderr]\n" + stderr
                return head
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return str(value)


def exit_code_of(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("exit_code", "exitCode", "returnCode", "return_code", "code"):
            code = value.get(key)
            if isinstance(code, int) and not isinstance(code, bool):
                return code
        meta = value.get("metadata")
        if isinstance(meta, dict):
            return exit_code_of(meta)
    if isinstance(value, str):
        m = re.search(r"(?:Exit code:?|exit_code\W+)\s*(-?\d+)", value[:2000])
        if m:
            return int(m.group(1))
    return None


_DENIED = re.compile(r"doesn't want to proceed|tool use was rejected|permission (?:to use "
                     r"\S+ )?(?:was )?denied|blocked by (?:a )?hook|denied by", re.I)
_INTERRUPTED = re.compile(r"\[Request interrupted|interrupted by user|was interrupted",
                          re.I)


def status_of(*, is_error: bool, text: str, interrupt: bool = False,
              exit_code: int | None = None) -> str:
    """ok | error | denied (never ran) | interrupted (may have run partly)."""
    if interrupt or _INTERRUPTED.search(text[:500]):
        return "interrupted"
    if is_error and _DENIED.search(text[:500]):
        return "denied"
    if is_error or (exit_code is not None and exit_code != 0):
        return "error"
    return "ok"


# ---- hook payloads -> envelopes -------------------------------------------------------

def _str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def call_key(app_call_id: str | None, chat: str | None, tool: str, tool_input: Any) -> str:
    """The identity of one tool call for pairing start and finish. The app's call id when
    it has one (correlation only; never operation identity, AGENTS.md inv. 4); otherwise
    the chat, tool and input (a later identical call in the same chat is paired in order
    by the journal)."""
    if app_call_id:
        return "call:" + hashlib.sha256(app_call_id.encode()).hexdigest()[:32]
    body = json.dumps([chat or "", tool, tool_input], sort_keys=True, default=str)
    return "anon:" + hashlib.sha256(body.encode()).hexdigest()[:32]


def from_hook(app: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The envelope for one hook call, or None when the event carries nothing to keep
    (prompts, turn ends and session events are handled by the chat capture as before)."""
    event = str(payload.get("hook_event_name") or payload.get("hookEventName")
                or payload.get("type") or "")
    low = event.lower()
    chat = _str(payload, "session_id", "sessionId", "conversation_id", "thread-id",
                "thread_id")
    base: dict[str, Any] = {"v": ENVELOPE_VERSION, "app": app, "event": event, "chat": chat,
                            "agent_id": _str(payload, "agent_id"),
                            "agent_type": _str(payload, "agent_type")}
    tool: str | None = None
    tool_input: Any = None
    if low in ("pretooluse", "posttooluse", "posttoolusefailure", "beforetool", "aftertool"):
        tool = _str(payload, "tool_name", "toolName") or "unknown"
        tool_input = payload.get("tool_input", payload.get("toolInput"))
    elif low in ("beforeshellexecution", "aftershellexecution"):
        tool = "Shell"
        tool_input = {"command": payload.get("command")}
    elif low in ("beforemcpexecution", "aftermcpexecution"):
        server = _str(payload, "server", "server_name", "mcp_server") or "mcp"
        tool = f"mcp__{server}__{_str(payload, 'tool_name') or 'tool'}"
        tool_input = payload.get("tool_input")
        if isinstance(tool_input, str):
            with contextlib.suppress(ValueError):
                tool_input = json.loads(tool_input)
    elif low == "afterfileedit":
        tool = "Edit"
        tool_input = {"file_path": payload.get("file_path"),
                      "edits": payload.get("edits")}
    elif low in ("precompact", "postcompact"):
        summary = payload.get("compact_summary")
        return {**base, "phase": "compact", "trigger": _str(payload, "trigger"),
                "when": "after" if low == "postcompact" else "before",
                "summary": summary if isinstance(summary, str) else None}
    elif low == "subagentstop":
        return {**base, "phase": "subagent_stop",
                "report": _str(payload, "last_assistant_message"),
                "transcript": _str(payload, "agent_transcript_path")}
    else:
        return None
    info = classify(tool, tool_input)
    app_call_id = _str(payload, "tool_use_id", "toolUseId", "call_id", "tool_call_id")
    if low == "afterfileedit":
        app_call_id = None
    key = call_key(app_call_id, chat, tool, tool_input if app_call_id is None else None)
    common = {**base, "call_key": key, "call_id": app_call_id, "tool": tool,
              "kind": info["kind"], "server": info["server"], "effect": info["effect"],
              "skill": skill_of(tool, tool_input)}
    if low in ("pretooluse", "beforetool", "beforeshellexecution", "beforemcpexecution"):
        return {**common, "phase": "start", "input": tool_input}
    # a finish (Cursor's afterFileEdit is a finished edit with no separate start)
    response = payload.get("tool_response", payload.get("toolResponse"))
    if low == "aftershellexecution":
        response = {"output": payload.get("output"), "exit_code": payload.get("exit_code")}
    elif low == "aftermcpexecution":
        response = payload.get("result_json", payload.get("result"))
    if info["effect"] == "write" and isinstance(response, dict):
        # an edit's response repeats the whole file; the snapshot diff is the impact
        response = {k: v for k, v in response.items()
                    if k not in ("originalFile", "content", "file_content", "oldString",
                                 "newString", "old_string", "new_string")}
    error = payload.get("error")
    if isinstance(response, dict) and isinstance(response.get("error"), str | dict):
        error = error or response.get("error")
    error_text = error if isinstance(error, str) else (
        json.dumps(error, default=str) if error else "")
    output = text_of_output(response)
    exit_code = exit_code_of(response)
    failed = low == "posttoolusefailure" or bool(error_text)
    status = status_of(is_error=failed, text=error_text or output,
                       interrupt=payload.get("is_interrupt") is True, exit_code=exit_code)
    if isinstance(response, dict) and response.get("interrupted") is True:
        status = "interrupted"
    duration = payload.get("duration_ms")
    finish = {**common, "phase": "finish", "status": status, "exit_code": exit_code,
              "duration_ms": duration if isinstance(duration, int) else None,
              "output": output, "error": error_text or None}
    if low == "afterfileedit":
        return {**finish, "phase": "start_finish", "input": tool_input}
    return finish
