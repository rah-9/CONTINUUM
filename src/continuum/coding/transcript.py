# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Extract the conversation from a coding agent's chat transcript, without an LLM.

Every coding agent stores its chats differently, and the formats change between
versions. So this parser does not depend on one app's schema. It recognises the
*structure* that all of them share, wherever it appears in a record:

* ``{"role": "user" | "assistant" | "model" | ..., "content": <text or text parts>}``
  (Claude Code, Codex response items, OpenAI/Anthropic-style chat exports)
* ``{"role": ..., "parts": [{"text": ...}]}`` (Gemini)
* ``{"type": "user_message" | "agent_message", "message": "..."}`` (Codex events)

Text parts are ``str`` items or objects of type ``text``/``input_text``/``output_text``.
Tool results, reasoning and images are not conversation and are skipped. Tool calls are
kept as one-line *actions* ("edit: src/app.py", "run: pytest -q", with a ``failed`` flag
from the call's result), so the next model knows what the previous one did, not only what
it said: Claude Code ``tool_use`` parts, Codex ``function_call``/``local_shell_call``/
``custom_tool_call`` items, Gemini ``functionCall`` parts.
Text that harnesses inject into the user's turn (system reminders, environment
context, instruction files) is removed, so only what the person typed remains.

Input is JSON Lines (one record per line) or one JSON document. Unparseable lines are
skipped, never fatal: a newer format degrades to fewer messages, not to a crash.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

USER_ROLES = frozenset({"user", "human"})
AGENT_ROLES = frozenset({"assistant", "model", "agent", "ai"})
TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
EVENT_TYPES = {"user_message": "user", "agent_message": "agent"}

MAX_DEPTH = 8
MAX_LINE_BYTES = 8 << 20       # a single record larger than this is not a chat message
MAX_READ_BYTES = 64 << 20      # read at most the newest 64 MiB of a transcript

# Blocks a harness inserts into the user's turn. Removed wherever they appear.
_INJECTED_TAGS = ("system-reminder", "environment_context", "user_instructions",
                  "user-prompt-submit-hook", "command-message", "command-name",
                  "command-args", "local-command-stdout", "local-command-stderr",
                  "local-command-caveat", "ide_selection", "ide_opened_file",
                  "ide_diagnostics", "INSTRUCTIONS", "turn_aborted", "session-start-hook")
_INJECTED_BLOCK = re.compile(
    r"<(" + "|".join(re.escape(t) for t in _INJECTED_TAGS) + r")(?:\s[^>]*)?>.*?</\1>",
    re.S)
# Whole messages that are harness text, not the person's words.
_INJECTED_PREFIXES = ("Caveat: The messages below were generated",
                      "# AGENTS.md instructions", "[Request interrupted by user",
                      "This session is being continued from a previous conversation",
                      "<environment_context>", "<user_instructions>")


Role = Literal["user", "agent", "action"]
ACTION_ITEM_TYPES = frozenset({"function_call", "local_shell_call", "custom_tool_call"})
ACTION_CHARS = 300
# Bookkeeping tools that are not work on the code (and Continuum's own tools).
_IGNORED_TOOLS = frozenset({"todowrite", "todoread", "exitplanmode", "enterplanmode",
                            "askuserquestion", "update_plan", "toolsearch"})
_EDIT_TOOLS = frozenset({"edit", "multiedit", "write", "notebookedit", "str_replace_editor",
                         "str_replace_based_edit_tool", "create_file", "write_file",
                         "replace", "edit_file", "apply_diff"})
_READ_TOOLS = frozenset({"read", "read_file", "view", "open_file", "read_many_files"})
_SEARCH_TOOLS = frozenset({"grep", "glob", "search", "search_file_content", "codebase_search",
                           "file_search", "list_dir", "ls", "find"})
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)
_EXIT_CODE = re.compile(r"(?:Exit code:?|exit_code\W+)\s*(-?\d+)")


@dataclass(frozen=True)
class Message:
    role: Role
    text: str
    # The transcript's own id for this message (Claude Code ``uuid``, Codex ``timestamp``),
    # when it has one: the exact identity used to re-import without duplicates or losses.
    ident: str | None = None
    # When the message was written, if the transcript says (ISO-8601): restores the true
    # order of messages recovered after a missed hook.
    at: str | None = None
    # actions only: the app recorded the call as failed (an error result or exit code)
    failed: bool = False
    call_id: str | None = None


