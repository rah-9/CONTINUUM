"""Handoff brief quality: bounded size for long sessions, and observed history (v0.7.0)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from continuum.coding.brief import compact
from continuum.coding.service import CodingContinuity
from continuum.core.clock import SystemClock
from continuum.core.ids import RandomIds

PY = sys.executable
TOKENS = 4  # ~chars per token for English/JSON; a conservative budget proxy


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "d@e.x")
    _git(root, "config", "user.name", "d")
    (root / "app.py").write_text("x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def coding(store):
    return CodingContinuity(store, clock=SystemClock(), ids=RandomIds())


def test_b1_brief_stays_bounded_for_a_long_session(coding, repo):
    sid = coding.start(str(repo), "long refactor", agent="m",
                       tasks=[{"title": f"task {i}"} for i in range(8)])["session_id"]
    for i in range(40):
        (repo / f"f{i}.py").write_text("y = 2\n" * i)
    for i in range(120):
        coding.note(sid, "context", f"observation {i}: " + "lorem ipsum " * 120, agent="m")
    for i in range(30):
        coding.note(sid, "decision", f"decision {i}: " + "because " * 80, agent="m")
        coding.checkpoint(sid, f"checkpoint {i}: " + "did things " * 60, agent="m")
    coding.note(sid, "next_step", "FINAL: wire the parser into the CLI", agent="m")
    full = coding.brief(sid)
    small = compact(full)
    assert len(json.dumps(full)) / TOKENS < 8000
    for i in range(120):  # twice as long a session: the brief must not grow
        coding.note(sid, "context", f"later {i}: " + "lorem ipsum " * 120, agent="m")
        coding.note(sid, "decision", f"later decision {i}: " + "because " * 80, agent="m")
    coding.note(sid, "next_step", "FINAL: wire the parser into the CLI", agent="m")
    assert len(json.dumps(coding.brief(sid))) <= len(json.dumps(full)) * 1.02
    full = coding.brief(sid)
    small = compact(full)
    assert len(json.dumps(small)) / TOKENS < 3000
    md = small["markdown"]
    assert "FINAL: wire the parser into the CLI" in md            # latest next step kept
    assert all(f"task {i}" in md for i in range(8))               # every task kept
    assert full["agent_notes"]["omitted"]["context"] == 230       # nothing silently dropped
    assert "older context notes omitted" in md
    assert {"session_id", "status", "goal", "tasks", "markdown"} <= set(small)


def test_b2_commits_since_last_checkpoint_are_observed_facts(coding, repo):
    sid = coding.start(str(repo), "goal", agent="claude-opus-4-8")["session_id"]
    coding.checkpoint(sid, "before the parser work", agent="claude-opus-4-8")
    for subject in ("add parser", "parser: handle empty input"):
        (repo / "app.py").write_text(f"# {subject}\n")
        _git(repo, "commit", "-qam", subject)
    brief = coding.resume(agent="claude-opus-5-5", repo_path=str(repo))
    commits = brief["verified_facts"]["commits_since_last_checkpoint"]
    assert [c["subject"] for c in commits] == ["parser: handle empty input", "add parser"]
    assert "2 commit(s) since the last checkpoint" in brief["markdown"]


def test_b3_history_is_best_effort_when_the_checkpoint_commit_is_gone(coding, repo):
    sid = coding.start(str(repo), "goal", agent="m")["session_id"]
    (repo / "app.py").write_text("v2\n")
    _git(repo, "commit", "-qam", "v2")
    coding.checkpoint(sid, "on v2", agent="m")
    _git(repo, "reset", "-q", "--hard", "HEAD~1")  # history rewritten under the session
    (repo / "app.py").write_text("v3\n")
    _git(repo, "commit", "-qam", "v3")
    brief = coding.brief(sid)
    assert brief["verified_facts"]["commits_since_last_checkpoint"] is None
    assert brief["verified_facts"]["changed_since_last_checkpoint"] is True
