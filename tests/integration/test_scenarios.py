# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
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
from continuum.core.errors import NotFoundError, SpecValidationError
from continuum.core.ids import RandomIds
from continuum.core.redaction import REDACTED
from tests.conftest import DSN

PY = sys.executable
REMOTE = "https://github.com/acme/shop.git"
REQ_1 = ("Build `fetch_rates` in lib/rates.py. Requirement: retry a failed request at most "
         "3 times, with exponential backoff.")
REQ_2 = "Also: amounts are Decimal, rounded half-even to 2 places, never float."


def _said(brief: dict) -> list[str]:
    """What the brief carries of the user's words, in conversation order."""
    m = brief["user_requests"]
    parts = ([(m["first"]["n"], 0, m["first"]["text"])] if m["first"] else [])
    parts += [(i["n"], i["pos"], i["text"]) for i in m["ledger"]]
    parts += [(r["n"], 0, r["text"]) for r in m["recent"]]
    parts += [(m["latest"]["n"], 99, m["latest"]["text"])] if m["latest"] else []
    return [text for _, _, text in sorted(parts)]


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
    # The transcript re-imports (5 hook calls, 4 concurrent) add nothing twice. REQ_1
    # submitted again as a new prompt IS a new message (people repeat themselves, and a
    # second "yes" must not be lost), and REQ_2 via another source is kept in the journal.
    # every agent text message of the transcript is kept, not only the turn's last one
    assert [(e["payload_json"]["role"], e["payload_json"]["text"][:12]) for e in captured] == [
        ("user", REQ_1[:12]), ("agent", "Reading lib/"), ("user", REQ_2[:12]),
        ("agent", "done"), ("user", REQ_1[:12]), ("user", REQ_2[:12])]
    # in order: #1 REQ_1, #2 REQ_2 (the copy from another source is not repeated),
    # #3 REQ_1 again: the latest message
    assert _said(coding.brief(sid)) == [REQ_1, REQ_2, REQ_1]


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
    assert "hook" in (tmp_path / "home" / "hooks.log").read_text(encoding="utf-8")


