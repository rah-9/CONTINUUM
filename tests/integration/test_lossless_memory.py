# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Lossless capture (D-046): every word, every image, and the conversation's real shape
(rewinds and edited messages) reach the journal; the brief shows what is current."""

from __future__ import annotations

import base64
import json
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from continuum.coding import hooks
from continuum.coding.service import CodingContinuity
from continuum.coding.transcript import Message, read_all
from continuum.core.clock import SystemClock
from continuum.core.ids import RandomIds


@pytest.fixture
def coding(store):
    return CodingContinuity(store, clock=SystemClock(), ids=RandomIds())


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path / "home"))
    root = tmp_path / "app"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _png(width: int, height: int) -> str:
    """A real (tiny, valid) PNG of the given size, base64."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8,
                                                            2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


def test_a_long_message_is_kept_whole(coding, repo):
    """Messages over 16,000 characters were clipped at capture: data lost."""
    log = "\n".join(f"line {i}: processing order {i}" for i in range(3000))
    text = f"Here is the whole log:\n{log}\nThe real error is at the very end: ERR_QUOKKA_42"
    coding.capture(str(repo), [Message("user", "Fix the order import."),
                               Message("user", text)], source="claude-code")
    sid = coding.latest_active(str(repo))
    hit = coding.recall("ERR_QUOKKA_42", repo_path=str(repo))
    assert hit["total_matches"] >= 1                     # the very end is still findable
    event = coding.show(sid, seq=hit["matches"][0]["seq"])["payload"]
    blob = event.get("text_blob")
    assert blob and len(event["text"]) < len(text)
    whole = coding.show(sid, blob=blob, limit=100_000)
    assert whole["chars"] == len(text) and "line 2999" in whole["content"]


def test_images_in_chats_and_tool_results_are_stored_and_viewable(coding, repo, tmp_path):
    shot = _png(32, 18)
    transcript = tmp_path / "chat.jsonl"
    lines = [
        {"uuid": "u1", "parentUuid": None, "type": "user", "message": {"role": "user",
         "content": [{"type": "text", "text": "The button is misaligned, see:"},
                     {"type": "image", "source": {"type": "base64", "media_type":
                                                  "image/png", "data": shot}}]}},
        {"uuid": "a1", "parentUuid": "u1", "type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                              "name": "mcp__browser__screenshot",
                                              "input": {"url": "http://localhost"}}]}},
        {"uuid": "r1", "parentUuid": "a1", "type": "user", "message": {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
             {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                          "data": _png(64, 40)}}]}]}},
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines))
    messages, tools = read_all(transcript, app="claude-code", chat="c1")
    assert [m.role for m in messages] == ["user"] and len(messages[0].media) == 1
    assert "base64" not in json.dumps([t.get("output") for t in tools])    # never as text
    coding.capture(str(repo), messages, source="claude-code", chat_id="c1", history=True)
    sid = coding.latest_active(str(repo))
    coding.record_activity(str(repo), tools, source="claude-code", chat_id="c1")
    brief = coding.brief(sid)
    md = brief["markdown"]
    assert "[image #" in md and "image/png 32x18" in md          # the user's screenshot
    assert "Images from tools" in md and "image/png 64x40" in md
    digest = brief["user_requests"]["latest"]["text"].split("[image #")[1].split(":")[0]
    found = coding.media(sid, digest)
    assert found["media_type"] == "image/png" and (found["width"], found["height"]) == (32, 18)
    assert found["content"] == base64.b64decode(shot)
    out = coding.forget(sid, actor="me", reason="test")
    assert out["outputs_erased"] >= 2                              # images erased too


def test_an_edited_and_resent_message_is_not_a_requirement(coding, repo, tmp_path):
    """Claude Code: Esc-Esc / edit a message and send it again adds a sibling under the
    same parent. The old version (and the agent's reply to it) is an abandoned branch."""
    def user(uuid, parent, text):
        return {"uuid": uuid, "parentUuid": parent, "type": "user",
                "message": {"role": "user", "content": text}}

    def agent(uuid, parent, text):
        return {"uuid": uuid, "parentUuid": parent, "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}

    records = [
        user("u1", None, "Build the CSV export."),
        agent("a1", "u1", "Which delimiter should I use?"),
        user("u2", "a1", "Use semicolons as the delimiter."),           # rewound
        agent("a2", "u2", "Done with semicolons."),
        user("u3", "a2", "Also gzip the output."),                      # on the old branch
        user("u2b", "a1", "Use tabs as the delimiter."),                 # the edit
        agent("a2b", "u2b", "Done with tabs."),
    ]
    path = tmp_path / "chat.jsonl"
    # live hooks captured the old prompts first, then the transcript shows the rewind
    coding.capture(str(repo), [Message("user", "Build the CSV export.")],
                   source="claude-code", chat_id="c1")
    for text in ("Use semicolons as the delimiter.", "Also gzip the output."):
        hooks.handle("claude-code", {"hook_event_name": "UserPromptSubmit", "prompt": text,
                                     "cwd": str(repo), "session_id": "c1"}, coding)
    path.write_text("\n".join(json.dumps(r) for r in records))
    hooks.handle("claude-code", {"hook_event_name": "Stop", "cwd": str(repo),
                                 "session_id": "c1", "transcript_path": str(path)}, coding)
    sid = coding.latest_active(str(repo))
    md = coding.brief(sid)["markdown"]
    section = md.split("## What the user asked", 1)[1].split("\n## ", 1)[0]
    assert "tabs" in section
    assert "semicolons" not in section and "gzip" not in section
    assert "2 message(s) were withdrawn" in section
    hits = coding.recall("semicolons", repo_path=str(repo))["matches"]
    assert hits                                   # still in the journal and recall


def test_a_codex_rollback_withdraws_the_turns_it_undid(coding, repo, tmp_path):
    rows = [
        {"type": "event_msg", "payload": {"type": "user_message",
                                          "message": "Add retries to the client."}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Added."}},
        {"type": "event_msg", "payload": {"type": "user_message",
                                          "message": "Make the retry count 9."}},
        {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}},
        {"type": "event_msg", "payload": {"type": "user_message",
                                          "message": "Make the retry count 3."}},
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    messages, _ = read_all(path, app="codex", chat="x")
    assert [(m.text, m.abandoned) for m in messages if m.role == "user"] == [
        ("Add retries to the client.", False), ("Make the retry count 9.", True),
        ("Make the retry count 3.", False)]
    coding.capture(str(repo), messages, source="codex", chat_id="x", history=True)
    md = coding.brief(coding.latest_active(str(repo)))["markdown"]
    assert "retry count 3" in md and "retry count 9" not in md


def test_recall_finds_identifiers_by_their_parts_and_fuses_rankings(coding, repo):
    coding.capture(str(repo), [Message("user", "Fix the order import."),
                               Message("user", "getUserById returns null for new accounts"),
                               Message("user", "the user list page is slow")],
                   source="claude-code")
    hits = coding.recall("user by id", repo_path=str(repo))["matches"]
    assert hits and "getUserById" in hits[0]["text"]
    # a query that matches both words in one message ranks it first
    both = coding.recall("user list slow", repo_path=str(repo))["matches"]
    assert "slow" in both[0]["text"]
