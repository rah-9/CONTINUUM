# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Conversation consolidation (coding/memory.py): rules and invariants."""

from hypothesis import given, settings
from hypothesis import strategies as st

from continuum.coding import memory
from continuum.coding.memory import Turn, classify, consolidate, prose, readable


def _chat(*messages: tuple[str, str]) -> list[Turn]:
    return [Turn(i, role, text, f"t{i}", "app", "c") for i, (role, text) in
            enumerate(messages, start=1)]


def _ledger(turns: list[Turn]) -> list[str]:
    return [i["text"] for i in consolidate(turns)["ledger"]]


def test_requirements_from_the_middle_of_a_long_chat_are_kept():
    turns = _chat(("user", "Build the rates service."),
                  *[("user", f"Rename helper {i}?") for i in range(20)],
                  ("user", "Looks good. Uploads must be limited to 20 MB. Also check tests?"),
                  *[("user", "continue") for _ in range(20)],
                  ("user", "Now add the CLI."))
    assert _ledger(turns) == ["Uploads must be limited to 20 MB."]
    m = consolidate(turns)
    assert m["latest"]["text"] == "Now add the CLI." and m["first"]["n"] == 1


def test_pasted_output_is_never_a_requirement():
    text = ("I got this, can you look?\nTraceback (most recent call last):\n"
            "  File \"x.py\", line 1\nValueError: config must define KEY\n"
            "note: this message must not be ignored\n\nThe fix must keep the API stable.")
    assert prose(text) == ["I got this, can you look?", "The fix must keep the API stable."]
    assert "[4 pasted line(s) omitted]" in readable(text)
    turns = _chat(("user", "Start."), ("user", text), ("user", "next"))
    assert _ledger(turns) == ["The fix must keep the API stable."]
    assert _ledger(_chat(("user", "Start."), ("user", "```\nmust never\n```\nok"),
                         ("user", "x"))) == []


def test_classification():
    assert classify("Never log the `ssn` field.") == ("requirement", 2)
    assert classify("Actually, use port 8431 instead of 8080.") == ("revision", 3)
    assert classify("Page size: 25 items.") == ("requirement", 1)
    assert classify("I'd like the output in YAML.") == ("requirement", 1)
    assert classify("Use tabs, not spaces.") == ("requirement", 1)
    assert classify("Timeouts are 500 ms across services.")[1] >= 1  # type: ignore[index]
    assert classify("Store money as integer cents.") == ("statement", 0)
    for chatter in ("Looks good so far.", "thanks", "fix that please", "What does x do?"):
        assert classify(chatter) is None, chatter


def test_short_answers_are_paired_with_the_question_they_answer():
    turns = _chat(("user", "Clean up the schema."),
                  ("agent", "Done. Should I also drop the legacy `v1_exports` table?"),
                  ("user", "yes"),
                  ("agent", "Dropped."),
                  ("user", "Now the CLI."))
    [item] = consolidate(turns)["ledger"]
    assert item["kind"] == "answer" and "v1_exports" in item["text"] and '"yes"' in item["text"]
    latest = consolidate(turns[:3])["latest"]
    assert latest["text"] == "yes" and "v1_exports" in latest["in_reply_to"]


def test_a_long_message_starting_with_ok_is_not_an_answer():
    """Trial 6: "OK, I'm out of time. Stop here, ..." was shown as the reply to the agent's
    open question."""
    turns = _chat(("user", "Find the bug."),
                  ("agent", "Found it. Should the VAT be rounded per line or on the total?"),
                  ("user", "OK, I'm out of time. Stop here, the next model will fix it all. "
                           "Keep the signature unchanged."))
    latest = consolidate(turns)["latest"]
    assert "in_reply_to" not in latest and latest["ends_chat"]
    assert "in_reply_to" in consolidate(_chat(*turns_of(turns[:2]), ("user", "ok, do it")))[
        "latest"]


def turns_of(turns):
    return [(t.role, t.text) for t in turns]


def test_agent_decisions_are_kept_as_claims():
    turns = _chat(("user", "Build it."),
                  ("agent", "Implemented. I chose SQLite WAL mode because writes overlap."),
                  ("user", "ok"))
    [d] = consolidate(turns)["agent_decisions"]
    assert "WAL mode" in d["text"]


def test_repeated_requirements_appear_once_at_their_newest_position():
    turns = _chat(("user", "Start."), ("user", "Port must be 8080."), ("user", "Other."),
                  ("user", "Port must be 8080."), ("user", "done"))
    [item] = consolidate(turns)["ledger"]
    assert item["n"] == 4


def test_the_budget_grows_with_the_chat_but_is_capped():
    assert memory.ledger_budget(10) == memory.LEDGER_CHARS
    assert memory.ledger_budget(80) > memory.LEDGER_CHARS
    assert memory.ledger_budget(10_000) == memory.LEDGER_MAX_CHARS


messages = st.lists(st.tuples(st.sampled_from(["user", "agent"]),
                              st.text(min_size=0, max_size=400)), min_size=0, max_size=120)


@settings(max_examples=150, deadline=None)
@given(messages)
def test_invariants_hold_for_any_conversation(msgs):
    turns = _chat(*msgs)
    m = consolidate(turns)
    users = [t for t in turns if t.role == "user"]
    assert m["messages"] == len(users)
    assert m == consolidate(turns)                                  # deterministic
    if users:
        assert m["latest"]["n"] == len(users)                       # newest always shown
        whole = " ".join(users[-1].text.split())
        assert m["latest"]["text"] == whole or m["latest"]["clipped"]
    numbers = [(i["n"], i["pos"]) for i in m["ledger"]]
    assert numbers == sorted(numbers)                               # conversation order
    used = sum(len(i["text"]) + 12 for i in m["ledger"])
    assert used <= memory.ledger_budget(len(users))                 # within budget
    assert all(len(i["text"]) <= memory.ITEM_CHARS + 2 for i in m["ledger"])
    assert all(0 < i["n"] < max(len(users), 1) for i in m["ledger"])  # latest not repeated


