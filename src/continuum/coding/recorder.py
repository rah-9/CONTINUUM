# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Journal what the agent did: tool calls, their results, file impact, compaction (D-042).

Input is a list of envelopes (``activity.from_hook`` / ``transcript.activity``). Everything
is written under the session's row lock in one transaction, idempotently: a call is
identified by its ``call_key``, so the live hooks and every later transcript import of
the same calls add nothing twice. Output text is redacted, then stored whole as a
content-addressed blob (D-042 decision: full output, redacted), with its head and tail
inline in the event for the brief.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from continuum.coding import snapshot
from continuum.coding.models import (
    CompactionObserved,
    FileImpactObserved,
    ToolCallFinished,
    ToolCallResolved,
    ToolCallStarted,
    ToolCallUnresolved,
)

EXCERPT_HEAD = 1500          # characters of output kept inline (start) ...
EXCERPT_TAIL = 1500          # ... and end: errors are usually at the end
BLOB_MAX_CHARS = 1_000_000   # a blob keeps at most this much (head and tail halves)
INPUT_MAX_CHARS = 16_000     # inline tool input; larger inputs are kept whole in a blob
MAX_ERROR = 2000
EFFECTS_NEEDING_RESULT = ("write", "external")


def excerpt(text: str) -> str:
    if len(text) <= EXCERPT_HEAD + EXCERPT_TAIL + 40:
        return text
    omitted = len(text) - EXCERPT_HEAD - EXCERPT_TAIL
    return (text[:EXCERPT_HEAD] + f"\n… [{omitted} characters omitted; the full output is "
            "in the blob] …\n" + text[-EXCERPT_TAIL:])


def _bounded(text: str) -> tuple[str, bool]:
    if len(text) <= BLOB_MAX_CHARS:
        return text, False
    half = BLOB_MAX_CHARS // 2
    return (text[:half] + f"\n… [{len(text) - BLOB_MAX_CHARS} characters omitted] …\n"
            + text[-half:]), True


