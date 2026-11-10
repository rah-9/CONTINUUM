# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Sentence embeddings for the session digest (optional, D-050).

An embedding model turns a sentence into a vector whose direction is its meaning, so two
sentences that say the same thing in different words are close. It is not a language
model: it writes nothing, runs locally on a CPU, and costs nothing per call. The digest
uses it for two things its word statistics cannot do (``digest.build(embedder=...)``):

* **relevance**: an agent sentence that answers what the user asked in that turn ranks
  higher, even when it shares no words with the question;
* **paraphrase**: a sentence that repeats a chosen one in other words is dropped.

``CONTINUUM_EMBED_MODEL`` (unset or ``off``: no embeddings, the lexical digest):

* ``sentence-transformers:<model>`` or a bare model name, e.g.
  ``sentence-transformers/all-MiniLM-L6-v2`` (needs ``pip install 'continuum[semantic]'``;
  the model is downloaded once, then works offline);
* ``model2vec:<model>``, e.g. ``model2vec:minishlab/potion-base-8M``: static embeddings,
  no neural network at run time, loads in milliseconds (``pip install model2vec``);
* ``fake``: a deterministic bag-of-words embedder, for tests.

Any failure (package missing, model not downloaded) falls back to the lexical digest.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Sequence
from typing import Any, Protocol

Vector = Sequence[float]


class Embedder(Protocol):
    @property
    def name(self) -> str: ...

    def embed(self, texts: list[str]) -> list[Vector]:
        """One unit-length vector per text."""


class EmbedError(Exception):
    """The embedding model is not installed or could not be loaded."""


_MODELS: dict[str, Any] = {}          # loaded once per process (the MCP server is long-lived)


class SentenceTransformerEmbedder:
    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def name(self) -> str:
        return f"sentence-transformers:{self.model}"

    def embed(self, texts: list[str]) -> list[Vector]:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbedError("needs `pip install 'continuum[semantic]'`") from exc
        try:
            if self.name not in _MODELS:
                _MODELS[self.name] = SentenceTransformer(self.model, device="cpu")
            out = _MODELS[self.name].encode(texts, normalize_embeddings=True, batch_size=64,
                                            show_progress_bar=False)
        except Exception as exc:      # a missing model, no network on first use ...
            raise EmbedError(f"{self.name}: {type(exc).__name__}") from exc
        return [row.tolist() for row in out]


class Model2VecEmbedder:
    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def name(self) -> str:
        return f"model2vec:{self.model}"

    def embed(self, texts: list[str]) -> list[Vector]:
        try:
            from model2vec import StaticModel
        except ImportError as exc:
            raise EmbedError("needs `pip install model2vec`") from exc
        try:
            if self.name not in _MODELS:
                _MODELS[self.name] = StaticModel.from_pretrained(self.model)
            out = _MODELS[self.name].encode(texts)
        except Exception as exc:
            raise EmbedError(f"{self.name}: {type(exc).__name__}") from exc
        return [_unit(row.tolist()) for row in out]


class FakeEmbedder:
    """Deterministic, for tests: a hashed bag of words. Texts sharing words are close;
    ``synonyms`` maps words to a shared concept so paraphrases are close too."""

    def __init__(self, synonyms: dict[str, str] | None = None, dims: int = 64) -> None:
        self.synonyms = synonyms or {}
        self.dims = dims

    @property
    def name(self) -> str:
        return "fake"

    def embed(self, texts: list[str]) -> list[Vector]:
        out: list[Vector] = []
        for text in texts:
            vec = [0.0] * self.dims
            for word in re.findall(r"[a-z0-9_]{3,}", text.lower()):
                word = self.synonyms.get(word, word)
                slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big")
                vec[slot % self.dims] += 1.0
            out.append(_unit(vec))
        return out


def _unit(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity of two unit-length vectors."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def configured() -> Embedder | None:
    """The embedder from ``CONTINUUM_EMBED_MODEL``, or None (the default: no embeddings)."""
    choice = os.environ.get("CONTINUUM_EMBED_MODEL", "").strip()
    if not choice or choice.lower() in ("off", "none", "0", "false"):
        return None
    if choice == "fake":
        return FakeEmbedder()
    if choice.startswith("model2vec:"):
        return Model2VecEmbedder(choice.split(":", 1)[1])
    return SentenceTransformerEmbedder(choice.removeprefix("sentence-transformers:"))