def test_a_message_that_ends_the_chat_is_flagged():
    stop = "OK, I'm out of time. Stop here; the next model will do the export."
    m = consolidate(_chat(("user", "Build it."), ("user", stop)))
    assert m["latest"]["ends_chat"] is True
    for text in ("Don't stop the server.", "Add a stop button.", "continue"):
        assert memory.ends_chat(text) is False, text


def _supersede(*items: tuple[str, str]) -> list[tuple[int, int | None, int | None]]:
    c = [{"n": i, "pos": 0, "kind": k, "text": t} for i, (k, t) in enumerate(items, 1)]
    memory.supersede(c)
    return [(x["n"], x.get("replaced_by"), x.get("maybe_replaced_by")) for x in c]


def test_a_changed_value_replaces_only_the_requirement_about_the_same_thing():
    """Review: "port 5000 instead of 4000" struck "the timeout must be 4000 ms"."""
    assert _supersede(("requirement", "Build the server on port 4000"),
                      ("requirement", "The HTTP timeout must be 4000 ms"),
                      ("revision", "run the API on port 5000 instead of 4000")) == [
        (1, 3, None), (2, None, 3), (3, None, None)]
    # one shared word is only a possible replacement: shown, never struck out
    assert _supersede(("requirement", "cache results in Redis"),
                      ("revision", "store sessions in Postgres instead of Redis")) == [
        (1, None, 2), (2, None, None)]
    assert _supersede(("requirement", "cache results in Redis"),
                      ("revision", "cache results in Memcached instead of Redis")) == [
        (1, 2, None), (2, None, None)]
    assert _supersede(("requirement", "use `old_export` for the CSV"),
                      ("revision", "use `new_export` instead of `old_export`"))[0][1] == 2


def test_the_agents_words_in_an_answer_never_replace_a_requirement():
    """Review: the agent asked "keep sessions in Postgres instead of Redis?", the user
    said "no", and the Redis requirement was struck out by the agent's question."""
    assert _supersede(
        ("requirement", "Requirement: cache results in Redis"),
        ("answer", 'asked "Should I keep sessions in Postgres instead of Redis?" → "no"'),
    ) == [(1, None, None), (2, None, None)]


def test_the_first_request_can_be_replaced_too():
    out = consolidate(_chat(("user", "Build the server on port 4000."),
                            ("agent", "Done."),
                            ("user", "Use port 5000 instead of 4000 for the server."),
                            ("agent", "Changed."), ("user", "Thanks, all good.")))
    assert out["first"]["replaced_by"] == 2


def _call(seq, key, tool, target, status, *, kind="shell", signals=()):
    start = {"seq": seq, "type": "ToolCallStarted", "at": "t", "p": {
        "call_key": key, "tool": tool, "kind": kind, "effect": "write", "source": "app",
        "input": {"command": target} if kind == "shell" else {"file_path": target}}}
    finish = {"seq": seq + 1, "type": "ToolCallFinished", "at": "t", "p": {
        "call_key": key, "tool": tool, "status": status, "exit_code": 1 if status ==
        "error" else 0, "output_excerpt": "boom" if status == "error" else "ok",
        "error": "boom" if status == "error" else None, "signals": list(signals)}}
    return [start, finish]


def test_an_open_test_failure_is_not_hidden_by_later_reads():
    """Review: a failing pytest followed by 12 reads vanished from the brief."""
    from continuum.coding import worklog

    events = _call(1, "c1", "Bash", "pytest -q", "error")
    for i in range(12):
        events += _call(10 + 2 * i, f"r{i}", "Read", f"f{i}.py", "ok", kind="builtin")
    w = worklog.build(events, lambda p: p)
    assert [f["target"] for f in w["failures"]] == ["pytest -q"]
    # a broader run of the same command that passes fixes it
    events += _call(100, "c2", "Bash", "pytest", "ok")
    narrow = _call(1, "n1", "Bash", "pytest -q tests/test_x.py", "error") + \
        _call(3, "n2", "Bash", "pytest", "ok")
    assert worklog.build(narrow, lambda p: p)["failures"] == []


def test_a_subagent_report_is_given_only_to_its_own_subagent():
    """Review: subagent A (no result) was shown with subagent B's report."""
    from continuum.coding import worklog

    events = [
        {"seq": 1, "type": "ToolCallStarted", "at": "t", "p": {
            "call_key": "a", "tool": "Task", "kind": "subagent", "effect": "delegate",
            "source": "app", "input": {"description": "audit auth"}}},
        {"seq": 2, "type": "ToolCallFinished", "at": "t", "p": {
            "call_key": "a", "tool": "Task", "status": "ok", "output_excerpt": "",
            "signals": []}},
        {"seq": 3, "type": "ToolCallStarted", "at": "t", "p": {
            "call_key": "b", "tool": "Task", "kind": "subagent", "effect": "delegate",
            "source": "app", "input": {"description": "check billing"}}},
        {"seq": 4, "type": "ToolCallFinished", "at": "t", "p": {
            "call_key": "b", "tool": "Task", "status": "ok",
            "output_excerpt": '{"agentId": "B"}', "signals": []}},
        {"seq": 5, "type": "SubagentReported", "at": "t", "p": {
            "agent_id": "B", "report_excerpt": "billing is fine"}},
    ]
    subs = {s["target"]: s["report"] for s in worklog.build(events, lambda p: p)["subagents"]}
    assert subs == {"audit auth": None, "check billing": "billing is fine"}
