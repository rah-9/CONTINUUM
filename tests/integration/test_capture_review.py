# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Regression tests for the capture, memory and recall review (1.0.0rc2): each test is a
failure scenario that was reproduced before the fix."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from continuum.coding import hooks, spool
from continuum.coding.service import CodingContinuity
from continuum.coding.transcript import Message
from continuum.core.clock import SystemClock
from continuum.core.errors import SpecValidationError
from continuum.core.ids import RandomIds
from tests.conftest import DSN


@pytest.fixture
def coding(store):
    return CodingContinuity(store, clock=SystemClock(), ids=RandomIds())


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path / "home"))
    root = tmp_path / "app"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "app.py").write_text("x = 1\n")
    return root


def _hook(coding, root: Path, payload: dict, app: str = "claude-code") -> str:
    out, entry = hooks.fast(app, {"cwd": str(root), "session_id": "chat-1", **payload})
    hooks.handle(app, entry["payload"], coding, entry["snapshot"])
    return out


def _start(coding, root: Path) -> str:
    coding.capture(str(root), [Message("user", "Ship the release.")], source="claude-code")
    return str(coding.latest_active(str(root)))


def test_an_interrupted_push_is_an_unknown_outcome_and_blocks_external_calls(coding, repo):
    """Review #1: an interrupted (or timed-out) external call counted as finished, so
    nothing was blocked although the push may have gone through."""
    sid = _start(coding, repo)
    push = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"},
            "tool_use_id": "p1"}
    _hook(coding, repo, {"hook_event_name": "PreToolUse", **push})
    _hook(coding, repo, {"hook_event_name": "PostToolUseFailure", **push,
                         "error": "Command interrupted", "is_interrupt": True})
    _hook(coding, repo, {"hook_event_name": "Stop"})
    [lost] = coding.unresolved(sid)
    assert lost["tool"] == "Bash" and "interrupted" in lost["reason"]
    denied = _hook(coding, repo, {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                  "tool_input": {"command": "git push --tags"},
                                  "tool_use_id": "p2"})
    decision = json.loads(denied)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert f"continuum coding resolve {sid} " in decision["permissionDecisionReason"]
    assert coding.brief(sid)["work_log"]["commands"][0]["status"] == "unknown"
    # an ordinary failure with an exit code is a known outcome: nothing more is blocked
    coding.resolve_unknown(sid, lost["call_key"], "not_applied", actor="me",
                           reason="checked the remote")
    hooks._update_blocks(coding, sid)
    lint = {"tool_name": "Bash", "tool_input": {"command": "curl -X POST https://x/hook"},
            "tool_use_id": "p3"}
    _hook(coding, repo, {"hook_event_name": "PreToolUse", **lint})
    _hook(coding, repo, {"hook_event_name": "PostToolUse", **lint,
                         "tool_response": {"stdout": "", "stderr": "400 bad request",
                                           "exit_code": 22}})
    assert coding.unresolved(sid) == []


def test_identical_calls_without_ids_are_each_recorded(coding, repo):
    """Review #2: Gemini and Cursor send no call id; a second identical push whose result
    was lost collapsed into the first and left no unknown outcome."""
    sid = _start(coding, repo)
    push = {"tool_name": "run_shell_command", "tool_input": {"command": "git push"}}
    _hook(coding, repo, {"hook_event_name": "BeforeTool", **push}, "gemini-cli")
    _hook(coding, repo, {"hook_event_name": "AfterTool", **push,
                         "tool_response": {"llmContent": "ok"}}, "gemini-cli")
    _hook(coding, repo, {"hook_event_name": "BeforeTool", **push}, "gemini-cli")
    _hook(coding, repo, {"hook_event_name": "AfterAgent"}, "gemini-cli")
    work = coding.brief(sid)["work_log"]
    assert [c["status"] for c in work["commands"]] == ["ok", "unknown"]
    assert len(coding.unresolved(sid)) == 1


