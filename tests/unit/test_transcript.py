# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Transcript extraction is structural, so it works across apps and future versions."""

import json

from continuum.coding.transcript import Message, clean_user_text, extract, read_transcript


def _jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


CLAUDE_CODE = [
    {"type": "summary", "summary": "old chat", "leafUuid": "x"},
    {"type": "user", "uuid": "u0", "isMeta": True, "message": {
        "role": "user", "content": "<local-command-caveat>Caveat: ...</local-command-caveat>"}},
    {"type": "user", "uuid": "u1", "sessionId": "s1", "message": {
        "role": "user", "content": "Build the rate fetcher. It must cache results "
                                   "for ten minutes."}},
    {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "plan"},
        {"type": "text", "text": "I'll read the service first."},
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}]}},
    {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "def f(): ..."}]}},
    {"type": "assistant", "uuid": "a2", "isSidechain": True, "message": {
        "role": "assistant", "content": [{"type": "text", "text": "subagent chatter"}]}},
    {"type": "user", "uuid": "u3", "message": {"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>todo list is empty</system-reminder>"
                                 "Also: round every amount to two decimals."}]}},
    {"type": "user", "uuid": "u4", "isCompactSummary": True, "message": {
        "role": "user", "content": "This session is being continued from a previous ..."}},
    {"type": "assistant", "uuid": "a3", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Part 1 done. Next: the cache."}]}},
    {"type": "api-request-blob", "hash": "x1", "message": {"role": "user",
                                                         "content": "Build the rate fetcher."}},
]

CODEX = [
    {"type": "session_meta", "payload": {"id": "th1", "cwd": "/w", "instructions": "x"}},
    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "<user_instructions>AGENTS.md rules</user_instructions>"}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "<environment_context><cwd>/w</cwd>"
                                       "</environment_context>"}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "Add the CLI command."}]}},
    {"type": "event_msg", "payload": {"type": "user_message", "message": "Add the CLI command.",
                                      "kind": "plain"}},
    {"type": "response_item", "payload": {"type": "reasoning", "summary": [
        {"type": "summary_text", "text": "thinking about it"}]}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
                                          "arguments": "{\"cmd\": [\"ls\"]}"}},
    {"type": "response_item", "payload": {"type": "function_call_output", "output": "a.py"}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "The CLI is added; tests pass."}]}},
    {"type": "event_msg", "payload": {"type": "agent_message",
                                      "message": "The CLI is added; tests pass."}},
]


def test_claude_code_transcript_keeps_only_what_the_person_typed(tmp_path):
    msgs = read_transcript(_jsonl(tmp_path / "c.jsonl", CLAUDE_CODE))
    assert [Message(m.role, m.text) for m in msgs if m.role == "user"] == [
        Message("user", "Build the rate fetcher. It must cache results for ten minutes."),
        Message("user", "Also: round every amount to two decimals.")]
    agent = [m.text for m in msgs if m.role == "agent"]
    assert agent[-1] == "Part 1 done. Next: the cache."
    assert "subagent chatter" not in agent and "plan" not in agent


def test_codex_rollout_without_injected_context_or_duplicates(tmp_path):
    msgs = [Message(m.role, m.text) for m in
            read_transcript(_jsonl(tmp_path / "rollout.jsonl", CODEX))]
    assert msgs == [Message("user", "Add the CLI command."),
                    Message("agent", "The CLI is added; tests pass.")]


def test_gemini_and_generic_chat_exports(tmp_path):
    gemini = tmp_path / "chat.json"
    gemini.write_text(json.dumps([{"role": "user", "parts": [{"text": "use sqlite"}]},
                                  {"role": "model", "parts": [{"text": "ok, sqlite"}]}]))
    assert [Message(m.role, m.text) for m in read_transcript(gemini)] == [
        Message("user", "use sqlite"), Message("agent", "ok, sqlite")]
    generic = extract([{"messages": [{"role": "system", "content": "be terse"},
                                     {"role": "user", "content": "q"},
                                     {"role": "assistant", "content": "a"}]}])
    assert generic == [Message("user", "q"), Message("agent", "a")]


def test_unknown_future_wrappers_and_garbage_degrade_gracefully(tmp_path):
    path = tmp_path / "future.jsonl"
    path.write_text("not json\n{\"partial\": \n" + json.dumps(
        {"kind": "turn", "data": {"msg": {"role": "User", "content": [
            {"type": "text", "text": "future format"}, {"type": "image", "url": "x"}]}}})
        + "\n\n")
    assert [Message(m.role, m.text) for m in read_transcript(path)] == [
        Message("user", "future format")]
    assert read_transcript(_jsonl(tmp_path / "empty.jsonl", [])) == []


def test_injected_blocks_are_removed_but_real_xml_is_kept():
    assert clean_user_text("<system-reminder>x</system-reminder>  fix <div> layout") == \
        "fix <div> layout"
    assert clean_user_text("<environment_context>cwd</environment_context>") == ""
    assert clean_user_text("<config><a>1</a></config>") == "<config><a>1</a></config>"


def test_a_turn_end_reads_only_the_newest_part_of_a_long_transcript(tmp_path):
    records = [{"role": "user", "content": f"old request {i} " + "x" * 200} for i in range(500)]
    records.append({"role": "assistant", "content": "final report"})
    path = _jsonl(tmp_path / "long.jsonl", records)
    tail = read_transcript(path, tail_bytes=4096)
    assert (tail[-1].role, tail[-1].text) == ("agent", "final report")
    assert 0 < len(tail) < 30 and "old request 0 " not in tail[0].text
    assert len(read_transcript(path)) == 501


