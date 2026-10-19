# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
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
    assert len(json.dumps(full)) / TOKENS < 10000  # opt-in detail="full"; bounded
    for i in range(120):  # twice as long a session: the brief must not grow
        coding.note(sid, "context", f"later {i}: " + "lorem ipsum " * 120, agent="m")
        coding.note(sid, "decision", f"later decision {i}: " + "because " * 80, agent="m")
    coding.note(sid, "next_step", "FINAL: wire the parser into the CLI", agent="m")
    assert len(json.dumps(coding.brief(sid))) <= len(json.dumps(full)) * 1.02
    full = coding.brief(sid)
    small = compact(full)
    assert len(json.dumps(small)) / TOKENS < 4000  # worst case: every note very long
    md = small["markdown"]
    assert "FINAL: wire the parser into the CLI" in md            # latest next step kept
    assert all(f"task {i}" in md for i in range(8))               # every task kept
    assert full["agent_notes"]["omitted"]["context"] == 234       # nothing silently dropped
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


def test_b4_decision_reasons_and_next_steps_reach_the_next_model_whole(coding, repo):
    """Found by a real Sonnet -> Opus handoff trial: clipping at 300 chars cut the decision's
    reason and the context mid-sentence, the very things the next model needs."""
    reason = ("Pattern-based redactions use a labelled marker [REDACTED:<rule>] so operators can "
              "see which rule fired, while exact environment secrets keep the plain marker. "
              "Reason: auditing needs the rule name; existing consumers match the plain "
              "marker, so changing it would break them. END-OF-REASON")
    context = ("Rules live in the ordered table _RULES (name, regex, replacement), applied "
               "after exact env-secret replacement. The bearer rule is case-sensitive on "
               "purpose: a case-insensitive version rewrote 'the bearer of good news'. "
               "END-OF-CONTEXT")
    sid = coding.start(str(repo), "goal", agent="m")["session_id"]
    coding.note(sid, "decision", reason, agent="m")
    coding.note(sid, "context", context, agent="m")
    coding.note(sid, "next_step", "Do task 2 next. " * 30 + "END-OF-NEXT", agent="m")
    md = compact(coding.brief(sid))["markdown"]
    assert "END-OF-REASON" in md and "END-OF-CONTEXT" in md and "END-OF-NEXT" in md


def test_b5_code_map_shows_where_the_session_worked(coding, repo):
    """The next model should read the session's code, not explore the repository."""
    (repo / "lib").mkdir()
    (repo / "lib" / "export.py").write_text("import json\n\n\ndef old():\n    return 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "before the session")
    coding.start(str(repo), "export feature", agent="claude-opus-4-8")
    (repo / "lib" / "export.py").write_text(
        "import json\n\n\ndef old():\n    return 1\n\n\ndef export_session(rows):\n"
        "    return [json.dumps(r, sort_keys=True) for r in rows]\n")
    _git(repo, "commit", "-qam", "add export_session")      # committed work counts
    (repo / "lib" / "cli.py").write_text("def main():\n    pass\n")  # untracked work counts
    brief = coding.resume(agent="claude-opus-5-5", repo_path=str(repo))
    files = {f["path"]: f for f in brief["verified_facts"]["session_changes"]["files"]}
    assert set(files) == {"lib/export.py", "lib/cli.py"}
    assert files["lib/export.py"]["added"] >= 3 and files["lib/cli.py"]["status"] == "untracked"
    assert "old" in " ".join(files["lib/export.py"]["near"])  # git's enclosing context
    assert "export_session" in files["lib/export.py"]["defines"]
    assert "main" in files["lib/cli.py"]["defines"]
    md = brief["markdown"]
    assert "Code touched in this session" in md and "lib/export.py" in md
    assert "app.py" not in md.split("Code touched in this session")[1].split("##")[0]
