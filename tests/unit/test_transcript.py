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
