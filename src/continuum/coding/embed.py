# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Sentence-embedding models for the session memory (D-050).

An embedding model turns a sentence into a vector whose direction is its meaning, so
sentences that say the same thing in different words are close. It is not a language
model: it writes nothing, runs locally on a CPU and costs nothing per use. The memory
(``semantic.build``) uses it to group the session into topics, to merge what is said
twice, and to put first what matters for the work at hand.

Models are downloaded once (``continuum models pull``) and then load from the local
cache, offline. A brief never downloads anything: with no model installed, the memory
is the lexical digest.

``CONTINUUM_EMBED_MODEL``:

* unset or ``auto`` (the default): the first model already downloaded among
  ``DEFAULT_MODELS``; none downloaded: no embeddings
* ``model2vec:<model>``: static embeddings, no neural network at run time, ~30 MB,
  loads in about a second (``pip install 'continuum[semantic-lite]'``)
* ``sentence-transformers:<model>`` or a bare ``owner/model`` name: a transformer
  encoder (``pip install 'continuum[semantic]'``, brings PyTorch)
* ``fake``: deterministic, for tests; ``off``: no embeddings

Models that need to run code downloaded with them are never loaded.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Protocol

try:  # numpy comes with every embedding backend; the lexical memory needs none
    import numpy as np
except ImportError:  # pragma: no cover - only without the semantic extras
    np = None  # type: ignore[assignment]

# tried in order by "auto": the light static model first, then a transformer encoder
DEFAULT_MODELS = ("model2vec:minishlab/potion-base-8M",
                  "sentence-transformers:sentence-transformers/all-MiniLM-L6-v2")
KNOWN_MODELS = {
    "model2vec:minishlab/potion-base-8M": "static, 256 dims, ~30 MB, no PyTorch, ~1 s to load",
    "model2vec:minishlab/potion-retrieval-32M": "static, 512 dims, ~120 MB, no PyTorch",
    "sentence-transformers:sentence-transformers/all-MiniLM-L6-v2":
        "transformer, 384 dims, ~90 MB, PyTorch, ~5 s to load",
    "sentence-transformers:BAAI/bge-small-en-v1.5": "transformer, 384 dims, ~130 MB, PyTorch",
}


class Embedder(Protocol):
    @property
    def name(self) -> str: ...

    def embed(self, texts: list[str]) -> Any:
        """A (len(texts), dims) float32 array of unit-length rows."""


class EmbedError(Exception):
    """The embedding model is not installed, not downloaded, or could not run."""


_LOADED: dict[str, Any] = {}          # one load per process (the MCP server is long-lived)


def _normalized(rows: Any) -> Any:
    arr = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.where(norms == 0, 1.0, norms)


class SentenceTransformerEmbedder:
    def __init__(self, model: str, *, offline: bool = True) -> None:
        self.model, self.offline = model, offline

    @property
    def name(self) -> str:
        return f"sentence-transformers:{self.model}"

    def _load(self) -> Any:
        if self.name not in _LOADED:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise EmbedError("needs `pip install 'continuum[semantic]'`") from exc
            try:
                _LOADED[self.name] = SentenceTransformer(
                    self.model, device="cpu", trust_remote_code=False,
                    local_files_only=self.offline)
            except Exception as exc:
                raise EmbedError(f"{self.name}: not downloaded (`continuum models pull "
                                 f"{self.name}`) or not loadable: {type(exc).__name__}") from exc
        return _LOADED[self.name]

    def embed(self, texts: list[str]) -> Any:
        model = self._load()
        try:
            return _normalized(model.encode(texts, batch_size=64, show_progress_bar=False,
                                            normalize_embeddings=True))
        except Exception as exc:
            raise EmbedError(f"{self.name}: {type(exc).__name__}") from exc


class Model2VecEmbedder:
    def __init__(self, model: str, *, offline: bool = True) -> None:
        self.model, self.offline = model, offline

    @property
    def name(self) -> str:
        return f"model2vec:{self.model}"

    def _load(self) -> Any:
        if self.name not in _LOADED:
            try:
                from model2vec import StaticModel
            except ImportError as exc:
                raise EmbedError("needs `pip install 'continuum[semantic-lite]'`") from exc
            env = {"HF_HUB_OFFLINE": "1"} if self.offline else {}
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                _LOADED[self.name] = StaticModel.from_pretrained(self.model)
            except Exception as exc:
                raise EmbedError(f"{self.name}: not downloaded (`continuum models pull "
                                 f"{self.name}`) or not loadable: {type(exc).__name__}") from exc
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        return _LOADED[self.name]

    def embed(self, texts: list[str]) -> Any:
        model = self._load()
        try:
            return _normalized(model.encode(texts))
        except Exception as exc:
            raise EmbedError(f"{self.name}: {type(exc).__name__}") from exc


class FakeEmbedder:
    """Deterministic, for tests: a hashed bag of words. Texts sharing words are close;
    ``synonyms`` maps words to a shared concept so paraphrases are close too."""

    def __init__(self, synonyms: dict[str, str] | None = None, dims: int = 64) -> None:
        self.synonyms = synonyms or {}
        self.dims = dims

    @property
    def name(self) -> str:
        return "fake"

    def embed(self, texts: list[str]) -> Any:
        rows = []
        for text in texts:
            vec = [0.0] * self.dims
            for word in re.findall(r"[a-z0-9_]{3,}", text.lower()):
                word = self.synonyms.get(word, word)
                slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big")
                vec[slot % self.dims] += 1.0
            rows.append(vec)
        return _normalized(rows)


def cosine(a: Any, b: Any) -> float:
    """Cosine similarity of two unit-length vectors."""
    return float(np.dot(a, b))


def make(choice: str, *, offline: bool = True) -> Embedder:
    if choice == "fake":
        return FakeEmbedder()
    if choice.startswith("model2vec:"):
        return Model2VecEmbedder(choice.split(":", 1)[1], offline=offline)
    return SentenceTransformerEmbedder(choice.removeprefix("sentence-transformers:"),
                                       offline=offline)


def configured() -> Embedder | None:
    """The embedder from ``CONTINUUM_EMBED_MODEL``, or None when none is set up. "auto"
    (the default) picks the first of DEFAULT_MODELS that is installed and downloaded."""
    if np is None:
        return None
    choice = os.environ.get("CONTINUUM_EMBED_MODEL", "auto").strip()
    if choice.lower() in ("off", "none", "0", "false"):
        return None
    if choice.lower() not in ("", "auto"):
        return make(choice)
    for name in DEFAULT_MODELS:
        model = make(name)
        try:
            model._load()  # type: ignore[attr-defined]
        except EmbedError:
            continue
        return model
    return None


def pull(choice: str) -> dict[str, Any]:
    """Download a model once into the local cache (``continuum models pull``)."""
    model = make(choice, offline=False)
    model.embed(["warm up"])
    return {"model": model.name, "status": "ready",
            "note": KNOWN_MODELS.get(model.name, "")}
