"""Continuity scenarios (docs/MEMORY-ALGORITHM.md §5): one test per way real work moves
between models, chats, apps and machines. Each drives the same entry points the apps use
(hook payloads, MCP-equivalent service calls, the CLI) against PostgreSQL and real git."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest

from continuum.coding import hooks
from continuum.coding.brief import compact
from continuum.coding.install import BEGIN, HOOK_COMMAND, setup
from continuum.coding.service import CodingContinuity
from continuum.coding.transcript import Message
from continuum.core.clock import SystemClock
from continuum.core.errors import NotFoundError
from continuum.core.ids import RandomIds
from continuum.core.redaction import REDACTED
from tests.conftest import DSN

PY = sys.executable
REMOTE = "https://github.com/acme/shop.git"
REQ_1 = ("Build `fetch_rates` in lib/rates.py. Requirement: retry a failed request at most "
         "3 times, with exponential backoff.")
REQ_2 = "Also: amounts are Decimal, rounded half-even to 2 places, never float."


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def coding(store):
    return CodingContinuity(store, clock=SystemClock(), ids=RandomIds())


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "shop"
    (root / "lib").mkdir(parents=True)
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "d@e.x")
    _git(root, "config", "user.name", "d")
    _git(root, "remote", "add", "origin", REMOTE)
    (root / "lib" / "app.py").write_text("def main():\n    return 0\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def _claude_transcript(path: Path, last_report: str) -> Path:
    records = [
        {"type": "user", "message": {"role": "user", "content": REQ_1}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Reading lib/app.py."},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "def main(): ..."}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>x</system-reminder>" + REQ_2}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": last_report}]}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


# S1 ---------------------------------------------------------------------------------------

def test_s1_claude_code_to_codex_with_an_abrupt_end_and_no_agent_notes(coding, repo, tmp_path):
    """The hard case: the first app's agent never called a Continuum tool and the chat ended
    without a next step (quota hit). The next app still gets the user's requirements, the
    last report and where the code is, and continues from a subdirectory."""
    cc = {"session_id": "cc-1", "cwd": str(repo)}
    assert hooks.handle("claude-code", {**cc, "hook_event_name": "SessionStart"}, coding) == ""
    hooks.handle("claude-code", {**cc, "hook_event_name": "UserPromptSubmit",
                                 "prompt": REQ_1}, coding)          # starts the session
    (repo / "lib" / "rates.py").write_text(
        "import json\n\n\ndef fetch_rates(client):\n"
        "    return json.loads(client.get('/rates'))\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fetch_rates")
    report = "Part 1 (fetch_rates) is committed. Next: the retry policy."
    hooks.handle("claude-code", {**cc, "hook_event_name": "Stop", "transcript_path": str(
        _claude_transcript(tmp_path / "cc.jsonl", report))}, coding)

    # Codex starts in a subdirectory and resumes over MCP
    brief = compact(coding.resume(agent="gpt-6.1-codex", repo_path=str(repo / "lib")))
    md = brief["markdown"]
    assert REQ_1 in md and REQ_2 in md                        # chat-only requirements kept
    assert md.index(REQ_1) < md.index(REQ_2)                   # in order: newest last
    assert report in md and "a claim" in md                    # the last report, labelled
    assert "lib/rates.py" in md and "fetch_rates" in md         # the code map
    assert "Code touched in this session" in md
    assert "newer than the last recorded next step" in md      # attention: no next step
    assert len(coding.sessions(str(repo))) == 1               # one session, not two

    # a Codex turn (notify payload as argv JSON), then back to Claude Code: memory flows
    hooks.handle("codex", {"type": "agent-turn-complete", "thread-id": "cx-1",
                           "cwd": str(repo / "lib"),
                           "input-messages": ["Now add the retry policy; keep it at 3."],
                           "last-assistant-message": "Retry added in lib/retry.py."}, coding)
    context = hooks.handle("claude-code", {**cc, "hook_event_name": "SessionStart"}, coding)
    assert context.startswith(hooks.CONTEXT_HEADER)
    assert "Now add the retry policy; keep it at 3." in context
    assert "Retry added in lib/retry.py." in context and "codex" in context


# S2 ---------------------------------------------------------------------------------------

def test_s2_another_checkout_continues_the_session_and_uses_its_checks(coding, repo, tmp_path):
    """Local machine -> cloud container: a different path, same remote and branch."""
    coding.register_check(str(repo), "unit", [PY, "-c", "import lib.app"], actor="alice")
    sid = coding.start(str(repo), "ship rates", agent="claude-opus-4-8",
                       tasks=[{"title": "app imports", "checks": ["unit"]}])["session_id"]
    coding.note(sid, "decision", "Rates are cached per currency pair.", agent="o")
    clone = tmp_path / "container" / "work"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    _git(clone, "remote", "set-url", "origin", "git@github.com:Acme/shop.git")  # same repo

    brief = coding.resume(agent="gpt-6.1-codex", repo_path=str(clone))
    assert brief["session_id"] == sid
    assert brief["repo_root"] == str(clone.resolve())
    assert any("moved to another checkout" in a for a in brief["attention"])
    assert "cached per currency pair" in brief["markdown"]
    task = brief["tasks"][0]["task_id"]
    assert coding.run_check(sid, "unit", agent="gpt-6.1-codex")["passed"]  # alice's check
    assert coding.complete_task(sid, task, agent="gpt-6.1-codex")["verified"] is True

    # back on the laptop, the app's hook runs first (before any agent): it follows too
    context = hooks.handle("claude-code", {"hook_event_name": "SessionStart",
                                           "cwd": str(repo)}, coding)
    assert context.startswith(hooks.CONTEXT_HEADER) and sid in context
    assert coding.brief(sid)["repo_root"] == str(repo.resolve())


def test_s2b_a_different_branch_is_never_joined_by_guessing(coding, repo, tmp_path):
    sid = coding.start(str(repo), "feature", agent="a")["session_id"]
    clone = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    _git(clone, "remote", "set-url", "origin", REMOTE)
    _git(clone, "checkout", "-qb", "hotfix")
    with pytest.raises(NotFoundError) as err:
        coding.resume(agent="b", repo_path=str(clone))
    assert sid in str(err.value) and "branch" in str(err.value)
    # explicit is fine: the user named the session
    assert coding.resume(agent="b", session_id=sid, repo_path=str(clone))["session_id"] == sid


# S3 ---------------------------------------------------------------------------------------

def test_s3_hooks_fire_repeatedly_and_concurrently_without_duplicates(coding, repo, tmp_path):
    transcript = _claude_transcript(tmp_path / "t.jsonl", "done")
    payload = {"session_id": "c1", "cwd": str(repo), "hook_event_name": "Stop",
               "transcript_path": str(transcript)}
    hooks.handle("claude-code", payload, coding)
    threads = [threading.Thread(target=hooks.handle, args=("claude-code", payload, coding))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    hooks.handle("claude-code", {**payload, "hook_event_name": "UserPromptSubmit",
                                 "prompt": REQ_1}, coding)       # same words, other event
    coding.capture(str(repo), [Message("user", REQ_2)], source="transcript", chat_id="x")
    sid = coding.latest_active(str(repo))
    captured = [e for e in coding._events(sid)[1] if e["type"] == "ChatCaptured"]
    assert len(captured) == 4   # REQ_1, REQ_2, the report, and REQ_2 from another source
    items = [r["text"] for r in coding.brief(sid)["user_requests"]["items"]]
    assert items == [REQ_1, REQ_2]                               # shown once each


# S4 ---------------------------------------------------------------------------------------

def test_s4_a_hook_never_breaks_or_stalls_the_agent(tmp_path):
    exe = str(Path(PY).parent / "continuum")
    env = {**os.environ, "CONTINUUM_DATABASE_URL": "postgresql://x:y@127.0.0.1:9/none",
           "CONTINUUM_HOME": str(tmp_path / "home")}
    for args, stdin in ((["hook", "claude-code"], json.dumps({"prompt": "hi",
                                                               "cwd": str(tmp_path)})),
                        (["hook", "codex", "{not json"], ""),
                        (["hook", "claude-code"], "[1, 2]")):
        started = time.monotonic()
        out = subprocess.run([exe, *args], input=stdin, env=env, capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0 and out.stdout == "", out.stderr
        assert time.monotonic() - started < 20
    assert "hook" in (tmp_path / "home" / "hooks.log").read_text()


def test_s4b_hook_cli_round_trip_over_a_real_database(schema, repo, tmp_path):
    exe = str(Path(PY).parent / "continuum")
    env = {**os.environ, "CONTINUUM_DATABASE_URL": DSN, "CONTINUUM_DB_SCHEMA": schema,
           "CONTINUUM_HOME": str(tmp_path / "home")}
    notify = json.dumps({"type": "agent-turn-complete", "cwd": str(repo),
                         "input-messages": [REQ_1], "last-assistant-message": "ok"})
    out = subprocess.run([exe, "hook", "codex", notify], env=env, capture_output=True,
                         text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert out.returncode == 0 and out.stdout == "", out.stderr
    start = subprocess.run([exe, "hook", "claude-code"], env=env, capture_output=True,
                           text=True, timeout=60, input=json.dumps(
                               {"hook_event_name": "SessionStart", "cwd": str(repo)}))
    assert REQ_1 in start.stdout and "Continuum coding brief" in start.stdout


# S5 ---------------------------------------------------------------------------------------

def test_s5_long_sessions_stay_bounded_and_nothing_is_forgotten(coding, repo):
    msgs = [Message("user", f"request {i}: " + ("detail " * 40)) for i in range(60)]
    msgs[7] = Message("user", "request 7: the cache directory must be ~/cache-zeta")
    msgs[30] = Message("user", "continue")
    coding.capture(str(repo), msgs, source="claude-code", chat_id="c")
    sid = coding.latest_active(str(repo))
    for i in range(80):
        coding.note(sid, "context", f"context {i}: " + "lorem " * 60, agent="m")
    coding.note(sid, "decision", "Use the vendored parser (ticket QX-551).", agent="m")
    for i in range(10):
        coding.note(sid, "decision", f"later decision {i}", agent="m")
    small = compact(coding.brief(sid))
    md = small["markdown"]
    assert len(json.dumps(small)) / 4 < 4000
    assert "request 0:" in md and "request 59:" in md          # first and newest kept
    assert "zeta" not in md and "QX-551" not in md             # bounded out ...
    hit = coding.recall("cache-zeta directory", repo_path=str(repo))["matches"][0]
    assert "~/cache-zeta" in hit["text"] and hit["kind"] == "user"      # ... but recalled
    assert "QX-551" in coding.recall("QX-551", session_id=sid)["matches"][0]["text"]


# S6 ---------------------------------------------------------------------------------------

def test_s6_projects_without_git_get_a_code_map_too(coding, tmp_path):
    plain = tmp_path / "notebook-project"
    plain.mkdir()
    (plain / "old.py").write_text("x = 1\n")
    past = time.time() - 3600
    os.utime(plain / "old.py", (past, past))
    sid = coding.start(str(plain), "analysis", agent="m")["session_id"]
    (plain / "analysis.py").write_text("def load():\n    return 1\n")
    changes = coding.brief(sid)["verified_facts"]["session_changes"]
    assert changes["basis"] == "mtime"
    assert [f["path"] for f in changes["files"]] == ["analysis.py"]
    assert changes["files"][0]["defines"] == ["load"]


# S7 ---------------------------------------------------------------------------------------

def test_s7_secrets_pasted_into_chat_are_never_stored(coding, repo, store):
    key = "sk-ant-api03-Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0"
    coding.capture(str(repo), [Message("user", f"use this key {key} for the test run"),
                               Message("agent", f"set ANTHROPIC_API_KEY={key}")],
                   source="claude-code")
    with psycopg.connect(DSN) as conn:
        rows = conn.execute(f'SELECT payload_json::text FROM "{store.schema}".coding_events'
                            .encode()).fetchall()
    assert rows and not any(key in r[0] for r in rows)
    assert any(REDACTED in r[0] for r in rows)


# S8 ---------------------------------------------------------------------------------------

def test_s8_short_replies_and_conflicting_requests(coding, repo):
    coding.capture(str(repo), [Message("user", "Use SQLite for storage."),
                               Message("user", "ok"), Message("user", "continue"),
                               Message("user", "Actually use PostgreSQL, not SQLite."),
                               Message("user", "yes")], source="codex", chat_id="t")
    brief = coding.brief(coding.latest_active(str(repo)))
    items = [r["text"] for r in brief["user_requests"]["items"]]
    assert items == ["Use SQLite for storage.", "Actually use PostgreSQL, not SQLite.", "yes"]
    assert brief["user_requests"]["omitted_low_information"] == 2
    assert any("newer one wins" in rule for rule in brief["rules"])


# S9 ---------------------------------------------------------------------------------------

def test_s9_closed_sessions_are_not_resumed_and_capture_starts_a_new_one(coding, repo):
    old = coding.capture(str(repo), [Message("user", "task one")], source="cli")["session_id"]
    coding.close(old, "merged", agent="op", human=True)
    new = coding.capture(str(repo), [Message("user", "task two")], source="cli")["session_id"]
    assert new != old
    assert coding.capture(str(repo), [Message("agent", "report")], source="cli",
                          autostart=False)["captured"] == 1
    # an agent report alone never starts a session
    coding.close(new, "done", agent="op", human=True)
    assert coding.capture(str(repo), [Message("agent", "x")], source="cli")["session_id"] \
        is None


# S10 --------------------------------------------------------------------------------------

def test_s10_setup_wires_any_repo_idempotently_and_keeps_existing_config(tmp_path):
    root = tmp_path / "proj"
    (root / ".claude").mkdir(parents=True)
    (root / "AGENTS.md").write_text("# Team rules\n\nUse tabs.\n")
    (root / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": ["Bash(make:*)"]},
         "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}]}}))
    first = setup(str(root))
    assert first["files"]["AGENTS.md"] == "updated"
    agents = (root / "AGENTS.md").read_text()
    assert agents.startswith("# Team rules\n\nUse tabs.\n") and BEGIN in agents
    assert "coding_resume" in agents and "coding_recall" in agents
    assert "@AGENTS.md" in (root / "CLAUDE.md").read_text()
    settings = json.loads((root / ".claude" / "settings.json").read_text())
    assert settings["permissions"] == {"allow": ["Bash(make:*)"]}
    stop = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert stop == ["notify-send done", HOOK_COMMAND]
    assert set(settings["hooks"]) >= {"SessionStart", "UserPromptSubmit", "PreCompact"}
    assert json.loads((root / ".mcp.json").read_text())["mcpServers"]["continuum"]["args"] \
        == ["mcp"]
    assert 'notify = ["continuum", "hook", "codex"]' in first["codex"]["add_to_user_config"]
    second = setup(str(root))
    assert set(second["files"].values()) <= {"unchanged",
                                             "unchanged (already imports AGENTS.md)"}


# S11 --------------------------------------------------------------------------------------

def test_s11_a_v3_database_upgrades_in_place(store):
    """Existing installations: the old event-type constraint must not reject captures."""
    with store.connection() as conn:
        conn.execute(b"ALTER TABLE coding_events DROP CONSTRAINT coding_events_type_valid")
        conn.execute(b"ALTER TABLE coding_events ADD CONSTRAINT coding_events_type_valid "
                     b"CHECK (type IN ('SessionStarted'))")
        conn.execute(b"UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'")
        conn.commit()
    assert store.ensure_schema() is True
    with store.connection() as conn:
        definition = conn.execute(
            b"SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
            b"WHERE conname = 'coding_events_type_valid' "
            b"AND conrelid = 'coding_events'::regclass").fetchone()["d"]
        conn.rollback()
    assert "ChatCaptured" in definition


# S12 --------------------------------------------------------------------------------------

def test_s12_hooks_installed_for_every_project_only_journal_git_repositories(coding, tmp_path):
    """A plugin's hooks run in every folder the user chats in (Downloads, ~/notes ...):
    capture starts sessions only in git repositories; elsewhere coding_start is explicit."""
    folder = tmp_path / "downloads"
    folder.mkdir()
    out = hooks.handle("claude-code", {"hook_event_name": "UserPromptSubmit",
                                       "cwd": str(folder), "prompt": "summarise this pdf"},
                       coding)
    assert out == "" and coding.sessions(str(folder)) == []
    sid = coding.start(str(folder), "notes cleanup", agent="m")["session_id"]
    coding.capture(str(folder), [Message("user", "keep the headings")], source="cli")
    assert coding.brief(sid)["user_requests"]["items"][0]["text"] == "keep the headings"
