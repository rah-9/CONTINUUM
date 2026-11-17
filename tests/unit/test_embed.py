# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""The semantic session memory (D-050), with a deterministic stand-in embedder: never a
downloaded model in tests."""

from __future__ import annotations

import re

import pytest

from continuum.coding import digest, embed, semantic

SYNONYMS = {"broke": "fail", "break": "fail", "failed": "fail", "crashed": "fail",
            "customers": "tenant", "tenants": "tenant", "clients": "tenant",
            "lacked": "ignore", "ignored": "ignore", "missed": "ignore"}


def _session(n: int = 40) -> list[tuple[int, str]]:
    """Two interleaved threads (a CSV export, a flaky login test) and filler."""
    lines: list[tuple[int, str]] = []
    seq = 1
    for i in range(n):
        if i % 2 == 0:
            text = (f"USER: the csv export of invoices_{i} should use semicolons, "
                    f"it must handle region_{i}")
            report = (f"ASSISTANT: The csv export of invoices_{i} now writes "
                      f"{100 + i} rows with semicolons to `export/csv_{i}.py`.")
        else:
            text = f"USER: the login test login_case_{i} is flaky again, check the session"
            report = (f"ASSISTANT: The login test login_case_{i} failed because the session "
                      f"token expired after {i} s in `auth/session_{i}.py`.")
        lines += [(seq, f"#{seq} {text}"), (seq + 1, f"#{seq + 1} ASSISTANT: Let me look at "
                  "the code first so I understand it."), (seq + 2, f"#{seq + 2} {report}")]
        seq += 3
    return lines


def test_model_choices(monkeypatch):
    for value, name in (("fake", "fake"), ("off", None),
                        ("sentence-transformers/all-MiniLM-L6-v2",
                         "sentence-transformers:sentence-transformers/all-MiniLM-L6-v2"),
                        ("model2vec:minishlab/potion-base-8M",
                         "model2vec:minishlab/potion-base-8M")):
        monkeypatch.setenv("CONTINUUM_EMBED_MODEL", value)
        model = embed.configured()
        assert (model.name if model else None) == name


def test_auto_uses_only_a_downloaded_model(monkeypatch):
    """"auto" never downloads during a brief: a model that cannot load is skipped."""
    monkeypatch.setenv("CONTINUUM_EMBED_MODEL", "auto")
    monkeypatch.setattr(embed, "DEFAULT_MODELS", ("model2vec:no-such/model-xyz",))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    assert embed.configured() is None


def test_vectors_are_unit_length_and_deterministic():
    model = embed.FakeEmbedder()
    v = model.embed(["cache key tenant", "cache key tenant", "completely different words"])
    assert embed.cosine(v[0], v[1]) == pytest.approx(1.0)
    assert embed.cosine(v[0], v[2]) < 0.5


def test_the_memory_is_organised_by_topic_within_its_budget():
    lines = _session()
    text = digest.build(lines, 9000, embedder=embed.FakeEmbedder())
    assert 0 < len(text) <= 9000
    heads = [ln for ln in text.splitlines() if ln.startswith("### Topic: ")]
    assert len(heads) >= 2
    # the two threads are told apart, though they alternate turn by turn
    csv = next(h for h in heads if "csv" in h or "semicolons" in h or "invoices" in h)
    body = text.split(csv, 1)[1].split("\n### ", 1)[0]
    assert "login_case" not in body and "invoices_" in body
    topics = text.split("### Other", 1)[0]
    assert "Let me look at the code first" not in topics     # filler forms no topic


def test_a_paraphrase_is_kept_once_but_a_new_value_is_kept():
    base = [*_session(20),
            (901, "#901 USER: please look at the checkout cache problem for me today"),
            (902, "#902 ASSISTANT: The `cache_key` ignored which tenants owned stored data."),
            (903, "#903 USER: and what about the other customers, is it the same thing"),
            (904, "#904 ASSISTANT: The `cache_key` lacked which customers owned stored data.")]
    space = semantic._Space(embed.FakeEmbedder(SYNONYMS), None, 4)
    units = digest.units(base)
    digest._score(units)
    space.prepare(units)
    a = next(u for u in units if u.seq == 902)
    b = next(u for u in units if u.seq == 904)
    assert space.redundant(b, [a])                      # the same thing in other words
    newer = digest.units([(5, "#5 ASSISTANT: The stored cache data lacked which customers "
                              "owned the entry in 41 cases.")])[0]
    assert not space.redundant(newer, [a])              # not embedded: never merged blindly


def test_focus_puts_the_topic_being_asked_about_first():
    lines = _session()
    plain = semantic._Space(embed.FakeEmbedder(), None, 6)
    focused = semantic._Space(embed.FakeEmbedder(), "the login test is flaky, session token", 6)
    for space in (plain, focused):
        units = digest.units(lines)
        digest._score(units)
        space.prepare(units)
    def top(space: semantic._Space) -> str:
        best = max(space.topics, key=lambda t: t.weight)
        return " ".join(space.units[i].text for i in best.members)
    assert "login_case" in top(focused)                 # the thread being asked about


def test_a_model_that_cannot_load_or_a_small_budget_gives_the_lexical_digest():
    class Broken:
        name = "broken"

        def embed(self, texts: list[str]) -> object:
            raise embed.EmbedError("not installed")

    lines = _session(20)
    assert digest.build(lines, 9000, embedder=Broken()) == digest.build(lines, 9000)
    small = semantic.MIN_BUDGET - 1
    assert digest.build(lines, small, embedder=embed.FakeEmbedder()) == digest.build(lines, small)


def test_always_kept_lines_and_sources_survive_the_reorganisation():
    lines = [*_session(20),
             (900, '#900 TOOL AskUserQuestion [ok]: {"questions": []} => User has answered '
                   'your questions: "Where should exports be stored?"="Object storage". ok'),
             (901, "#901 OUTCOME UNKNOWN: Bash: git push origin feature/x was interrupted")]
    text = digest.build(lines, 9000, embedder=embed.FakeEmbedder())
    assert "Where should exports be stored? -> Object storage" in text
    assert "git push origin feature/x" in text
    for line in text.splitlines():
        if line.startswith("- ") and not line.startswith("- ("):
            assert re.match(r"- \[#\d+\]", line), line


def test_deterministic():
    lines = _session()
    a = digest.build(lines, 9000, embedder=embed.FakeEmbedder())
    assert a == digest.build(lines, 9000, embedder=embed.FakeEmbedder())
