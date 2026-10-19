# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Task definition model shared by the Continuum runtime (C1) and the baseline (B0)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from continuum.core.canonical import digest, sha256_hex
from continuum.core.state import TaskSpec
from continuum.tools.manifest import ToolRegistry
from continuum.tools.trusted.services import ServiceMode
from continuum.verify.registry import VerifierRegistry
from continuum_lab.mock_services import MockDatabase, MockMessages, MockObjectStore


@dataclass(frozen=True)
class PlanStep:
    """One intended tool call. ``op_key`` is the trusted identity Continuum derives."""

    milestone: str
    tool: str
    args: dict[str, Any]
    op_key: str | None = None            # None for reads
    expect_reject: bool = False          # policy probes (forbidden id, excluded recipient)
    check: tuple[str, dict[str, Any]] | None = None  # optional read used by B0 before repeats


@dataclass
class ServiceEnv:
    """The task's mock services and their contract modes."""

    state_dir: Path
    store_mode: ServiceMode = "deduped"
    db_mode: ServiceMode = "deduped"
    msg_mode: ServiceMode = "deduped"
    read_lag: int = 0
    pending_ops: int = 2
    retention_seconds: int = 86_400
    now_fn: Callable[[], float] | None = None
    _cache: dict[str, Any] = field(default_factory=dict)

    def _kw(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"read_lag": self.read_lag, "pending_ops": self.pending_ops,
                              "retention_seconds": self.retention_seconds}
        if self.now_fn is not None:
            kw["now_fn"] = self.now_fn
        return kw

    @property
    def store(self) -> MockObjectStore:
        if "store" not in self._cache:
            self._cache["store"] = MockObjectStore(self.state_dir / "store.json",
                                                   mode=self.store_mode, **self._kw())
        svc: MockObjectStore = self._cache["store"]
        return svc

    @property
    def db(self) -> MockDatabase:
        if "db" not in self._cache:
            self._cache["db"] = MockDatabase(self.state_dir / "db.json", mode=self.db_mode,
                                             **self._kw())
        svc: MockDatabase = self._cache["db"]
        return svc

    @property
    def messages(self) -> MockMessages:
        if "msg" not in self._cache:
            self._cache["msg"] = MockMessages(self.state_dir / "messages.json",
                                              mode=self.msg_mode, **self._kw())
        svc: MockMessages = self._cache["msg"]
        return svc


@dataclass(frozen=True)
class Expected:
    """Independent ground-truth expectations for scoring."""

    objects: dict[str, str] = field(default_factory=dict)            # path -> sha256
    records: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> fields
    messages: dict[str, str] = field(default_factory=dict)           # "rcpt/slot" -> body

    def intended_writes(self) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for p in self.objects:
            out[("store", p)] = 1
        for r in self.records:
            out[("db", r)] = 1
        for m in self.messages:
            out[("msg", m)] = 1
        return out


@dataclass(frozen=True)
class TaskDefinition:
    task_id: str
    family: str
    spec: TaskSpec
    inputs: dict[str, bytes]
    plan: list[PlanStep]
    expected: Expected
    build_tools: Callable[[ServiceEnv], ToolRegistry]
    build_verifiers: Callable[[], VerifierRegistry]
    seed: Callable[[ServiceEnv], None] = lambda env: None
    human_decision_expected: bool = False
    env_overrides: dict[str, Any] = field(default_factory=dict)  # e.g. {"msg_mode": "opaque"}

    def write_inputs(self, input_root: Path) -> None:
        for rel, data in self.inputs.items():
            path = input_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


@dataclass
class GroundTruthScore:
    final_state_correct: bool
    committed_duplicates: int
    intended_writes: int
    write_requests: int
    detail: dict[str, Any]


def score_ground_truth(env: ServiceEnv, expected: Expected) -> GroundTruthScore:
    """Score against mock-service ground truth only (never agent output)."""
    env.store.settle()
    env.db.settle()
    env.messages.settle()
    ok = True
    detail: dict[str, Any] = {}
    for path, sha in expected.objects.items():
        head = env.store.head(path)
        ok &= bool(head.get("exists")) and head.get("content_sha256") == sha
    for rid, fields in expected.records.items():
        rec = env.db.get(rid)
        ok &= bool(rec.get("exists")) and rec.get("fields_sha256") == digest(fields)
    for key, body in expected.messages.items():
        rcpt, slot = key.split("/", 1)
        msg = env.messages.query(rcpt, slot)
        ok &= bool(msg.get("exists")) and msg.get("body_sha256") == sha256_hex(body)
    intended = expected.intended_writes()
    commits: dict[tuple[str, str], int] = {}
    for svc, name in ((env.store, "store"), (env.db, "db"), (env.messages, "msg")):
        for c in svc.ground_truth():
            commits[(name, c["target"])] = commits.get((name, c["target"]), 0) + 1
    duplicates = sum(max(0, n - intended.get(k, 0)) for k, n in commits.items())
    unintended = sum(n for k, n in commits.items() if k not in intended)
    detail["commits"] = {f"{k[0]}:{k[1]}": n for k, n in commits.items()}
    detail["unintended_targets"] = unintended
    requests = sum(len(s.attempts_log()) for s in (env.store, env.db, env.messages))
    return GroundTruthScore(final_state_correct=bool(ok), committed_duplicates=duplicates,
                            intended_writes=sum(intended.values()), write_requests=requests,
                            detail=detail)


def file_sha(data: bytes) -> str:
    return sha256_hex(data)
