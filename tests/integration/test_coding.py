# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Coding-session continuity: real coding work carries across models and chats (C1-C8)."""

import subprocess
import sys
import threading
from pathlib import Path

import psycopg
import pytest

from continuum.coding.service import CodingContinuity
from continuum.core.clock import SystemClock
from continuum.core.errors import InvalidTransitionError, NotFoundError
from continuum.core.ids import RandomIds
from continuum.core.redaction import Redactor

PY = sys.executable


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "app.py").write_text("def add(a, b):\n    raise NotImplementedError\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "dev")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def coding(store):
    return CodingContinuity(store, clock=SystemClock(), ids=RandomIds(),
                            redactor=Redactor.from_environment())


def _register_unit(coding, repo):
    coding.register_check(str(repo), "unit", [PY, "-c",
                                              "import app; assert app.add(1, 2) == 3"],
                          actor="operator", timeout_seconds=30)


def _implement(repo):
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")


def test_c1_switch_claude_opus_4_8_to_5_5_keeps_verified_coding_work(coding, repo):
    _register_unit(coding, repo)
    started = coding.start(str(repo), "Implement add()", agent="claude-opus-4-8",
                           tasks=[{"title": "implement add", "checks": ["unit"]}])
    sid = started["session_id"]
    [task] = started["tasks"]
    coding.note(sid, "decision", "keep add() pure; no type coercion", agent="claude-opus-4-8")
    _implement(repo)
    coding.checkpoint(sid, "implemented add()", agent="claude-opus-4-8")
    refused = coding.complete_task(sid, task["task_id"], agent="claude-opus-4-8")
    assert refused["status"] == "refused"  # claim without evidence is not accepted
    assert coding.run_check(sid, "unit", agent="claude-opus-4-8")["passed"]
    done = coding.complete_task(sid, task["task_id"], agent="claude-opus-4-8")
    assert done == {"status": "done", "task_id": task["task_id"], "verified": True,
                    "warning": None}
    coding.note(sid, "next_step", "add subtract() next", agent="claude-opus-4-8")

    # --- the user switches the coding agent to Opus 5.5 (new chat, new process) ---
    brief = coding.resume(agent="claude-opus-5-5", repo_path=str(repo))
    assert [a["agent"] for a in brief["agents"]] == ["claude-opus-4-8", "claude-opus-5-5"]
    [t] = brief["tasks"]
    assert t["verified"] and not t["needs_reverification"]
    assert brief["verified_facts"]["latest_checks"]["unit"]["on_current_code"]
    assert brief["agent_notes"]["decisions"][0]["text"] == "keep add() pure; no type coercion"
    assert brief["agent_notes"]["next_steps"][-1]["text"] == "add subtract() next"
    assert "verified" in brief["markdown"] and "claude-opus-5-5" in brief["markdown"]


def test_c2_codex_5_6_to_6_1_finds_session_by_repo_in_a_new_process(store, repo):
    first = CodingContinuity(store, clock=SystemClock(), ids=RandomIds())
    sid = first.start(str(repo), "refactor", agent="gpt-5.6-codex")["session_id"]
    first.note(sid, "context", "app.py is the only module", agent="gpt-5.6-codex")
    second = CodingContinuity(store, clock=SystemClock(), ids=RandomIds())  # new chat
    brief = second.resume(agent="gpt-6.1-codex", repo_path=str(repo))
    assert brief["session_id"] == sid
    assert brief["agent_notes"]["context"][0]["by"] == "gpt-5.6-codex"


def test_c3_editing_code_after_verification_requires_reverification(coding, repo):
    _register_unit(coding, repo)
    started = coding.start(str(repo), "g", agent="a",
                           tasks=[{"title": "t", "checks": ["unit"]}])
    sid, tid = started["session_id"], started["tasks"][0]["task_id"]
    _implement(repo)
    coding.run_check(sid, "unit", agent="a")
    coding.complete_task(sid, tid, agent="a")
    (repo / "app.py").write_text("def add(a, b):\n    return a + b + 0\n")
    brief = coding.brief(sid)
    assert brief["tasks"][0]["needs_reverification"]
    assert not brief["verified_facts"]["latest_checks"]["unit"]["on_current_code"]
    assert brief["verified_facts"]["current_code"]["dirty"]
    assert "app.py" in brief["verified_facts"]["current_code"]["changed_files"]


def test_c4_failing_missing_and_timed_out_checks_never_verify(coding, repo):
    _register_unit(coding, repo)
    coding.register_check(str(repo), "slow", [PY, "-c", "import time; time.sleep(5)"],
                          actor="op", timeout_seconds=1)
    started = coding.start(str(repo), "g", agent="a",
                           tasks=[{"title": "t", "checks": ["unit", "slow"]}])
    sid, tid = started["session_id"], started["tasks"][0]["task_id"]
    assert not coding.run_check(sid, "unit", agent="a")["passed"]  # not implemented yet
    timed = coding.run_check(sid, "slow", agent="a")
    assert timed["timed_out"] and not timed["passed"]
    assert coding.complete_task(sid, tid, agent="a")["status"] == "refused"
    with pytest.raises(NotFoundError):
        coding.run_check(sid, "deploy_to_prod", agent="a")  # agents cannot invent checks
    unregistered = coding.start(str(repo), "g2", agent="a",
                                tasks=[{"title": "x", "checks": ["nonexistent"]}])
    out = coding.complete_task(unregistered["session_id"],
                               unregistered["tasks"][0]["task_id"], agent="a")
    assert out["status"] == "refused" and "not registered" in out["reason"]


def test_c5_task_without_checks_is_an_unverified_claim(coding, repo):
    started = coding.start(str(repo), "docs", agent="a", tasks=[{"title": "write README"}])
    out = coding.complete_task(started["session_id"], started["tasks"][0]["task_id"],
                               agent="a")
    assert out["verified"] is False and "unverified" in out["warning"]
    assert "done (unverified)" in coding.brief(started["session_id"])["markdown"]


def test_c6_secrets_redacted_from_notes_and_check_output(store, repo, monkeypatch):
    secret = "ghp_secretTokenValue123456789"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    coding = CodingContinuity(store, clock=SystemClock(), ids=RandomIds(),
                              redactor=Redactor.from_environment())
    coding.register_check(str(repo), "leak", [PY, "-c", f"print('{secret}')"], actor="op")
    sid = coding.start(str(repo), "g", agent="a")["session_id"]
    coding.note(sid, "context", f"token is {secret}", agent="a")
    coding.run_check(sid, "leak", agent="a")
    with store.connection() as conn:
        dump = conn.execute(b"SELECT string_agg(payload_json::text, '') AS t "
                            b"FROM coding_events").fetchone()["t"]
    assert secret not in dump and "[REDACTED]" in dump


def test_c7_journal_is_immutable_and_closed_sessions_reject_writes(coding, repo, store):
    sid = coding.start(str(repo), "g", agent="a")["session_id"]
    with store.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute(b"UPDATE coding_events SET actor = 'x'")
    coding.close(sid, "goal reached", agent="a")
    with pytest.raises(InvalidTransitionError):
        coding.note(sid, "context", "late", agent="a")


def test_c8_concurrent_agents_append_gaplessly(coding, repo, store):
    sid = coding.start(str(repo), "g", agent="a")["session_id"]

    def writer(n):
        for i in range(10):
            coding.note(sid, "context", f"{n}-{i}", agent=f"agent-{n}")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with store.connection() as conn:
        seqs = [r["seq"] for r in conn.execute(
            b"SELECT seq FROM coding_events WHERE session_id = %s ORDER BY seq", (sid,))]
    assert seqs == list(range(1, 42))


def test_c9_any_kind_of_project_without_git(coding, tmp_path):
    proj = tmp_path / "thesis"
    proj.mkdir()
    (proj / "chapter1.tex").write_text("\\section{Intro}")
    (proj / "node_modules").mkdir()
    sid = coding.start(str(proj), "write thesis", agent="a")["session_id"]
    before = coding.brief(sid)["verified_facts"]["current_code"]
    assert before["vcs"] == "files"
    (proj / "node_modules" / "ignored.js").write_text("x")  # dependency dirs are skipped
    assert coding.brief(sid)["verified_facts"]["current_code"]["fingerprint"] == \
        before["fingerprint"]
    (proj / "chapter1.tex").write_text("\\section{Introduction}")
    assert coding.brief(sid)["verified_facts"]["current_code"]["fingerprint"] != \
        before["fingerprint"]


def test_c10_changing_a_check_definition_invalidates_verification(coding, repo):
    """Tamper evidence: weakening a check (e.g. to `true`) after verification is detected."""
    _register_unit(coding, repo)
    started = coding.start(str(repo), "g", agent="a",
                           tasks=[{"title": "t", "checks": ["unit"]}])
    sid, tid = started["session_id"], started["tasks"][0]["task_id"]
    _implement(repo)
    coding.run_check(sid, "unit", agent="a")
    assert coding.complete_task(sid, tid, agent="a")["verified"]
    coding.register_check(str(repo), "unit", [PY, "-c", "pass"], actor="someone")
    brief = coding.brief(sid)
    assert brief["tasks"][0]["needs_reverification"]
    assert brief["verified_facts"]["latest_checks"]["unit"]["valid_now"] is False
    assert brief["verified_facts"]["registered_checks"]["unit"]["changed_during_session"]
    assert "CHANGED" in brief["markdown"] and "(re)registered" in brief["markdown"]
    assert coding.complete_task(sid, tid, agent="a")["status"] == "refused"


def test_c11_shell_only_agents_use_the_same_protocol_via_cli(schema, repo, tmp_path):
    """Agents without MCP support (any agent with a shell) follow the same workflow."""
    import json
    import os

    from tests.conftest import DSN

    exe = str(Path(sys.executable).parent / "continuum")
    env = {**os.environ, "CONTINUUM_DATABASE_URL": DSN, "CONTINUUM_DB_SCHEMA": schema,
           "CONTINUUM_HOME": str(tmp_path / "home")}

    def cli(*args, ok=True):
        out = subprocess.run([exe, *args], env=env, capture_output=True, text=True)
        assert (out.returncode == 0) == ok, out.stderr + out.stdout
        return out.stdout

    cli("coding", "checks", "add", str(repo), "unit", "--actor", "op", "--", PY, "-c",
        "import app; assert app.add(1, 2) == 3")
    sid = json.loads(cli("coding", "start", str(repo), "--goal", "add",
                         "--agent", "gpt-5.6-codex"))["session_id"]
    tid = json.loads(cli("coding", "task", "add", sid, "implement add", "--check", "unit",
                         "--agent", "gpt-5.6-codex"))["task_id"]
    _implement(repo)
    cli("coding", "task", "done", sid, tid, "--agent", "gpt-5.6-codex", ok=False)  # no evidence
    cli("coding", "check", sid, "unit", "--agent", "gpt-5.6-codex")
    cli("coding", "task", "done", sid, tid, "--agent", "gpt-5.6-codex")
    cli("coding", "note", sid, "next_step", "add tests for negatives",
        "--agent", "gpt-5.6-codex")
    brief = cli("coding", "resume", "--repo", str(repo), "--agent", "gpt-6.1-codex")
    assert "✅ verified" in brief and "add tests for negatives" in brief
    assert "gpt-5.6-codex (started), gpt-6.1-codex (joined)" in brief