class Recorder:
    """Writes activity for one session inside an open transaction (``conn``)."""

    def __init__(self, svc: Any, conn: Any, session_id: str, seq: int, actor: str,
                 root: str | None) -> None:
        self.svc = svc
        self.conn = conn
        self.sid = session_id
        self.seq = seq
        self.actor = actor
        self.root = root
        self.written = 0
        rows = conn.execute(
            b"SELECT type, payload_json FROM coding_events WHERE session_id = %s AND "
            b"payload_json ? 'call_key'", (session_id,)).fetchall()
        self.started: dict[str, dict[str, Any]] = {}
        self.finished: set[str] = set()
        self.unresolved: set[str] = set()
        self.resolved: set[str] = set()
        self.impacts: set[str] = set()
        for r in rows:
            self._note(r["type"], r["payload_json"])

    def _note(self, kind: str, p: dict[str, Any]) -> None:
        key = p.get("call_key")
        if not key:
            return
        if kind == "ToolCallStarted":
            self.started.setdefault(key, p)
        elif kind == "ToolCallFinished":
            self.finished.add(key)
        elif kind == "ToolCallUnresolved":
            self.unresolved.add(key)
        elif kind == "ToolCallResolved":
            self.resolved.add(key)
        elif kind == "FileImpactObserved":
            self.impacts.add(key)

    # ---- low level ---------------------------------------------------------------------

    def _put(self, payload: Any) -> None:
        self.seq = self.svc._insert(self.conn, self.sid, self.seq + 1, payload, self.actor)
        self.written += 1
        self._note(type(payload).__name__, payload.model_dump(mode="json"))

    def blob(self, text: str, media_type: str = "text/plain") -> str:
        stored, truncated = _bounded(text)
        digest = "sha256:" + hashlib.sha256(stored.encode("utf-8", "replace")).hexdigest()
        self.conn.execute(
            b"INSERT INTO coding_blobs (session_id, digest, media_type, chars, content, "
            b"truncated, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            b"ON CONFLICT DO NOTHING",
            (self.sid, digest, media_type, len(text), stored, truncated,
             self.svc.clock.now()))
        return digest

    def _text(self, value: Any) -> str:
        if value is None:
            return ""
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                                default=str)
        from continuum.coding.service import _clean

        return _clean(self.svc.redactor.text(text))

    def _redact_json(self, value: Any, depth: int = 0) -> Any:
        if depth > 12:
            return None
        if isinstance(value, str):
            return self._text(value)
        if isinstance(value, list):
            return [self._redact_json(v, depth + 1) for v in value[:500]]
        if isinstance(value, dict):
            return {self._text(str(k))[:200]: self._redact_json(v, depth + 1)
                    for k, v in list(value.items())[:200]}
        if isinstance(value, bool | int | float) or value is None:
            return value
        return self._text(str(value))

    # ---- envelopes ---------------------------------------------------------------------

    def apply(self, env: dict[str, Any]) -> None:
        phase = env.get("phase")
        if phase == "start":
            self.start(env)
        elif phase == "finish":
            self.finish(env)
        elif phase == "start_finish":
            self.start(env)
            self.finish(env)
        elif phase == "compact":
            self.compaction(env)
        elif phase == "turn_end":
            self.turn_end(env)

    def start(self, env: dict[str, Any]) -> None:
        key = str(env["call_key"])
        if key in self.started:
            return
        raw_input = env.get("input")
        redacted = self._redact_json(raw_input)
        serial = json.dumps(redacted, ensure_ascii=False, default=str)
        input_blob = None
        if len(serial) > INPUT_MAX_CHARS:
            input_blob = self.blob(serial, "application/json")
            redacted = serial[:INPUT_MAX_CHARS] + "…"
        snap = _snap(env)
        self._put(ToolCallStarted(
            call_key=key, call_id=self._opt(env.get("call_id"), 200),
            tool=self._text(env.get("tool") or "unknown")[:200],
            kind=env.get("kind") or "builtin", effect=env.get("effect") or "external",
            server=self._opt(env.get("server"), 200), skill=self._opt(env.get("skill"), 200),
            agent_id=self._opt(env.get("agent_id"), 200),
            agent_type=self._opt(env.get("agent_type"), 200),
            input=redacted, input_blob=input_blob, input_chars=len(serial),
            source=self._text(env.get("app") or "app")[:60],
            chat_id=self._opt(env.get("chat"), 120),
            tree_before=snap.get("tree") if isinstance(snap.get("tree"), str) else None,
            sent_at=_timestamp(env.get("at"))))

    def finish(self, env: dict[str, Any]) -> None:
        key = str(env["call_key"])
        if key in self.finished:
            return
        if key not in self.started:          # the start hook was missed: record it now
            self.start({**env, "phase": "start", "snapshot": None})
        output = self._text(env.get("output"))
        raw_error = self._text(env.get("error"))
        # errors are usually at the end of a long output: keep the head and the tail
        error = (raw_error if len(raw_error) <= MAX_ERROR else raw_error[:MAX_ERROR // 4]
                 + "\n… [omitted] …\n" + raw_error[-(MAX_ERROR * 3 // 4):]) or None
        if len(raw_error) > len(output):
            output = raw_error                          # the full error is the output
        blob = self.blob(output) if len(output) > EXCERPT_HEAD + EXCERPT_TAIL + 40 else None
        snap = _snap(env)
        tree_after = snap.get("tree") if isinstance(snap.get("tree"), str) else None
        status = env.get("status") if env.get("status") in (
            "ok", "error", "denied", "interrupted") else "error"
        duration = env.get("duration_ms")
        self._put(ToolCallFinished(
            call_key=key, tool=self.started[key]["tool"], status=status,
            exit_code=env.get("exit_code") if isinstance(env.get("exit_code"), int) else None,
            duration_ms=duration if isinstance(duration, int) else None,
            output_excerpt=excerpt(output), output_blob=blob, output_chars=len(output),
            error=error, untrusted=True, tree_after=tree_after,
            source=self._text(env.get("app") or "app")[:60],
            chat_id=self._opt(env.get("chat"), 120), sent_at=_timestamp(env.get("at"))))
        before = self.started[key].get("tree_before")
        if before and tree_after and key not in self.impacts:
            self.impact(key, before, tree_after, str(env.get("app") or "app"),
                        snap.get("root"))

    def impact(self, key: str | None, before: str, after: str, source: str,
               root: Any = None) -> None:
        """What changed between two snapshots, observed with git (not reported)."""
        where = root if isinstance(root, str) else self.root
        if before == after or where is None:
            return
        found = snapshot.diff(where, before, after)
        if found is None or not found["files"]:
            return
        diff_blob = self.blob(found["patch"], "text/x-diff") if found["patch"] else None
        self._put(FileImpactObserved(
            call_key=key, tree_before=before, tree_after=after, files=found["files"],
            files_total=found["files_total"], diff_blob=diff_blob,
            patch_bytes=found["patch_bytes"], truncated=found["truncated"],
            source=self._text(source)[:60]))

    def compaction(self, env: dict[str, Any]) -> None:
        summary = self._text(env.get("summary"))
        blob = self.blob(summary) if len(summary) > 2000 else None
        when = "before" if env.get("when") == "before" else "after"
        exists = self.conn.execute(
            b"SELECT 1 FROM coding_events WHERE session_id = %s AND type = "
            b"'CompactionObserved' AND payload_json->>'summary_excerpt' = %s AND "
            b"payload_json->>'when' = %s AND coalesce(payload_json->>'sent_at', '') = %s",
            (self.sid, summary[:2000], when, _timestamp(env.get("at")) or "")).fetchone()
        if exists:
            return
        self._put(CompactionObserved(
            trigger=self._opt(env.get("trigger"), 60), when=when,
            summary_excerpt=summary[:2000], summary_blob=blob,
            source=self._text(env.get("app") or "app")[:60],
            chat_id=self._opt(env.get("chat"), 120), sent_at=_timestamp(env.get("at"))))

    def turn_end(self, env: dict[str, Any]) -> None:
        """Changes made between tool calls (the user, an unhooked tool): the snapshot at
        the end of a turn against the newest snapshot the journal already has."""
        snap = _snap(env)
        after = snap.get("tree")
        if not isinstance(after, str):
            return
        row = self.conn.execute(
            b"SELECT coalesce(payload_json->>'tree_after', payload_json->>'tree_before', "
            b"payload_json->'state'->>'tree') AS tree FROM coding_events WHERE "
            b"session_id = %s AND (payload_json ? 'tree_after' OR payload_json ? "
            b"'tree_before' OR payload_json->'state' ? 'tree') AND coalesce("
            b"payload_json->>'tree_after', payload_json->>'tree_before', "
            b"payload_json->'state'->>'tree') IS NOT NULL ORDER BY seq DESC LIMIT 1",
            (self.sid,)).fetchone()
        if row is not None and row["tree"] and row["tree"] != after:
            self.impact(None, row["tree"], after, str(env.get("app") or "app"),
                        snap.get("root"))

    # ---- reconciliation (never a model) --------------------------------------------------

    def open_calls(self) -> list[dict[str, Any]]:
        """Started calls with no recorded result and no unresolved mark yet."""
        return [p for k, p in self.started.items()
                if k not in self.finished and k not in self.unresolved]

    def mark_unresolved(self, *, chat: str | None, other_chats: bool, reason: str) -> int:
        """Calls that can change something and whose result was never recorded: their
        outcome is unknown (not failed). ``chat``: this chat's turn ended; ``other_chats``:
        a new chat started while another chat's calls were still open (a crash)."""
        count = 0
        for p in self.open_calls():
            same = p.get("chat_id") == chat
            if same != other_chats and p.get("effect") in EFFECTS_NEEDING_RESULT:
                self._put(ToolCallUnresolved(call_key=p["call_key"], tool=p["tool"],
                                             effect=p["effect"], reason=reason))
                count += 1
        return count

    def resolve(self, key: str, outcome: str, reason: str) -> None:
        self._put(ToolCallResolved.model_validate(
            {"call_key": key, "outcome": outcome, "reason": reason}))

    def _opt(self, value: Any, limit: int) -> str | None:
        return self._text(value)[:limit] or None if isinstance(value, str) else None


def _snap(env: dict[str, Any]) -> dict[str, Any]:
    value = env.get("snapshot")
    return value if isinstance(value, dict) else {}


def _timestamp(value: Any) -> str | None:
    from continuum.coding.service import _timestamp as ts

    return ts(value) if isinstance(value, str) else None