def test_s4b_hook_cli_round_trip_over_a_real_database(schema, repo, tmp_path):
    exe = str(Path(PY).parent / "continuum")
    env = {**os.environ, "CONTINUUM_DATABASE_URL": DSN, "CONTINUUM_DB_SCHEMA": schema,
           "CONTINUUM_HOME": str(tmp_path / "home"), "CONTINUUM_HOOKS_DEBUG": "1"}
    notify = json.dumps({"type": "agent-turn-complete", "cwd": str(repo),
                         "input-messages": [REQ_1], "last-assistant-message": "ok"})
    out = subprocess.run([exe, "hook", "codex", notify], env=env, capture_output=True,
                         text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert out.returncode == 0 and out.stdout == "", out.stderr
    # an app reading through a pipe in a legacy code page (Windows' default) still gets
    # the whole brief, symbols included: Windows CI found it was silently lost
    start = subprocess.run([exe, "hook", "claude-code"], env={
        **env, "PYTHONIOENCODING": "cp1252"}, capture_output=True, timeout=60,
        input=json.dumps({"hook_event_name": "SessionStart", "cwd": str(repo)}).encode())
    start.stdout = start.stdout.decode("utf-8")
    log = tmp_path / "home" / "hooks.log"
    assert REQ_1 in start.stdout and "Continuum coding brief" in start.stdout, (
        log.read_text(encoding="utf-8") if log.exists() else "no hooks.log",
        sorted(p.name for p in (tmp_path / "home").rglob("*")))


# S5 ---------------------------------------------------------------------------------------

def test_s5_long_sessions_stay_bounded_and_nothing_is_forgotten(coding, repo):
    msgs = [Message("user", f"request {i}: " + ("detail " * 40)) for i in range(60)]
    msgs[7] = Message("user", "request 7: the cache directory must be ~/cache-zeta")
    msgs[12] = Message("user", "Why is the build slow on the `orbit-x9` runner?")
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
    assert "~/cache-zeta" in md                  # a requirement from message 8 of 60 is kept
    # questions and old notes are not requirements; the chat index may name the stretch
    # where the question was asked (D-046), so it is reachable, never as a requirement
    body = "\n".join(ln for ln in md.splitlines()
                     if not ln.startswith("- **Index of the whole chat**"))
    assert "orbit-x9" not in body and "QX-551" not in md
    hit = coding.recall("orbit-x9 runner", repo_path=str(repo))["matches"][0]
    assert "orbit-x9" in hit["text"] and hit["kind"] == "user"          # ... but recalled
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
    m = brief["user_requests"]
    assert _said(brief) == ["Use SQLite for storage.", "Actually use PostgreSQL, not SQLite.",
                            "yes"]
    assert m["ledger"][0]["kind"] == "revision" and m["ledger"][0]["n"] == 4
    assert m["omitted_messages"] == 2                            # "ok", "continue"
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
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("# Team rules\n\nUse tabs.\n") and BEGIN in agents
    assert "coding_resume" in agents and "coding_recall" in agents
    assert "@AGENTS.md" in (root / "CLAUDE.md").read_text(encoding="utf-8")
    settings = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["permissions"] == {"allow": ["Bash(make:*)"]}
    stop = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert stop == ["notify-send done", HOOK_COMMAND]
    assert set(settings["hooks"]) >= {"SessionStart", "UserPromptSubmit", "PreCompact"}
    mcp = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["continuum"]["args"] == ["mcp"]
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
    assert _said(coding.brief(sid)) == ["keep the headings"]


# S13 --------------------------------------------------------------------------------------

def test_s13_repeated_words_in_one_chat_are_all_kept_and_reimports_add_nothing(
        coding, repo, tmp_path):
    """A second "yes" or "continue" in the same chat was dropped as a duplicate (found in the
    30-message trial). Each submitted prompt is a message; a transcript re-import is not."""
    turns = [("Should I drop `v1`?", "yes"), ("And `v2`?", "yes"), ("And `v3`?", "no")]
    for question, reply in turns:
        hooks.handle("codex", {"type": "agent-turn-complete", "thread-id": "t",
                               "cwd": str(repo), "input-messages": ["go on"],
                               "last-assistant-message": question}, coding)
        hooks.handle("codex", {"type": "agent-turn-complete", "thread-id": "t",
                               "cwd": str(repo), "input-messages": [reply],
                               "last-assistant-message": "Done."}, coding)
    sid = coding.latest_active(str(repo))
    users = [e["payload_json"]["text"] for e in coding._events(sid)[1]
             if e["type"] == "ChatCaptured" and e["payload_json"]["role"] == "user"]
    assert users == ["go on", "yes", "go on", "yes", "go on", "no"]
    answers = [i["text"] for i in coding.brief(sid)["user_requests"]["ledger"]
               if i["kind"] == "answer"]
    assert any("v1" in a for a in answers) and any("v2" in a for a in answers)
    assert "v3" in coding.brief(sid)["user_requests"]["latest"]["in_reply_to"]

    # the same chat as a transcript, imported twice: nothing is added
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps({"role": "user", "content": u}) + "\n" for u in users))
    before = len(coding._events(sid)[1])
    for _ in range(2):
        coding.capture(str(repo), [Message("user", u) for u in users], source="codex",
                       chat_id="t", history=True)
    assert len(coding._events(sid)[1]) == before


# S14 --------------------------------------------------------------------------------------