def _acts(envs):
    return [(e["phase"], e.get("tool"), e.get("status"), e.get("effect")) for e in envs]


def test_claude_code_tool_calls_with_results_subagents_and_compaction():
    """Every tool call with its result (D-042), paired by the app's call id."""
    from continuum.coding.transcript import activity

    records = [
        {"type": "assistant", "uuid": "a1", "timestamp": "t1", "message": {
            "role": "assistant", "content": [
                {"type": "text", "text": "Fixing the parser."},
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/r/a.py"}},
                {"type": "tool_use", "id": "t2", "name": "Bash",
                 "input": {"command": "pytest -q"}},
                {"type": "tool_use", "id": "t3", "name": "mcp__github__create_issue",
                 "input": {"title": "bug"}},
                {"type": "tool_use", "id": "t4", "name": "Skill",
                 "input": {"skill": "pdf"}}]}},
        {"type": "user", "uuid": "u1", "timestamp": "t2", "toolUseResult": {
            "stdout": "", "stderr": "1 failed", "interrupted": False},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
              "content": "Exit code 1\nFAILED test_a"}]}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "def f(): ..."},
            {"type": "tool_result", "tool_use_id": "t3", "is_error": True,
             "content": "The user doesn't want to proceed with this tool use."}]}},
        {"type": "assistant", "uuid": "s1", "isSidechain": True, "agentId": "ag1",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t5", "name": "Grep", "input": {"pattern": "x"}}]}},
        {"type": "system", "subtype": "compact_boundary", "uuid": "c0",
         "compactMetadata": {"trigger": "auto", "preTokens": 150000}},
        {"type": "user", "uuid": "c1", "isCompactSummary": True, "message": {
            "role": "user", "content": "Summary: parser fixed, tests pending."}},
        {"type": "api-request-blob", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "rm -rf"}}]}},
    ]
    envs = activity(records, app="claude-code", chat="c")
    acts = _acts(envs)
    assert ("start", "Read", None, "read") in acts and ("finish", "Read", "ok", "read") in acts
    assert ("finish", "Bash", "error", "write") in acts
    assert ("finish", "mcp__github__create_issue", "denied", "external") in acts
    assert ("start", "Skill", None, "read") in acts
    assert not any(e.get("call_id") == "t9" for e in envs)        # request copies skipped
    [bash] = [e for e in envs if e.get("tool") == "Bash" and e["phase"] == "finish"]
    assert "FAILED test_a" in bash["output"] and "1 failed" in bash["output"]
    [grep] = [e for e in envs if e.get("tool") == "Grep"]
    assert grep["agent_id"] == "ag1"                               # a subagent's own call
    compact = [e for e in envs if e["phase"] == "compact"]
    assert [c["when"] for c in compact] == ["before", "after"]
    assert compact[1]["summary"].startswith("Summary: parser fixed")
    skill = next(e for e in envs if e.get("tool") == "Skill")
    assert skill["kind"] == "skill" and skill["skill"] == "pdf"
    again = activity(records, app="claude-code", chat="c")
    assert [e.get("call_key") for e in again] == [e.get("call_key") for e in envs]


def test_codex_and_gemini_tool_calls_with_results():
    from continuum.coding.transcript import activity

    codex = [
        {"type": "response_item", "timestamp": "t1", "payload": {
            "type": "function_call", "name": "shell", "call_id": "c1",
            "arguments": json.dumps({"command": ["bash", "-lc", "make test"]})}},
        {"type": "event_msg", "timestamp": "t2", "payload": {
            "type": "exec_command_end", "call_id": "c1", "exit_code": 2,
            "aggregated_output": "boom"}},
        {"type": "response_item", "timestamp": "t3", "payload": {
            "type": "custom_tool_call", "name": "apply_patch", "call_id": "c2",
            "input": "*** Begin Patch\n*** Update File: src/x.py\n*** End Patch"}},
        {"type": "event_msg", "timestamp": "t4", "payload": {
            "type": "patch_apply_end", "call_id": "c2", "success": True, "changes": {}}},
        {"type": "response_item", "timestamp": "t5", "payload": {
            "type": "function_call", "name": "shell", "call_id": "c3",
            "arguments": json.dumps({"command": ["git", "push", "origin", "main"]})}},
        {"type": "compacted", "timestamp": "t6", "payload": {"message": "summary"}},
    ]
    envs = activity(codex, app="codex", chat="th")
    acts = _acts(envs)
    assert ("finish", "shell", "error", "write") in acts           # exit 2
    assert ("finish", "apply_patch", "ok", "write") in acts
    assert ("start", "shell", None, "external") in acts             # git push: no result
    assert not any(e.get("call_id") == "c3" and e["phase"] == "finish" for e in envs)
    [out] = [e for e in envs if e.get("call_id") == "c1" and e["phase"] == "finish"]
    assert out["output"] == "boom" and out["exit_code"] == 2
    gemini = [{"role": "model", "parts": [
        {"functionCall": {"name": "run_shell_command", "args": {"command": "npm test"}}}]},
        {"role": "user", "parts": [{"functionResponse": {
            "name": "run_shell_command", "response": {"output": "ok", "exit_code": 0}}}]}]
    assert _acts(activity(gemini, app="gemini-cli", chat=None)) == [
        ("start", "run_shell_command", None, "write"),
        ("finish", "run_shell_command", "ok", "write")]
