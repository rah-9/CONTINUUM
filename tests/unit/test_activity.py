# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Every app's tool-call hooks become the same records, and each tool is classified by
what it can affect (D-042)."""

import subprocess

import pytest

from continuum.coding import activity, snapshot
from continuum.coding.activity import classify, command_effect, from_hook


@pytest.mark.parametrize(("command", "effect"), [
    ("pytest -q", "write"),
    ("ruff check . && mypy src", "write"),
    ("git status && git push origin main", "external"),
    ("cd app; npm publish", "external"),
    ("FOO=1 kubectl apply -f k8s/", "external"),
    ("curl -s https://api.example.com/rates", "write"),
    ("curl -X POST https://api.example.com/rates -d '{}'", "external"),
    ("gh pr view 12", "write"),
    ("gh pr create --fill", "external"),
    ("terraform plan", "write"),
    ("terraform apply -auto-approve", "external"),
    ("aws s3 ls", "write"),
    ("aws s3 cp out.txt s3://bucket/", "external"),
    ("psql -c 'delete from users'", "external"),
    ("sudo docker push img:1", "external"),
    ("ssh prod 'systemctl restart api'", "external"),
])
def test_shell_commands_are_classified_by_what_they_can_reach(command, effect):
    assert command_effect(command) == effect


def test_tools_of_every_app_are_classified():
    assert classify("Read")["effect"] == "read"
    assert classify("Edit")["effect"] == "write"
    assert classify("apply_patch")["effect"] == "write"
    assert classify("Bash", {"command": "git push"}) == {
        "kind": "shell", "server": None, "effect": "external"}
    assert classify("mcp__github__list_issues") == {
        "kind": "mcp", "server": "github", "effect": "read"}
    assert classify("mcp__github__create_issue")["effect"] == "external"
    assert classify("mcp__plugin_slack_slack__post_message")["server"] == "slack_slack"
    assert classify("mcp_jira_getIssue") == {"kind": "mcp", "server": "jira",
                                             "effect": "read"}
    assert classify("Task") == {"kind": "subagent", "server": None, "effect": "delegate"}
    assert classify("Skill")["kind"] == "skill"
    assert classify("some_new_tool")["effect"] == "external"      # unknown: conservative


def test_claude_code_and_codex_hooks():
    pre = from_hook("claude-code", {
        "hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"}, "tool_use_id": "toolu_1"})
    post = from_hook("claude-code", {
        "hook_event_name": "PostToolUse", "session_id": "s", "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"}, "tool_use_id": "toolu_1",
        "duration_ms": 812, "tool_response": {"stdout": "3 passed", "stderr": "",
                                              "interrupted": False}})
    fail = from_hook("claude-code", {
        "hook_event_name": "PostToolUseFailure", "session_id": "s", "tool_name": "Bash",
        "tool_input": {"command": "make"}, "tool_use_id": "toolu_2",
        "error": "Exit code 2: no rule", "is_interrupt": False})
    assert pre is not None and post is not None and fail is not None
    assert pre["phase"] == "start" and pre["call_key"] == post["call_key"]
    assert (post["status"], post["output"], post["duration_ms"]) == ("ok", "3 passed", 812)
    assert fail["status"] == "error" and "no rule" in fail["error"]
    edit = from_hook("codex", {
        "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_use_id": "e1",
        "tool_input": {"file_path": "a.py"},
        "tool_response": {"filePath": "a.py", "originalFile": "x" * 100_000,
                          "structuredPatch": [{"lines": ["-a", "+b"]}]}})
    assert edit is not None and "originalFile" not in edit["output"]   # no file copies
    mcp = from_hook("claude-code", {
        "hook_event_name": "PreToolUse", "tool_name": "mcp__linear__create_issue",
        "tool_input": {"title": "x"}, "tool_use_id": "m1"})
    assert mcp is not None and (mcp["kind"], mcp["server"], mcp["effect"]) == (
        "mcp", "linear", "external")
    assert from_hook("claude-code", {"hook_event_name": "UserPromptSubmit"}) is None
    sub = from_hook("claude-code", {"hook_event_name": "SubagentStop", "agent_id": "a1",
                                    "agent_transcript_path": "/x.jsonl",
                                    "last_assistant_message": "done"})
    assert sub is not None and sub["phase"] == "subagent_stop" and sub["report"] == "done"
    compact = from_hook("claude-code", {"hook_event_name": "PostCompact",
                                        "compact_summary": "we did X"})
    assert compact is not None and compact["summary"] == "we did X"


def test_gemini_and_cursor_hooks():
    after = from_hook("gemini-cli", {
        "hook_event_name": "AfterTool", "tool_name": "run_shell_command",
        "tool_input": {"command": "npm test"},
        "tool_response": {"llmContent": "Tests: 1 failed", "error": "exit 1"}})
    assert after is not None and after["status"] == "error"
    shell = from_hook("cursor", {"hook_event_name": "afterShellExecution",
                                 "conversation_id": "cv", "command": "git push",
                                 "output": "rejected"})
    assert shell is not None and shell["effect"] == "external" and shell["output"] == "rejected"
    edit = from_hook("cursor", {"hook_event_name": "afterFileEdit", "file_path": "b.py",
                                "edits": [{"old_string": "a", "new_string": "b"}]})
    assert edit is not None and edit["phase"] == "start_finish" and edit["effect"] == "write"
    mcp = from_hook("cursor", {"hook_event_name": "afterMCPExecution", "tool_name": "query",
                               "tool_input": "{\"sql\": \"select 1\"}",
                               "result_json": {"rows": 1}})
    assert mcp is not None and mcp["kind"] == "mcp" and "rows" in mcp["output"]


def test_status_is_derived_not_guessed():
    assert activity.status_of(is_error=True, text="Permission denied by user") == "denied"
    assert activity.status_of(is_error=False, text="[Request interrupted by user]") == \
        "interrupted"
    assert activity.status_of(is_error=False, text="ok", exit_code=3) == "error"
    assert activity.status_of(is_error=False, text="ok", exit_code=0) == "ok"


def test_snapshots_give_the_exact_diff_of_any_change(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    before = snapshot.take(repo)
    (repo / "a.py").write_text("x = 2\n")                   # changed by "a shell command"
    (repo / "new.txt").write_text("hello\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.pyc").write_bytes(b"\0")    # noise: never a change
    after = snapshot.take(repo)
    assert before and after and before["tree"] != after["tree"]
    found = snapshot.diff(repo, before["tree"], after["tree"])
    assert found is not None
    assert {(f["path"], f["status"], f["added"], f["deleted"]) for f in found["files"]} == {
        ("a.py", "modified", 1, 1), ("new.txt", "added", 1, 0)}
    assert "+x = 2" in found["patch"]
    assert snapshot.diff(repo, after["tree"], after["tree"])["files"] == []  # type: ignore[index]


def test_directories_outside_git_are_snapshotted_in_a_private_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path / "home"))
    work = tmp_path / "notes"
    work.mkdir()
    (work / "a.py").write_text("x = 1\n")
    (work / "old.txt").write_text("bye\n")
    (work / "node_modules").mkdir()
    (work / "node_modules" / "dep.js").write_text("1")          # never project content
    before = snapshot.take(work)
    assert before is not None and before["git_dir"].startswith(str(tmp_path / "home"))
    (work / "a.py").write_text("x = 2\n")
    (work / "old.txt").unlink()
    (work / "new.md").write_text("hi\n")
    after = snapshot.take(work)
    assert after is not None
    found = snapshot.diff(after["root"], before["tree"], after["tree"], after["git_dir"])
    assert found is not None
    assert {(f["path"], f["status"]) for f in found["files"]} == {
        ("a.py", "modified"), ("old.txt", "deleted"), ("new.md", "added")}
    assert not (work / ".git").exists()                       # the user's folder untouched
    monkeypatch.setattr(snapshot, "SHADOW_MAX_FILES", 1)       # too big: no snapshot
    assert snapshot.take(work) is None
    assert snapshot.take(work.anchor) is None                  # never a filesystem root


def test_outputs_that_report_failures_despite_exit_0():
    """Trial 7: `pytest -q | tail -30` exits 0 although tests failed."""
    from continuum.coding.recorder import signals

    assert signals("== 1 failed, 3 passed in 0.21s ==") == ["tests_failed"]
    assert signals("FAILED tests/test_a.py::test_x - AssertionError") == ["tests_failed"]
    assert signals("Tests: 2 failed, 10 passed") == ["tests_failed"]
    assert signals("Traceback (most recent call last):\n  File x") == ["traceback"]
    assert signals("3 passed, 0 failed") == [] and signals("all good") == []


@pytest.mark.parametrize(("command", "effect"), [
    ("git -C /srv/app push", "external"),
    ("git -c user.name=x push origin main", "external"),
    ('bash -c "git push"', "external"),
    ('bash -lc "make && git push origin main"', "external"),
    ("command git push", "external"),
    ("sudo -E kubectl apply -f k8s/", "external"),
    ("sudo -u deploy kubectl apply -f k8s/", "external"),
    ("npx vercel deploy --prod", "external"),
    ("python -m twine upload dist/*", "external"),
    ("(cd app && git push)", "external"),
    ("git -C /srv/app status", "write"),
    ('bash -c "pytest -q"', "write"),
    ("npx prettier --write .", "write"),
    ("sudo ls /root", "write"),
])
def test_wrapped_commands_are_seen_through(command, effect):
    """Review: `git -C x push`, `bash -c "..."`, `sudo -E`, `npx`, `python -m` hid the
    command that acts outside the repository."""
    assert command_effect(command) == effect


@pytest.mark.parametrize(("tool", "effect"), [
    ("mcp__github__merge_pull_request", "external"),    # starts with "me"
    ("mcp__chat__message_send", "external"),
    ("mcp__git__checkout_branch", "external"),           # starts with "check"
    ("mcp__x__listen_and_reply", "external"),            # starts with "list"
    ("mcp__x__getOrCreate", "external"),
    ("mcp__x__get_or_create_user", "external"),
    ("mcp__github__get_issue", "read"),
    ("mcp__x__getIssue", "read"),
    ("mcp__x__list_files", "read"),
    ("mcp__x__me", "read"),
    ("MCP:create_issue", "external"),                    # Cursor's naming
    ("MCP:list_files", "read"),
])
def test_mcp_tools_are_reads_only_when_their_whole_name_says_so(tool, effect):
    assert classify(tool)["effect"] == effect


def test_cursor_hooks_as_documented():
    pre = from_hook("cursor", {"hook_event_name": "preToolUse", "tool_name": "Shell",
                               "tool_input": {"command": "git push"}, "tool_use_id": "u1",
                               "conversation_id": "cv", "workspace_roots": ["/p"]})
    post = from_hook("cursor", {"hook_event_name": "postToolUse", "tool_name": "Shell",
                                "tool_input": {"command": "git push"}, "tool_use_id": "u1",
                                "tool_output": '{"exitCode":1,"stdout":"rejected"}',
                                "duration": 5432})
    assert pre is not None and post is not None
    assert pre["effect"] == "external" and pre["call_key"] == post["call_key"]
    assert (post["status"], post["exit_code"], post["duration_ms"]) == ("error", 1, 5432)
    assert "rejected" in post["output"]
    timeout = from_hook("cursor", {"hook_event_name": "postToolUseFailure",
                                   "tool_name": "Shell", "tool_use_id": "u2",
                                   "tool_input": {"command": "terraform apply"},
                                   "error_message": "Command timed out after 30s",
                                   "failure_type": "timeout"})
    assert timeout is not None and timeout["failure_type"] == "timeout"
    assert timeout["status"] == "error" and "timed out" in timeout["error"]
    mcp = from_hook("cursor", {"hook_event_name": "beforeMCPExecution",
                               "tool_name": "create_issue", "mcp_server_name": "linear",
                               "tool_input": "{\"title\": \"x\"}"})
    assert mcp is not None and (mcp["server"], mcp["effect"]) == ("linear", "external")
    sub = from_hook("cursor", {"hook_event_name": "subagentStop", "task": "audit auth",
                               "summary": "auth is fine", "subagent_type": "explore"})
    assert sub is not None and sub["report"] == "auth is fine" and sub["agent_id"]
    shell = from_hook("cursor", {"hook_event_name": "afterShellExecution",
                                 "command": "ls", "output": "a b"})
    assert shell is not None and shell["status"] == "ok"   # no exit code is not a failure


def test_claude_code_subagent_handback_is_a_report_not_an_external_call():
    env = from_hook("claude-code", {"hook_event_name": "PreToolUse",
                                    "tool_name": "SubagentHandback", "agent_id": "a7",
                                    "tool_input": {"message": "Found 3 callers."}})
    assert env is not None and env["phase"] == "subagent_stop"
    assert (env["agent_id"], env["report"]) == ("a7", "Found 3 callers.")
    assert from_hook("claude-code", {"hook_event_name": "PostToolUse",
                                     "tool_name": "SubagentHandback",
                                     "tool_input": {"message": "x"}}) is None
    mcp = from_hook("claude-code", {"hook_event_name": "PreToolUse",
                                    "tool_name": "mcp__plugin_x_y__create_issue",
                                    "mcp_server": {"name": "linear", "source": "plugin"},
                                    "tool_input": {}, "tool_use_id": "m"})
    assert mcp is not None and mcp["server"] == "linear"
    assert classify("TaskCreate")["effect"] == "read"          # harness bookkeeping


def test_each_app_gets_its_own_refusal_and_context_format():
    import json

    from continuum.coding import hooks

    claude = json.loads(hooks.deny_output("claude-code", "no"))
    assert claude["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert json.loads(hooks.deny_output("codex", "no"))["hookSpecificOutput"][
        "permissionDecision"] == "deny"
    assert json.loads(hooks.deny_output("gemini-cli", "no")) == {"decision": "deny",
                                                                 "reason": "no"}
    assert json.loads(hooks.deny_output("cursor", "no"))["permission"] == "deny"
    # Cursor blocks a permission hook whose output is not JSON: allow explicitly
    assert json.loads(hooks.allow_output("cursor", "pretooluse")) == {"permission": "allow"}
    assert json.loads(hooks.allow_output("cursor", "beforesubmitprompt")) == {
        "continue": True}
    assert hooks.allow_output("claude-code", "pretooluse") == ""
    assert hooks.context_output("claude-code", "brief") == "brief"
    assert json.loads(hooks.context_output("gemini-cli", "brief"))["hookSpecificOutput"][
        "additionalContext"] == "brief"
    assert json.loads(hooks.context_output("cursor", "brief")) == {
        "additional_context": "brief"}
    assert hooks._cwd({"workspace_roots": ["/work/app"]}) == "/work/app"


def test_secret_files_are_listed_but_their_content_is_never_in_a_diff(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "app.py").write_text("x = 1\n")
    before = snapshot.take(repo)
    (repo / ".env").write_text("DB_PASSWORD=hunter2-very-secret\n")
    (repo / "app.py").write_text("x = 2\n")
    after = snapshot.take(repo)
    assert before and after
    found = snapshot.diff(repo, before["tree"], after["tree"])
    assert found is not None
    assert {f["path"] for f in found["files"]} == {".env", "app.py"}
    assert "hunter2" not in found["patch"] and "+x = 2" in found["patch"]
    assert "content not stored" in found["patch"]


def test_the_drain_lock_is_held_by_one_process_and_freed_by_the_kernel(tmp_path,
                                                                        monkeypatch):
    from continuum.coding import spool

    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path))
    first, second = spool.Lock(), spool.Lock()
    assert first.acquire() and not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
    code = ("import os,sys; os.environ['CONTINUUM_HOME']=sys.argv[1]; "
            "from continuum.coding import spool; l=spool.Lock(); "
            "assert l.acquire(); os._exit(0)")                 # dies holding the lock
    import sys

    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True)
    assert spool.Lock().acquire()                              # never stale


def test_setup_installs_hooks_for_codex_gemini_and_cursor(tmp_path):
    import json

    from continuum.coding import install

    out = install.setup(str(tmp_path))
    codex = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    start = codex["hooks"]["SessionStart"][0]["hooks"][0]
    assert start["command"] == "continuum hook codex" and start["additionalContextLimit"]
    assert codex["hooks"]["PreToolUse"][0]["matcher"] == "*"
    gemini = json.loads((tmp_path / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert gemini["hooks"]["BeforeTool"][0]["hooks"][0]["timeout"] == 15_000   # ms
    assert gemini["hooks"]["BeforeTool"][0]["matcher"] == ".*"
    cursor = json.loads((tmp_path / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert cursor["version"] == 1
    assert cursor["hooks"]["preToolUse"] == [{"command": "continuum hook cursor",
                                              "timeout": 15}]
    assert out["files"][".cursor/hooks.json"] == "created"
    again = install.setup(str(tmp_path))                        # idempotent
    assert set(again["files"].values()) <= {"unchanged",
                                           "unchanged (already imports AGENTS.md)"}


def test_erasing_a_shadow_store_removes_read_only_git_objects(tmp_path, monkeypatch):
    """Windows CI: git writes objects read-only and Windows refuses to delete them, so
    `forget` left the snapshot store (file contents) behind."""
    import os
    import stat

    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path / "home"))
    work = tmp_path / "notes"
    work.mkdir()
    (work / "a.txt").write_text("private\n")
    assert snapshot.take(work) is not None
    store = snapshot.shadow_dir(work.resolve())
    assert any(p.is_file() and not p.stat().st_mode & stat.S_IWRITE
               for p in (store / "objects").rglob("*"))           # read-only objects
    real_unlink = os.unlink

    def windows_unlink(path, *, dir_fd=None):                      # Windows semantics
        mode = os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode
        if not mode & stat.S_IWRITE:
            raise PermissionError(13, "Access is denied", str(path))
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", windows_unlink)
    assert snapshot.remove_shadow(work.resolve()) and not store.exists()


@pytest.mark.parametrize(("command", "expected"), [
    ("git push", (None, [])),
    ("git push origin main", ("origin", [("main", "main")])),
    ("git push -u origin feature/x", ("origin", [("feature/x", "feature/x")])),
    ("git push origin HEAD:refs/heads/release", ("origin", [("HEAD", "release")])),
    ("git -C /srv/app push --force-with-lease origin main", ("origin", [("main", "main")])),
    ("pytest -q && git push origin main", ("origin", [("main", "main")])),
    ("git push --tags", None),              # tags cannot be proven by a branch
    ("git push origin :old-branch", None),  # a deletion
    ("git status", None),
])
def test_push_commands_are_parsed_for_a_read_only_probe(command, expected):
    from continuum.coding.reconcile import push_targets

    assert push_targets(command) == expected


def test_a_probe_never_runs_with_injected_options(tmp_path):
    from continuum.coding.reconcile import probe_push

    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert probe_push(repo, "git push --upload-pack=touch_pwned origin main") is None
    assert probe_push(repo, "git push 'origin;rm' main") is None
    assert not (tmp_path / "pwned").exists()