def test_s14_both_sides_of_the_chat_are_kept_exactly_through_an_outage(coding, repo, tmp_path):
    """Everything the user AND the agent wrote is kept: no message missing, none twice, in
    the order written. Shaped like a real Claude Code transcript (found in Trial 5's): each
    message record has a uuid and a timestamp; the file also holds uuid-less request/cache
    copies of messages; words repeat ("continue", "Done."); hooks fail for the first turns
    (database down) and catch up later."""
    script = [("user", "Build the parser."), ("agent", "Reading the grammar first."),
              ("agent", "I chose a recursive-descent parser because the grammar is LL(1)."),
              ("user", "continue"), ("agent", "Done."),
              ("user", "Errors must include the line number."), ("agent", "Done."),
              ("user", "continue"), ("agent", "Note: unicode identifiers are not handled yet."),
              ("agent", "Done.")]
    lines, starts = [], []
    for i, (role, text) in enumerate(script):
        kind = "user" if role == "user" else "assistant"
        if role == "user":
            starts.append(len(lines))
        lines.append(json.dumps({"type": kind, "uuid": f"u{i}",
                                 "timestamp": f"2026-10-18T10:{i:02d}:00Z",
                                 "message": {"role": kind, "content": [
                                     {"type": "text", "text": text}]}}))
        lines.append(json.dumps({"type": "api-request-blob", "hash": f"h{i}",
                                 "message": {"role": kind, "content": text}}))
    starts.append(len(lines))
    partial = tmp_path / "t.jsonl"
    base = {"session_id": "chat-1", "cwd": str(repo)}
    for t in range(len(starts) - 1):
        if t < 2:
            continue                       # database down: these hooks only logged errors
        prompt = json.loads(lines[starts[t]])["message"]["content"][0]["text"]
        hooks.handle("claude-code", {**base, "hook_event_name": "UserPromptSubmit",
                                     "prompt": prompt}, coding)
        partial.write_text("\n".join(lines[:starts[t + 1]]) + "\n")
        hooks.handle("claude-code", {**base, "hook_event_name": "Stop",
                                     "transcript_path": str(partial)}, coding)
    for _ in range(2):                     # session end re-reads it all (twice: idempotent)
        partial.write_text("\n".join(lines) + "\n")
        hooks.handle("claude-code", {**base, "hook_event_name": "SessionEnd",
                                     "transcript_path": str(partial)}, coding)
    view = coding._view(coding.latest_active(str(repo)))
    assert [(c["role"], c["text"]) for c in view.chat] == script
    brief = coding.brief(view.session_id)
    decisions = " ".join(d["text"] for d in brief["agent_notes"]["reported_decisions"])
    assert "recursive-descent" in decisions and "unicode identifiers" in decisions
    assert any("line number" in i["text"] for i in brief["user_requests"]["ledger"])


