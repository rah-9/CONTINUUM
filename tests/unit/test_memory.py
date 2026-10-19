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
