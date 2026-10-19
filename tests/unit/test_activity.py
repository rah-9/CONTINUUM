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
    assert snapshot.take(tmp_path / "not-git" if (tmp_path / "not-git").mkdir() is None
                         else tmp_path) is None