def test_s15_everything_the_agent_did_is_journaled_with_results_and_impact(
        coding, repo, tmp_path, monkeypatch):
    """A real debugging turn through the hook code path (D-042): every tool call with its
    result, what each call changed in the files (observed with snapshots, including a
    shell command's changes), a failing test, an MCP write whose result was lost, a skill,
    a subagent, compaction, and a secret in a command. The next model gets all of it, and
    a person must resolve the lost external call before new external calls run."""
    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path / "home"))
    root = str(repo)
    secret = "sk-live-" + "a1b2c3d4" * 4
    long_log = "\n".join(f"line {i}: ok" for i in range(4000)) + "\nERROR zeta-917 at the end"

    def hook(payload, app="claude-code"):
        payload = {"cwd": root, "session_id": "chat-1", **payload}
        out, entry = hooks.fast(app, payload)
        hooks.handle(app, entry["payload"], coding, entry["snapshot"])
        return out

    def call(i, tool, tool_input, response=None, *, error=None, edit=None):
        hook({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
              "tool_use_id": f"t{i}"})
        if edit:
            edit()
        if error is not None:
            hook({"hook_event_name": "PostToolUseFailure", "tool_name": tool,
                  "tool_input": tool_input, "tool_use_id": f"t{i}", "error": error})
        elif response is not None:
            hook({"hook_event_name": "PostToolUse", "tool_name": tool,
                  "tool_input": tool_input, "tool_use_id": f"t{i}",
                  "tool_response": response, "duration_ms": 40})

    hook({"hook_event_name": "UserPromptSubmit", "prompt": "Totals are off by a cent; fix it."})
    sid = coding.latest_active(root)
    call(1, "Read", {"file_path": f"{root}/lib/app.py"}, {"file": {"content": "def main()"}})
    call(2, "Edit", {"file_path": f"{root}/lib/app.py"}, {"filePath": "lib/app.py"},
         edit=lambda: (repo / "lib" / "app.py").write_text("def main():\n    return 1\n"))
    call(3, "Bash", {"command": "python gen.py"}, {"stdout": "generated", "stderr": ""},
         edit=lambda: (repo / "lib" / "gen_out.py").write_text("TABLE = [1, 2]\n"))
    call(4, "Bash", {"command": f"curl -s -H 'Authorization: Bearer {secret}' localhost"},
         {"stdout": "{}", "stderr": ""})
    call(5, "Bash", {"command": "pytest -q tests/test_totals.py"}, error="Exit code 1\n"
         + long_log)
    call(6, "Skill", {"skill": "pdf"}, {"result": "loaded"})
    call(7, "Task", {"description": "find callers of round_half"},
         {"content": [{"type": "text", "text": "Callers: billing.py:12, invoice.py:40"}]})
    call(8, "mcp__github__create_issue", {"title": "rounding bug"})      # result lost
    hook({"hook_event_name": "PostCompact", "compact_summary": "Working on rounding."})
    hook({"hook_event_name": "Stop", "last_assistant_message": "Stopping for today."})

    brief = coding.brief(sid)
    work, md = brief["work_log"], brief["markdown"]
    changed = {f["path"]: f for f in work["files_changed"]}
    assert set(changed) == {"lib/app.py", "lib/gen_out.py"}        # the shell's file too
    assert changed["lib/gen_out.py"]["status"] == "added"
    assert work["by_kind"] == {"builtin": 2, "shell": 3, "skill": 1, "subagent": 1, "mcp": 1}
    assert [f["target"] for f in work["failures"]] == ["pytest -q tests/test_totals.py"]
    assert "zeta-917" in work["failures"][0]["error_tail"]          # the end of the output
    assert work["skills"] == ["pdf"] and "billing.py:12" in work["subagents"][0]["report"]
    assert work["compactions"] == 1
    [lost] = work["unresolved"]
    assert lost["tool"] == "mcp__github__create_issue" and lost["effect"] == "external"
    assert "Outcome UNKNOWN" in md and "continuum coding resolve" in md
    assert "## What the previous agent did" in md and "`lib/gen_out.py`" in md
    assert secret not in json.dumps(coding.show(sid, seq=1)) and secret not in md
    with psycopg.connect(DSN) as conn:
        dump = conn.execute(f'SELECT string_agg(content, \'\') FROM "{coding.store.schema}"'
                            f'.coding_blobs'.encode()).fetchone()[0]
        events = json.dumps(conn.execute(f'SELECT payload_json FROM "{coding.store.schema}"'
                                         f'.coding_events'.encode()).fetchall())
    assert secret not in dump and secret not in events
    # the full output is kept and found: the brief has its end, recall and show the rest
    hit = coding.recall("line 2500", session_id=sid)["matches"][0]
    assert hit.get("blob") and "line 2500" in coding.show(sid, blob=hit["blob"],
                                                          limit=100_000)["content"]
    diff_seq = changed["lib/gen_out.py"]["seq"]
    diff = coding.show(sid, seq=diff_seq)["payload"]
    assert "+TABLE = [1, 2]" in coding.show(sid, blob=diff["diff_blob"])["content"]
    # a new external call is denied until a person resolves the lost one; local work is not
    denied = hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "git push"}, "tool_use_id": "t9"})
    assert json.loads(denied)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "pytest -q"}, "tool_use_id": "t10"}) == ""
    with pytest.raises(SpecValidationError):
        coding.resolve_unknown(sid, lost["short"], "applied", actor="", reason="checked")
    coding.resolve_unknown(sid, lost["short"], "not_applied", actor="alice",
                           reason="no such issue on GitHub")
    from continuum.coding import spool

    spool.set_blocks(coding.sessions_root(sid), coding.unresolved(sid))
    assert hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "git push"}, "tool_use_id": "t11"}) == ""
    assert coding.brief(sid)["work_log"]["unresolved"] == []
    # the app's transcript of the same calls adds nothing twice
    before = len(coding._events(sid)[1])
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": f"{root}/lib/app.py"}},
            {"type": "tool_use", "id": "t5", "name": "Bash",
             "input": {"command": "pytest -q tests/test_totals.py"}}]}},
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "def main()"},
            {"type": "tool_result", "tool_use_id": "t5", "is_error": True,
             "content": "Exit code 1"}]}}]) + "\n")
    hook({"hook_event_name": "UserPromptSubmit", "prompt": "continue",
          "transcript_path": str(transcript)})
    added = [e["type"] for e in coding._events(sid)[1][before:]]
    assert "ToolCallStarted" not in added[:1] and added.count("ToolCallFinished") <= 1