def test_a_late_result_lifts_the_block_at_once(coding, repo):
    """Review #4: a new chat marked another chat's running call unresolved; when that
    call's result arrived the block file stayed and denied the next external call."""
    sid = _start(coding, repo)
    deploy = {"tool_name": "Bash", "tool_input": {"command": "kubectl apply -f k8s/"},
              "tool_use_id": "d1"}
    _hook(coding, repo, {"hook_event_name": "PreToolUse", **deploy})
    hooks.handle("claude-code", {"hook_event_name": "SessionStart", "cwd": str(repo),
                                 "session_id": "chat-2"}, coding)
    assert spool.blocks_for(str(repo))
    _hook(coding, repo, {"hook_event_name": "PostToolUse", **deploy,
                         "tool_response": {"stdout": "applied", "exit_code": 0}})
    assert coding.unresolved(sid) == [] and spool.blocks_for(str(repo)) == []
    assert spool.blocks_for(str(repo / "sub" / "dir")) == []   # found from a subfolder too


def test_a_secret_written_by_a_tool_never_reaches_the_stored_diff(coding, repo):
    """Review #3: the diff blob was stored unredacted."""
    sid = _start(coding, repo)
    secret = "sk-live-" + "9f8e7d6c" * 4
    edit = {"tool_name": "Write", "tool_input": {"file_path": str(repo / "conf.py")},
            "tool_use_id": "w1"}
    _hook(coding, repo, {"hook_event_name": "PreToolUse", **edit})
    (repo / "conf.py").write_text(f"KEY = '{secret}'\n")
    _hook(coding, repo, {"hook_event_name": "PostToolUse", **edit,
                         "tool_response": {"filePath": "conf.py"}})
    assert [f["path"] for f in coding.brief(sid)["work_log"]["files_changed"]] == ["conf.py"]
    with psycopg.connect(DSN) as conn:
        stored = conn.execute(f'SELECT string_agg(content, \'\') FROM '
                              f'"{coding.store.schema}".coding_blobs'.encode()).fetchone()[0]
    assert stored and "conf.py" in stored and secret not in stored


def test_recall_handles_stems_aliases_short_words_and_whole_days(coding, repo):
    """Review: a stemmed-only match in a long event crashed recall; transcript aliases
    came back as empty hits; "or" matched "for"; `until=<date>` dropped that day."""
    root = str(repo)
    long_text = ("The nightly payments job failed again after the upgrade. " * 15).strip()
    coding.capture(root, [Message("user", long_text, "u1", "2026-10-19T10:00:00+00:00"),
                          Message("user", "zebra quokka plan", "u2",
                                  "2026-10-19T10:01:00+00:00")],
                   source="claude-code", chat_id="c", history=True)
    coding.capture(root, [Message("user", "zebra quokka plan", "u2",
                                  "2026-10-19T10:01:00+00:00")],
                   source="claude-code", chat_id="c", history=True)
    hit = coding.recall("failing", repo_path=root)          # stem only: no crash
    assert hit["total_matches"] >= 1 and "failed" in hit["matches"][0]["text"]
    quokka = coding.recall("quokka", repo_path=root)
    assert all(m["text"] for m in quokka["matches"]) and quokka["total_matches"] == 1
    assert coding.recall("or", repo_path=root)["total_matches"] == 0
    today = datetime.now(UTC).date().isoformat()
    assert coding.recall("quokka", repo_path=root, until=today)["total_matches"] == 1


def test_show_accepts_only_a_digest_and_never_guesses_between_two(coding, repo):
    sid = _start(coding, repo)
    for prefix in ("%", "zz", "sha256:%"):
        with pytest.raises(SpecValidationError):
            coding.show(sid, blob=prefix)
    out = coding.forget(sid, actor="me", reason="test")
    assert out["events_erased"] >= 1 and "outputs_erased" in out