def _one_line(text: str, limit: int = ACTION_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _command_line(command: str) -> str:
    """A command's first line: a heredoc's body is the agent's code, not the command."""
    lines = [ln for ln in command.strip().splitlines() if ln.strip()]
    head = _one_line(lines[0], 200) if lines else ""
    return head + (f" … (+{len(lines) - 1} lines)" if len(lines) > 1 else "")


def _command(value: Any) -> str | None:
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        if len(value) == 3 and value[0].rsplit("/", 1)[-1] in ("bash", "sh", "zsh") \
                and value[1] in ("-c", "-lc"):
            return str(value[2])                     # Codex wraps commands in `bash -lc`
        return shlex.join(value)
    return str(value) if isinstance(value, str) and value.strip() else None


def describe_action(name: str, args: Any) -> str | None:
    """One line saying what a tool call did, or None for bookkeeping calls."""
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except ValueError:
            parsed = {"input": args}
        args = parsed if isinstance(parsed, dict) else {"input": args}
    if not isinstance(args, dict):
        args = {}
    low = name.lower()
    if low in _IGNORED_TOOLS or "continuum" in low or low.startswith("coding_"):
        return None
    path = next((args[k] for k in ("file_path", "path", "notebook_path", "filePath",
                                   "target_file", "absolute_path") if isinstance(args.get(k), str)),
                None)
    patch = args.get("input") if isinstance(args.get("input"), str) else args.get("patch")
    if isinstance(patch, str) and "*** Begin Patch" in patch:
        files = _PATCH_FILE.findall(patch)
        return "edit: " + _one_line(", ".join(f.strip() for f in files) or "(patch)")
    command = _command(args.get("command", args.get("cmd")))
    if command is not None and low not in _SEARCH_TOOLS:
        return "run: " + _command_line(command)
    if low in _EDIT_TOOLS and path:
        return "edit: " + _one_line(path)
    if low in _READ_TOOLS and path:
        return "read: " + _one_line(path)
    if low in _SEARCH_TOOLS:
        what = next((args[k] for k in ("pattern", "query", "glob", "path")
                     if isinstance(args.get(k), str)), "")
        return "search: " + _one_line(what or name)
    if isinstance(args.get("url"), str):
        return "fetch: " + _one_line(args["url"])
    if low in ("task", "agent") and isinstance(args.get("description"), str):
        return "delegate: " + _one_line(args["description"])
    return "tool: " + _one_line(name + (f" {path}" if path else ""))


def _action_of(node: dict[str, Any]) -> Message | None:
    """A tool call in any of the shapes the supported apps write."""
    kind = node.get("type")
    if kind == "tool_use" and isinstance(node.get("name"), str):
        text = describe_action(node["name"], node.get("input"))
        call = node.get("id")
    elif kind == "local_shell_call":
        action = node.get("action")
        command = _command(action.get("command")) if isinstance(action, dict) else None
        text = None if command is None else "run: " + _command_line(command)
        call = node.get("call_id")
    elif kind in ("function_call", "custom_tool_call") and isinstance(node.get("name"), str):
        text = describe_action(node["name"], node.get("arguments", node.get("input")))
        call = node.get("call_id")
    elif isinstance(node.get("functionCall"), dict):     # Gemini
        fc = node["functionCall"]
        text = describe_action(str(fc.get("name", "")), fc.get("args"))
        call = fc.get("id")
    else:
        return None
    if text is None:
        return None
    return Message("action", text, call_id=call if isinstance(call, str) else None)


def _failed_calls(records: list[Any]) -> set[str]:
    """Ids of tool calls whose recorded result is an error or a non-zero exit code."""
    failed: set[str] = set()

    def visit(node: Any, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        if isinstance(node, list):
            for item in node:
                visit(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if kind == "tool_result" and isinstance(node.get("tool_use_id"), str):
            if node.get("is_error") is True:
                failed.add(node["tool_use_id"])
            return
        if kind in ("function_call_output", "custom_tool_call_output") \
                and isinstance(node.get("call_id"), str):
            output = node.get("output")
            text = output if isinstance(output, str) else json.dumps(output)
            code = _EXIT_CODE.search(text[:2000])
            if code is not None and int(code.group(1)) != 0:
                failed.add(node["call_id"])
            return
        for value in node.values():
            if isinstance(value, dict | list):
                visit(value, depth + 1)

    visit(records, 0)
    return failed


def clean_user_text(text: str) -> str:
    """The person's own words: harness-injected blocks and messages removed."""
    text = _INJECTED_BLOCK.sub("", text).strip()
    if any(text.startswith(p) for p in _INJECTED_PREFIXES):
        return ""
    return text


def _text_of(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str) and (
                part.get("type") in TEXT_PART_TYPES or "type" not in part):
            texts.append(part["text"])
    return "\n".join(texts) if texts else None


def _role(value: Any) -> Literal["user", "agent"] | None:
    if not isinstance(value, str):
        return None
    value = value.lower()
    return "user" if value in USER_ROLES else "agent" if value in AGENT_ROLES else None


def _walk(node: Any, depth: int = 0) -> Iterator[Message]:
    if depth > MAX_DEPTH:
        return
    if isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1)
        return
    if not isinstance(node, dict):
        return
    # harness-internal records: meta lines, sub-agent (sidechain) turns, compaction
    # summaries written as a "user" message
    if node.get("isMeta") is True or node.get("isSidechain") is True \
            or node.get("isCompactSummary") is True:
        return
    role = _role(node.get("role"))
    if role is not None and ("content" in node or "parts" in node):
        body = node.get("content", node.get("parts"))
        text = _text_of(body)
        if text is not None:
            yield Message(role, text)
        if role == "agent" and isinstance(body, list):
            for part in body:
                action = _action_of(part) if isinstance(part, dict) else None
                if action is not None:
                    yield action
        return
    if node.get("type") in ACTION_ITEM_TYPES:
        action = _action_of(node)
        if action is not None:
            yield action
        return
    event = node.get("type")
    if isinstance(event, str) and event in EVENT_TYPES and isinstance(node.get("message"), str):
        yield Message("user" if EVENT_TYPES[event] == "user" else "agent", node["message"])
        return
    for value in node.values():
        if isinstance(value, dict | list):
            yield from _walk(value, depth + 1)


def extract(records: Iterable[Any]) -> list[Message]:
    """Conversation messages, in order, from parsed records. User text is cleaned of
    harness injections; empty messages and consecutive duplicates are dropped (some
    formats log the same message twice, e.g. as an item and as an event)."""
    out: list[Message] = []
    records = list(records)
    ids = [_record_id(r) for r in records]
    # When a transcript identifies its messages, records without an id are copies (Claude
    # Code writes request/cache blobs of messages it already logged), not new messages.
    identified = any(i is not None for i in ids) and len(records) > 1
    failed = _failed_calls(records)
    for record, rid in zip(records, ids, strict=True):
        if identified and rid is None:
            continue
        stamp = record.get("timestamp") if isinstance(record, dict) else None
        stamp = stamp if isinstance(stamp, str) else None
        for k, msg in enumerate(_walk(record)):
            text = clean_user_text(msg.text) if msg.role == "user" else msg.text.strip()
            if not text:
                continue
            # the same message logged twice (Codex: item and event). Actions repeat for
            # real (running the tests twice), so they are never collapsed.
            if msg.role != "action" and out and out[-1].role == msg.role \
                    and out[-1].text == text:
                continue
            out.append(Message(msg.role, text, None if rid is None else f"{rid}/{k}", stamp,
                               failed=msg.call_id is not None and msg.call_id in failed,
                               call_id=msg.call_id))
    return out


def _record_id(record: Any) -> str | None:
    if isinstance(record, dict):
        for key in ("uuid", "id", "timestamp"):
            value = record.get(key)
            if isinstance(value, str | int) and str(value):
                return f"{key}:{value}"
    return None


def _records(data: bytes) -> list[Any]:
    records: list[Any] = []
    for line in data.splitlines():
        line = line.strip()
        if not line or len(line) > MAX_LINE_BYTES:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    if records:
        return records
    try:  # not JSON Lines: one JSON document (e.g. a Gemini chat checkpoint)
        return [json.loads(data)]
    except ValueError:
        return []


def read_transcript(path: str | Path, *, tail_bytes: int = MAX_READ_BYTES) -> list[Message]:
    """Messages of a transcript file. Very large files are read from their newest part
    (older turns were captured by earlier hook calls)."""
    file = Path(path)
    size = file.stat().st_size
    limit = min(tail_bytes, MAX_READ_BYTES)
    with file.open("rb") as fh:
        if size > limit:
            fh.seek(size - limit)
            fh.readline()  # drop the partial first line
        data = fh.read()
    return extract(_records(data))
