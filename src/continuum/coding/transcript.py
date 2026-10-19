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
Tool calls, tool results, reasoning and images are not conversation and are skipped.
Text that harnesses inject into the user's turn (system reminders, environment
context, instruction files) is removed, so only what the person typed remains.

Input is JSON Lines (one record per line) or one JSON document. Unparseable lines are
skipped, never fatal: a newer format degrades to fewer messages, not to a crash.
"""

from __future__ import annotations

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
                  "ide_diagnostics", "INSTRUCTIONS", "turn_aborted", "session-start-hook")
_INJECTED_BLOCK = re.compile(
    r"<(" + "|".join(re.escape(t) for t in _INJECTED_TAGS) + r")(?:\s[^>]*)?>.*?</\1>",
    re.S)
# Whole messages that are harness text, not the person's words.
_INJECTED_PREFIXES = ("Caveat: The messages below were generated",
                      "# AGENTS.md instructions", "[Request interrupted by user",
                      "This session is being continued from a previous conversation",
                      "<environment_context>", "<user_instructions>")


@dataclass(frozen=True)
class Message:
    role: Literal["user", "agent"]
    text: str
    # The transcript's own id for this message (Claude Code ``uuid``, Codex ``timestamp``),
    # when it has one: the exact identity used to re-import without duplicates or losses.
    ident: str | None = None
    # When the message was written, if the transcript says (ISO-8601): restores the true
    # order of messages recovered after a missed hook.
    at: str | None = None


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
        text = _text_of(node.get("content", node.get("parts")))
        if text is not None:
            yield Message(role, text)
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
    for record, rid in zip(records, ids, strict=True):
        if identified and rid is None:
            continue
        stamp = record.get("timestamp") if isinstance(record, dict) else None
        stamp = stamp if isinstance(stamp, str) else None
        for k, msg in enumerate(_walk(record)):
            text = clean_user_text(msg.text) if msg.role == "user" else msg.text.strip()
            if not text:
                continue
            if out and out[-1].role == msg.role and out[-1].text == text:
                continue
            out.append(Message(msg.role, text, None if rid is None else f"{rid}/{k}", stamp))
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