def test_s15b_a_crashed_chat_leaves_unknown_outcomes_for_the_next_chat(coding, repo, tmp_path,
                                                                        monkeypatch):
    """The app was killed during a deploy: no result, no turn end. When the next chat
    starts, that call is unknown (not failed), and external calls are blocked."""
    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path / "home"))
    root = str(repo)
    hooks.handle("claude-code", {"hook_event_name": "UserPromptSubmit", "cwd": root,
                                 "session_id": "old", "prompt": "Deploy the fix."}, coding)
    hooks.handle("claude-code", {"hook_event_name": "PreToolUse", "cwd": root,
                                 "session_id": "old", "tool_name": "Bash",
                                 "tool_input": {"command": "kubectl apply -f prod.yaml"},
                                 "tool_use_id": "k1"}, coding)
    hooks.handle("claude-code", {"hook_event_name": "PreToolUse", "cwd": root,
                                 "session_id": "old", "tool_name": "Read",
                                 "tool_input": {"file_path": "x"}, "tool_use_id": "r1"},
                 coding)
    brief = hooks.handle("claude-code", {"hook_event_name": "SessionStart", "cwd": root,
                                         "session_id": "new"}, coding)
    assert "Outcome UNKNOWN" in brief and "kubectl apply" in brief
    sid = coding.latest_active(root)
    assert [u["tool"] for u in coding.unresolved(sid)] == ["Bash"]    # the read: no matter
    out, _ = hooks.fast("claude-code", {"hook_event_name": "PreToolUse", "cwd": root,
                                        "tool_name": "mcp__slack__post_message",
                                        "tool_input": {"text": "deployed"}})
    assert "deny" in out


def test_s15d_file_impact_outside_git_in_a_second_app(coding, tmp_path, monkeypatch):
    """A project that is not a git repository, worked on from Codex: every change a tool
    call makes is still observed (shadow snapshots), and a forgotten session takes its
    local snapshot store with it."""
    from continuum.coding import snapshot

    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path / "home"))
    work = tmp_path / "scripts"
    work.mkdir()
    (work / "sync.sh").write_text("echo old\n")
    root = str(work)

    def hook(payload):
        _, entry = hooks.fast("codex", {"cwd": root, "session_id": "cx-1", **payload})
        hooks.handle("codex", entry["payload"], coding, entry["snapshot"])

    coding.start(root, "maintain the sync scripts", agent="me")   # outside git: explicit
    hook({"hook_event_name": "UserPromptSubmit", "prompt": "Make sync.sh print new."})
    hook({"hook_event_name": "PreToolUse", "tool_name": "shell", "tool_use_id": "c1",
          "tool_input": {"command": "sed -i s/old/new/ sync.sh && touch done.flag"}})
    (work / "sync.sh").write_text("echo new\n")
    (work / "done.flag").write_text("")
    hook({"hook_event_name": "PostToolUse", "tool_name": "shell", "tool_use_id": "c1",
          "tool_input": {"command": "sed -i s/old/new/ sync.sh && touch done.flag"},
          "tool_response": {"output": "", "exit_code": 0}})
    hook({"hook_event_name": "Stop"})
    sid = coding.latest_active(root)
    work_log = coding.brief(sid)["work_log"]
    assert {f["path"] for f in work_log["files_changed"]} == {"sync.sh", "done.flag"}
    assert not (work / ".git").exists()
    store = snapshot.shadow_dir(work.resolve())
    assert store.is_dir()
    coding.forget(sid, actor="me", reason="test")
    assert not store.exists()


def test_s16_the_same_short_answer_in_two_apps_is_two_answers(coding, repo):
    """'yes' in Claude Code, and later 'yes' in Codex to a new question: two answers, each
    paired with its own question, whatever order the apps' transcripts were imported in."""
    root = str(repo)
    claude = [Message("user", "Build the export.", "u1", "2026-10-19T10:00:00+00:00"),
              Message("agent", "Should I also drop the `old_export` table?", "a1",
                      "2026-10-19T10:01:00+00:00"),
              Message("user", "yes", "u2", "2026-10-19T10:02:00+00:00")]
    codex = [Message("user", "Continue the export work.", "c1", "2026-10-19T11:00:00+00:00"),
             Message("agent", "Should I gzip the `nightly_dump` files?", "c2",
                     "2026-10-19T11:01:00+00:00"),
             Message("user", "yes", "c3", "2026-10-19T11:02:00+00:00"),
             Message("user", "Now add the CSV header row.", "c4", "2026-10-19T11:03:00+00:00")]
    # Codex's transcript is imported first: journal order is not conversation order
    coding.capture(root, codex, source="codex", chat_id="cx", history=True)
    coding.capture(root, claude, source="claude-code", chat_id="cc", history=True)
    md = coding.brief(coding.latest_active(root))["markdown"]
    assert '"Should I also drop the `old_export` table?" → "yes"' in md
    assert '"Should I gzip the `nightly_dump` files?" → "yes"' in md


