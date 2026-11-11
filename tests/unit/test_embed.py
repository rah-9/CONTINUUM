# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Embeddings in the session digest (D-050), with a deterministic stand-in embedder:
never a real model in tests."""

from __future__ import annotations

import pytest

from continuum.coding import digest, embed

SYNONYMS = {"broke": "fail", "break": "fail", "failed": "fail", "crashed": "fail",
            "customers": "tenant", "tenants": "tenant", "clients": "tenant",
            "lacked": "ignore", "ignored": "ignore", "missed": "ignore"}


def test_model_choices(monkeypatch):
    monkeypatch.delenv("CONTINUUM_EMBED_MODEL", raising=False)
    assert embed.configured() is None                       # the default: no embeddings
    for value, name in (("fake", "fake"), ("off", None),
                        ("sentence-transformers/all-MiniLM-L6-v2",
                         "sentence-transformers:sentence-transformers/all-MiniLM-L6-v2"),
                        ("sentence-transformers:all-MiniLM-L6-v2",
                         "sentence-transformers:all-MiniLM-L6-v2"),
                        ("model2vec:minishlab/potion-base-8M",
                         "model2vec:minishlab/potion-base-8M")):
        monkeypatch.setenv("CONTINUUM_EMBED_MODEL", value)
        model = embed.configured()
        assert (model.name if model else None) == name


def test_vectors_are_unit_length_and_deterministic():
    model = embed.FakeEmbedder()
    a, b = model.embed(["cache key tenant", "cache key tenant"])
    assert a == b and embed.cosine(a, a) == pytest.approx(1.0)
    far = model.embed(["completely different words here"])[0]
    assert embed.cosine(a, far) < 0.5


def test_what_answers_the_users_question_ranks_higher():
    """Two agent sentences of equal word statistics; only meaning tells which one answers
    "why did checkout break for some customers?"."""
    lines = [
        (1, "#1 USER: why did the checkout break for some customers yesterday evening"),
        (2, "#2 ASSISTANT: The nightly report job in `jobs/report_42.py` wrote 310 rows "
            "to the archive table. The checkout crashed because `cart_key_77` missed the "
            "tenants id in `shop/cache_19.py`, 310 times."),
    ]
    model = embed.FakeEmbedder(SYNONYMS)
    units = digest.units(lines)
    digest._score(units)
    digest._mark_requests(units)
    plain = {u.text[:20]: u.score for u in units if u.kind == "report"}
    digest._semantic(units, model)
    lifted = {u.text[:20]: u.score for u in units if u.kind == "report"}
    answer, other = "The checkout crashed", "The nightly report j"
    assert lifted[answer] / plain[answer] > lifted[other] / plain[other] * 1.2


def test_a_paraphrase_is_dropped_but_a_new_number_is_kept():
    lines = [
        (1, "#1 USER: please look at the checkout cache problem for me today"),
        (2, "#2 ASSISTANT: The cache entry ignored which tenants owned the stored data."),
        (3, "#3 USER: and what about the other customers, is it the same thing there"),
        (4, "#4 ASSISTANT: The stored cache data lacked which customers owned the entry."),
        (5, "#5 ASSISTANT: The stored cache data lacked which customers owned the entry in "
            "41 cases."),
    ]
    model = embed.FakeEmbedder(SYNONYMS)
    lexical = digest.build(lines[:4], 4000)
    assert "[#2]" in lexical and "[#4]" in lexical         # different words: both kept
    semantic = digest.build(lines[:4], 4000, embedder=model)
    assert ("[#2]" in semantic) != ("[#4]" in semantic)    # said once: one of the two
    with_number = digest.build(lines, 4000, embedder=model)
    assert "41 cases" in with_number                       # names something new: kept


def test_a_model_that_cannot_load_leaves_the_lexical_digest():
    class Broken:
        name = "broken"

        def embed(self, texts: list[str]) -> list[embed.Vector]:
            raise embed.EmbedError("not installed")

    lines = [(1, "#1 USER: speed up the export please, it takes 730 ms now"),
             (2, "#2 ASSISTANT: The export now takes 140 ms in `export/run.py`.")]
    assert digest.build(lines, 3000, embedder=Broken()) == digest.build(lines, 3000)


def test_the_budget_and_determinism_hold_with_embeddings():
    lines = [(i, f"#{i} {'USER' if i % 4 == 1 else 'ASSISTANT'}: Step {i} moved `mod_{i}.py` "
                 f"to 0.{i} s and fixed issue #{i} in the tenants cache.")
             for i in range(1, 200)]
    model = embed.FakeEmbedder(SYNONYMS)
    a, b = digest.build(lines, 5000, embedder=model), digest.build(lines, 5000, embedder=model)
    assert a == b and 0 < len(a) <= 5000
