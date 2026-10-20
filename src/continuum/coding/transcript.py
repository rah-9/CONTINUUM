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
Tool calls, tool results, reasoning and images are not conversation: ``activity()`` reads
the tool calls with their results, subagent calls and compaction from the same records
(D-042), so a transcript catches up whatever a live hook missed.
Text that harnesses inject into the user's turn (system reminders, environment
context, instruction files) is removed, so only what the person typed remains.

Input is JSON Lines (one record per line) or one JSON document. Unparseable lines are
skipped, never fatal: a newer format degrades to fewer messages, not to a crash.
"""

from __future__ import annotations

import contextlib
import json
import re
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
                  "ide_diagnostics", "INSTRUCTIONS", "turn_aborted", "session-start-hook",
                  "task-notification", "bash-input", "bash-stdout", "bash-stderr")
_INJECTED_BLOCK = re.compile(
    r"<(" + "|".join(re.escape(t) for t in _INJECTED_TAGS) + r")(?:\s[^>]*)?>.*?</\1>",
    re.S)
# Whole messages that are harness text, not the person's words.
_INJECTED_PREFIXES = ("Caveat: The messages below were generated",
                      "# AGENTS.md instructions", "[Request interrupted by user",
                      "This session is being continued from a previous conversation",
                      "<environment_context>", "<user_instructions>")


Role = Literal["user", "agent"]


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
    # Images and documents in the message (``media.find`` parts: media type + base64).
    media: tuple[dict[str, str], ...] = ()
    # On a branch the person abandoned (rewound, or edited and resent): kept in the
    # journal and recall, never shown as a current requirement (D-046).
    abandoned: bool = False


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


def _tool_result(content: Any) -> bool:
    """A "user" record that only carries tool results (their images belong to the call)."""
    return isinstance(content, list) and any(
        isinstance(p, dict) and p.get("type") in ("tool_result", "function_call_output")
        for p in content)


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
        from continuum.coding import media

        found = tuple(media.find(body)) if role == "user" and not _tool_result(body) else ()
        if text is not None or found:
            yield Message(role, text or "", media=found)
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
    off_branch = _abandoned(records)
    # When a transcript identifies its messages, records without an id are copies (Claude
    # Code writes request/cache blobs of messages it already logged), not new messages.
    identified = any(i is not None for i in ids) and len(records) > 1
    for index, (record, rid) in enumerate(zip(records, ids, strict=True)):
        if identified and rid is None:
            continue
        if _rolled_back(record):
            _roll_back(out, record)
            continue
        stamp = record.get("timestamp") if isinstance(record, dict) else None
        stamp = stamp if isinstance(stamp, str) else None
        for k, msg in enumerate(_walk(record)):
            text = clean_user_text(msg.text) if msg.role == "user" else msg.text.strip()
            if not text and not msg.media:
                continue
            if not text and msg.media and msg.text.strip():
                continue         # only harness text around it (an injected screenshot)
            # the same message logged twice (Codex: item and event)
            if out and out[-1].role == msg.role and out[-1].text == text:
                continue
            out.append(Message(msg.role, text, None if rid is None else f"{rid}/{k}", stamp,
                               msg.media, abandoned=index in off_branch))
    return out


def _parent(record: dict[str, Any]) -> str | None:
    parent = record.get("parentUuid") or record.get("logicalParentUuid")
    return parent if isinstance(parent, str) else None


def _user_prose(record: dict[str, Any]) -> bool:
    message = record.get("message")
    if not isinstance(message, dict) or _role(message.get("role")) != "user":
        return False
    if record.get("isMeta") is True or record.get("isSidechain") is True or \
            record.get("isCompactSummary") is True:
        return False
    content = message.get("content")
    if _tool_result(content):
        return False
    text = _text_of(content)
    return bool((text and clean_user_text(text)) or (from_media(content)))


def from_media(content: Any) -> bool:
    from continuum.coding import media

    return bool(media.find(content))


def _abandoned(records: list[Any]) -> set[int]:
    """Indices of records on a branch the person abandoned (D-046).

    Claude Code links records into a tree (``uuid`` / ``parentUuid``; across compaction,
    ``logicalParentUuid``). Editing a message and sending it again, or rewinding the
    chat and typing something else, adds a new user message under the same parent as
    the old one. The active conversation is the path from the newest record back to the
    root; a user message *beside* that path (same parent) is abandoned, with everything
    under it. Parallel tool calls also fork the tree, but never with a second user
    message, so they are not branches. Without parent ids nothing is abandoned."""
    nodes: dict[str, int] = {}
    children: dict[str, list[int]] = {}
    for i, r in enumerate(records):
        if isinstance(r, dict) and isinstance(r.get("uuid"), str):
            nodes[r["uuid"]] = i
            parent = _parent(r)
            if parent:
                children.setdefault(parent, []).append(i)
    if not children:
        return set()
    leaf = next((i for i in range(len(records) - 1, -1, -1)
                 if isinstance(records[i], dict) and isinstance(records[i].get("uuid"), str)
                 and records[i].get("isSidechain") is not True), None)
    if leaf is None:
        return set()
    chain: set[int] = set()
    step: int | None = leaf
    while step is not None and step not in chain:
        chain.add(step)
        parent = _parent(records[step])
        step = nodes.get(parent) if parent else None
    roots = [sib for c in chain if _user_prose(records[c])
             for sib in children.get(_parent(records[c]) or "", [])
             if sib not in chain and _user_prose(records[sib])]
    out: set[int] = set()
    todo = list(roots)
    while todo:
        n = todo.pop()
        if n in out:
            continue
        out.add(n)
        uuid = records[n].get("uuid")
        todo += children.get(uuid, []) if isinstance(uuid, str) else []
    return out


def _rolled_back(record: Any) -> bool:
    """Codex appends ``thread_rolled_back {num_turns}`` when the person backtracks."""
    if not isinstance(record, dict):
        return False
    payload = _payload(record)
    return payload.get("type") == "thread_rolled_back" and isinstance(
        payload.get("num_turns"), int)


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    inner = record.get("payload")
    return inner if isinstance(inner, dict) else record


def _roll_back(out: list[Message], record: dict[str, Any]) -> None:
    """Mark the last ``num_turns`` user turns (and what followed them) abandoned."""
    turns = int(_payload(record)["num_turns"])
    users = [i for i, m in enumerate(out) if m.role == "user" and not m.abandoned]
    if turns <= 0 or not users:
        return
    start = users[-min(turns, len(users))]
    for i in range(start, len(out)):
        m = out[i]
        out[i] = Message(m.role, m.text, m.ident, m.at, m.media, abandoned=True)


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
    return extract(_read_records(path, tail_bytes))


def _read_records(path: str | Path, tail_bytes: int = MAX_READ_BYTES) -> list[Any]:
    file = Path(path)
    size = file.stat().st_size
    limit = min(tail_bytes, MAX_READ_BYTES)
    with file.open("rb") as fh:
        if size > limit:
            fh.seek(size - limit)
            fh.readline()  # drop the partial first line
        data = fh.read()
    return _records(data)


# ---- tool activity (D-042) -----------------------------------------------------------

def _result_text(content: Any) -> str:
    from continuum.coding.activity import text_of_output

    return text_of_output(content)


def _claude_records(record: dict[str, Any], app: str, chat: str | None,
                    stamp: str | None, results: dict[str, dict[str, Any]]
                    ) -> Iterator[dict[str, Any]]:
    from continuum.coding import activity as act

    message = record.get("message")
    agent_id = record.get("agentId") if record.get("isSidechain") is True else None
    if record.get("isCompactSummary") is True and isinstance(message, dict):
        summary = _text_of(message.get("content"))
        if summary:
            yield {"v": act.ENVELOPE_VERSION, "app": app, "chat": chat, "phase": "compact",
                   "when": "after", "trigger": None, "summary": summary, "at": stamp}
        return
    if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
        raw_meta = record.get("compactMetadata")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        yield {"v": act.ENVELOPE_VERSION, "app": app, "chat": chat, "phase": "compact",
               "when": "before", "trigger": meta.get("trigger"), "summary": None,
               "at": stamp}
        return
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return
    for part in message["content"]:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_use" and isinstance(part.get("name"), str):
            call_id = part.get("id") if isinstance(part.get("id"), str) else None
            yield _start(app, chat, part["name"], part.get("input"), call_id, stamp,
                         agent_id if isinstance(agent_id, str) else None)
        elif part.get("type") == "tool_result" and isinstance(part.get("tool_use_id"), str):
            extra = record.get("toolUseResult")
            results[part["tool_use_id"]] = {
                "content": part.get("content"), "is_error": part.get("is_error") is True,
                "extra": extra if isinstance(extra, dict) else None, "at": stamp}


def _start(app: str, chat: str | None, tool: str, tool_input: Any, call_id: str | None,
           stamp: str | None, agent_id: str | None = None) -> dict[str, Any]:
    from continuum.coding import activity as act

    if isinstance(tool_input, str):
        with contextlib.suppress(ValueError):
            tool_input = json.loads(tool_input)
    info = act.classify(tool, tool_input)
    return {"v": act.ENVELOPE_VERSION, "app": app, "chat": chat, "phase": "start",
            "call_key": act.call_key(call_id, chat, tool, None if call_id else tool_input),
            "call_id": call_id, "tool": tool, "kind": info["kind"], "server": info["server"],
            "effect": info["effect"], "skill": act.skill_of(tool, tool_input),
            "agent_id": agent_id, "input": tool_input, "at": stamp}


def _finish(start: dict[str, Any], *, output: str, is_error: bool, exit_code: int | None,
            interrupt: bool, stamp: str | None, raw: Any = None) -> dict[str, Any]:
    from continuum.coding import activity as act

    status = act.status_of(is_error=is_error, text=output, interrupt=interrupt,
                           exit_code=exit_code)
    return {**{k: start[k] for k in ("v", "app", "chat", "call_key", "call_id", "tool",
                                      "kind", "server", "effect", "skill", "agent_id",
                                      "input")},
            "phase": "finish", "status": status, "exit_code": exit_code,
            "duration_ms": None, "output": output,
            "error": output[:2000] if status in ("error", "denied") else None, "at": stamp,
            "media": _media(raw)}


def _media(raw: Any) -> list[dict[str, str]]:
    from continuum.coding import media

    return media.find(raw) if raw is not None else []


def activity(records: list[Any], *, app: str, chat: str | None) -> list[dict[str, Any]]:
    """Tool calls with their results, subagent calls and compaction, in order, as
    envelopes (``activity.from_hook``'s shape), from Claude Code transcripts (including
    sidechains), Codex rollouts and Gemini chats. The app's call ids pair a call with its
    result, so importing the same transcript twice, or after the live hooks, adds nothing.
    """
    from continuum.coding import activity as act

    starts: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    codex_out: dict[str, dict[str, Any]] = {}
    gemini_pending: list[dict[str, Any]] = []
    # Claude Code also writes uuid-less request/cache copies of records it logged
    uuids = any(isinstance(r, dict) and r.get("uuid") for r in records)
    for record in records:
        if not isinstance(record, dict):
            continue
        if uuids and "message" in record and not record.get("uuid"):
            continue
        stamp = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else None
        if payload is not None:                                       # Codex rollout
            kind = payload.get("type")
            if record.get("type") == "compacted":
                other.append({"v": act.ENVELOPE_VERSION, "app": app, "chat": chat,
                              "phase": "compact", "when": "after", "trigger": None,
                              "summary": payload.get("message"), "at": stamp})
            elif kind in ("function_call", "custom_tool_call") and isinstance(
                    payload.get("name"), str):
                starts.append(_start(app, chat, payload["name"], payload.get(
                    "arguments", payload.get("input")), payload.get("call_id"), stamp))
            elif kind == "local_shell_call":
                action = payload.get("action") if isinstance(payload.get("action"),
                                                             dict) else {}
                starts.append(_start(app, chat, "local_shell",
                                     {"command": action.get("command")},
                                     payload.get("call_id"), stamp))
            elif kind in ("function_call_output", "custom_tool_call_output") and isinstance(
                    payload.get("call_id"), str):
                codex_out.setdefault(payload["call_id"], {})["output"] = payload.get("output")
                codex_out[payload["call_id"]].setdefault("at", stamp)
            elif kind == "exec_command_end" and isinstance(payload.get("call_id"), str):
                entry = codex_out.setdefault(payload["call_id"], {})
                entry["exec"] = payload
                entry["at"] = stamp
            elif kind == "patch_apply_end" and isinstance(payload.get("call_id"), str):
                entry = codex_out.setdefault(payload["call_id"], {})
                entry["patch"] = payload
                entry["at"] = stamp
            continue
        if isinstance(record.get("message"), dict) or record.get("type") == "system":
            if record.get("isMeta") is not True:
                for env in _claude_records(record, app, chat, stamp, results):
                    (starts if env["phase"] == "start" else other).append(env)
            continue
        role = record.get("role")
        parts = record.get("parts")
        if role in ("model", "user") and isinstance(parts, list):         # Gemini chat
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("functionCall"), dict):
                    fc = part["functionCall"]
                    env = _start(app, chat, str(fc.get("name", "tool")), fc.get("args"),
                                 fc.get("id") if isinstance(fc.get("id"), str) else None,
                                 stamp)
                    starts.append(env)
                    gemini_pending.append(env)
                elif isinstance(part.get("functionResponse"), dict):
                    fr = part["functionResponse"]
                    match = next((s for s in gemini_pending if (
                        fr.get("id") and s["call_id"] == fr.get("id")) or (
                        not fr.get("id") and s["tool"] == fr.get("name"))), None)
                    if match is not None:
                        gemini_pending.remove(match)
                        response = fr.get("response")
                        text = _result_text(response)
                        error = isinstance(response, dict) and bool(response.get("error"))
                        other.append(_finish(match, output=text, is_error=error,
                                             exit_code=act.exit_code_of(response),
                                             interrupt=False, stamp=stamp, raw=response))
    found: list[dict[str, Any]] = []
    for env in starts:
        found.append(env)
        cid = env["call_id"]
        if cid and cid in results:
            r = results[cid]
            extra = r["extra"] or {}
            text = _result_text(r["content"])
            if extra.get("stderr") and isinstance(extra.get("stderr"), str):
                text += "\n[stderr]\n" + extra["stderr"]
            found.append(_finish(env, output=text, is_error=r["is_error"],
                               exit_code=act.exit_code_of(extra) or act.exit_code_of(text),
                               interrupt=extra.get("interrupted") is True, stamp=r["at"],
                               raw=r["content"]))
        elif cid and cid in codex_out:
            r = codex_out[cid]
            exec_end = r.get("exec") or {}
            patch = r.get("patch") or {}
            text = (exec_end.get("aggregated_output") or exec_end.get("stdout")
                    or _result_text(r.get("output")))
            code = exec_end.get("exit_code") if isinstance(exec_end.get("exit_code"),
                                                           int) else act.exit_code_of(
                r.get("output"))
            failed = patch.get("success") is False
            found.append(_finish(env, output=str(text or ""), is_error=failed,
                               exit_code=code, interrupt=False, stamp=r.get("at")))
    return found + [o for o in other if o["phase"] != "start"]


def read_all(path: str | Path, *, tail_bytes: int = MAX_READ_BYTES, app: str = "transcript",
             chat: str | None = None) -> tuple[list[Message], list[dict[str, Any]]]:
    """Messages and tool activity of a transcript, from one read of the file."""
    records = _read_records(path, tail_bytes)
    return extract(records), activity(records, app=app, chat=chat)
