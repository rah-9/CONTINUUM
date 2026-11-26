"""End-to-end tests: the real script, run as a separate process in a throwaway git repo."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "continuity.py"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    git("add", ".")
    git("commit", "-qm", "init")
    return tmp_path


def run(repo: Path, *args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, CONTINUITY_AGENT="test-model", PYTHONDONTWRITEBYTECODE="1")
    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
    return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                          capture_output=True, text=True, env=env)


def ok(repo: Path, *args: str) -> str:
    proc = run(repo, *args)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_store_is_excluded_from_git(repo: Path) -> None:
    ok(repo, "start", "--goal", "build add()")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert ".continuity" not in status


def test_check_pass_goes_stale_on_edit_but_not_on_commit(repo: Path) -> None:
    ok(repo, "start", "--goal", "g")
    ok(repo, "check", "add", "unit", "--",
       f"{sys.executable} -c \"import app; assert app.add(2, 2) == 4\"")
    task = ok(repo, "task", "add", "make add work", "--checks", "unit").split("#")[1].split()[0]
    assert "PASSED" in ok(repo, "check", "run", "unit")
    assert "PASSED on the current code" in ok(repo, "resume")

    subprocess.run(["git", "commit", "-qam", "noop", "--allow-empty"], cwd=repo, check=True)
    assert "PASSED on the current code" in ok(repo, "resume")

    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    brief = ok(repo, "resume")
    assert "code changed since" in brief
    refused = run(repo, "task", "done", task)
    assert refused.returncode == 3 and "stale" in refused.stderr

    assert run(repo, "check", "run", "unit").returncode == 1
    assert "FAILED" in ok(repo, "resume")

    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    ok(repo, "check", "run", "unit")
    assert "verified" in ok(repo, "task", "done", task)
    assert "[x] done" in ok(repo, "resume")
    (repo / "app.py").write_text("def add(a, b):\n    return b + a  # edited\n")
    assert "NEEDS RE-VERIFICATION" in ok(repo, "resume")


def test_changed_check_invalidates_previous_pass(repo: Path) -> None:
    ok(repo, "check", "add", "t", "--", "true")
    ok(repo, "check", "run", "t")
    ok(repo, "check", "add", "t", "--", "false")
    assert "check was changed since its last run" in ok(repo, "resume")


def test_lost_external_effect_blocks_new_ones(repo: Path) -> None:
    ok(repo, "start", "--goal", "release")
    out = ok(repo, "effect", "begin", "git push origin main", "--key", "push-main",
             "--verify", "git log -1 --format=%s", "--expect", "pushed-marker")
    eid = out.split("#")[1].split()[0]
    # the agent's chat dies here: no `effect end` was ever recorded

    blocked = run(repo, "effect", "begin", "npm publish")
    assert blocked.returncode == 3 and "unknown" in blocked.stderr
    assert run(repo, "guard").returncode == 3
    assert "UNKNOWN EXTERNAL EFFECTS" in ok(repo, "resume")

    # a verify that finds nothing is not proof of absence: still unknown
    assert run(repo, "effect", "verify", eid).returncode == 3
    assert run(repo, "guard").returncode == 3

    subprocess.run(["git", "commit", "-qm", "pushed-marker", "--allow-empty"], cwd=repo,
                   check=True)
    assert "confirmed DONE" in ok(repo, "effect", "verify", eid)
    ok(repo, "guard")

    again = run(repo, "effect", "begin", "git push origin main", "--key", "push-main")
    assert again.returncode == 3 and "already done" in again.stderr


def test_failed_effect_can_be_retried_and_user_decides_unknown(repo: Path) -> None:
    eid = ok(repo, "effect", "begin", "deploy", "--key", "d1").split("#")[1].split()[0]
    ok(repo, "effect", "end", eid, "--outcome", "failed", "--result", "401 rejected")
    eid2 = ok(repo, "effect", "begin", "deploy", "--key", "d1").split("#")[1].split()[0]
    ok(repo, "effect", "end", eid2, "--outcome", "unknown", "--result", "timed out")
    assert run(repo, "guard").returncode == 3
    ok(repo, "effect", "resolve", eid2, "--not-done", "--user-said", "it did not deploy")
    ok(repo, "guard")
    ok(repo, "effect", "begin", "deploy", "--key", "d1")


def test_override_requires_user_words(repo: Path) -> None:
    ok(repo, "effect", "begin", "x")
    assert run(repo, "effect", "begin", "y").returncode == 3
    ok(repo, "effect", "begin", "y", "--user-override", "user: go ahead anyway")


def test_requirements_replacement_and_retraction(repo: Path) -> None:
    ok(repo, "start", "--goal", "api")
    ok(repo, "note", "requirement", "use port 4000")
    ok(repo, "note", "requirement", "use port 5000 instead", "--replaces", "2")
    ok(repo, "note", "decision", "store in sqlite", "--reason", "no server allowed")
    ok(repo, "note", "context", "temporary detail")
    ok(repo, "retract", "5")
    ok(repo, "note", "next_step", "write the /health endpoint")
    brief = ok(repo, "resume")
    assert "port 5000" in brief and "port 4000" not in brief
    assert "because: no server allowed" in brief
    assert "temporary detail" not in brief
    assert "write the /health endpoint" in brief
    assert "retracted" in ok(repo, "search", "temporary")


def test_secrets_are_redacted(repo: Path) -> None:
    ok(repo, "note", "context", "key is sk-abcdefghijklmnopqrstuv and password=hunter22")
    raw = (repo / ".continuity" / "journal.jsonl").read_text()
    assert "sk-abcdefghijklmnopqrstuv" not in raw and "hunter22" not in raw
    assert "[REDACTED]" in raw


def test_capture_claude_code_transcript_without_duplicates(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = home / ".claude" / "projects" / "".join(
        c if c.isalnum() else "-" for c in str(repo))
    folder.mkdir(parents=True)
    lines = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "Export must be CSV with a header row"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "tool output"}]}},
        {"type": "user", "uuid": "u3", "isMeta": True,
         "message": {"role": "user", "content": "meta"}},
        {"type": "user", "uuid": "u4", "message": {"role": "user", "content":
            "<system-reminder>injected</system-reminder>Also never touch legacy/"}},
        "not json at all",
    ]
    (folder / "chat.jsonl").write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n")
    assert "Captured 2" in run(repo, "capture", home=home).stdout
    assert "Captured 0" in run(repo, "capture", home=home).stdout
    brief = ok(repo, "resume")
    assert "Export must be CSV with a header row" in brief
    assert "Also never touch legacy/" in brief
    assert "injected" not in brief and "tool output" not in brief and "meta" not in brief


def test_capture_codex_rollout(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    day = home / ".codex" / "sessions" / "2026" / "11" / "19"
    day.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"cwd": str(repo)}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>x</environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "dates must be ISO-8601"}]}},
    ]
    (day / "rollout-1.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    assert "Captured 1" in run(repo, "capture", home=home).stdout
    assert "dates must be ISO-8601" in ok(repo, "resume")


def test_brief_budget_keeps_essentials(repo: Path) -> None:
    ok(repo, "start", "--goal", "g")
    ok(repo, "note", "requirement", "the essential requirement")
    for i in range(60):
        ok(repo, "note", "context", f"filler context number {i} " + "x" * 200)
    brief = ok(repo, "resume", "--budget", "3000")
    assert "the essential requirement" in brief
    assert "omitted for size" in brief
    assert len(brief) < 4000
    assert "filler context number 59" in ok(repo, "resume", "--full")


def test_handoff_and_works_outside_git(tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "a.txt").write_text("1")
    ok(folder, "start", "--goal", "plain folder")
    ok(folder, "check", "add", "has-a", "--", "test -f a.txt")
    ok(folder, "check", "run", "has-a")
    ok(folder, "handoff", "--summary", "set up a.txt", "--next", "write b.txt")
    brief = ok(folder, "resume")
    assert "write b.txt" in brief and "set up a.txt" in brief
    assert "PASSED on the current code" in brief
    (folder / "a.txt").write_text("2")
    assert "code changed since" in ok(folder, "resume")