def test_s15c_background_subagents_reports_and_hidden_test_failures(coding, repo, tmp_path,
                                                                     monkeypatch):
    """Trial 7 found: a background subagent's tool result is only a handle (its report
    comes with SubagentStop), and a piped test run exits 0 although tests failed."""
    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path / "home"))
    root = str(repo)

    def hook(payload):
        hooks.handle("claude-code", {"cwd": root, "session_id": "c", **payload}, coding)

    hook({"hook_event_name": "UserPromptSubmit", "prompt": "Find TODOs and run the tests."})
    hook({"hook_event_name": "UserPromptSubmit", "prompt": "<task-notification><task-id>ag9"
          "</task-id><status>completed</status></task-notification>"})
    hook({"hook_event_name": "PostToolUse", "tool_name": "Task", "tool_use_id": "t1",
          "tool_input": {"description": "List TODO comments"},
          "tool_response": {"agentId": "ag9", "isAsync": True, "status": "async_launched"}})
    hook({"hook_event_name": "SubagentStop", "agent_id": "ag9", "agent_type": "Explore",
          "last_assistant_message": "Found 2 TODOs: parse.py:14 (CHF), totals.py:20 (VAT)."})
    hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": "t2",
          "tool_input": {"command": "python -m pytest -q 2>&1 | tail -30"},
          "tool_response": {"stdout": "F...\n1 failed, 3 passed in 0.1s", "stderr": ""}})
    brief = coding.brief(coding.latest_active(root))
    [sub] = brief["work_log"]["subagents"]
    assert "parse.py:14" in sub["report"] and "agentId" not in sub["report"]
    [cmd] = brief["work_log"]["commands"]
    assert cmd["status"] == "reports_failures"
    assert [f["target"] for f in brief["work_log"]["failures"]] == [
        "python -m pytest -q 2>&1 | tail -30"]
    assert "output reports failures" in brief["markdown"]
    assert brief["user_requests"]["messages"] == 1          # the app's notice is not a turn


def test_s4c_the_spool_keeps_order_quarantines_bad_entries_and_waits_for_the_database(
        coding, repo, tmp_path, monkeypatch):
    """A spooled entry that cannot be applied is set aside (logged), and the entries
    after it still reach the journal; while the database is down, nothing is lost."""
    import psycopg

    from continuum.coding import spool

    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path / "home"))
    root = str(repo)
    for text in ("first request", "BREAK", "third request"):
        spool.write({"app": "claude-code", "snapshot": None, "payload": {
            "hook_event_name": "UserPromptSubmit", "cwd": root, "prompt": text}})
    real = hooks.handle

    def flaky(app, payload, c, snap=None):
        if payload.get("prompt") == "BREAK":
            raise ValueError("an entry the journal rejects")
        return real(app, payload, c, snap)

    monkeypatch.setattr(hooks, "handle", flaky)
    assert hooks.drain(coding) == 2
    assert [p.name for p in spool.pending()] == []
    assert len(list((spool.spool_dir() / "failed").glob("*.json"))) == 1
    assert _said(coding.brief(coding.latest_active(root))) == ["first request",
                                                                "third request"]
    assert "not applied" in (tmp_path / "home" / "hooks.log").read_text(encoding="utf-8")

    spool.write({"app": "claude-code", "snapshot": None, "payload": {
        "hook_event_name": "UserPromptSubmit", "cwd": root, "prompt": "later"}})

    def down(app, payload, c, snap=None):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(hooks, "handle", down)
    with pytest.raises(psycopg.OperationalError):
        hooks.drain(coding)
    assert len(spool.pending()) == 1                     # kept for the next drain
    monkeypatch.setattr(hooks, "handle", real)
    assert hooks.drain(coding) == 1
    assert _said(coding.brief(coding.latest_active(root)))[-1] == "later"
