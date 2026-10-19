# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Typed task specification and state enums (docs/03-STATE-SCHEMA.md sections 1-2).

Pydantic models here mirror the JSON contracts; the PostgreSQL DDL in
``schema.sql`` is the persisted counterpart. There is no third source of truth.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from continuum.core.errors import SpecValidationError


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    RECOVERING = "recovering"
    PAUSED_FOR_APPROVAL = "paused_for_approval"
    AWAITING_RECONCILIATION = "awaiting_reconciliation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


class MilestoneStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    INVALIDATED = "invalidated"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    AUTHORIZED = "authorized"
    IN_FLIGHT = "in_flight"
    CONFIRMED = "confirmed"
    FAILED_CLEAN = "failed_clean"
    UNKNOWN = "unknown"
    NEEDS_HUMAN = "needs_human"
    REJECTED = "rejected"
    SKIPPED = "skipped"


# Actions in these states block any *new* side-effecting dispatch in the run.
UNRESOLVED_ACTION_STATUSES = frozenset(
    {ActionStatus.IN_FLIGHT, ActionStatus.UNKNOWN, ActionStatus.NEEDS_HUMAN}
)


class AttemptStatus(StrEnum):
    STARTED = "started"
    CONFIRMED = "confirmed"
    FAILED_CLEAN = "failed_clean"
    AMBIGUOUS = "ambiguous"


class VerifierResult(StrEnum):
    PASS = "pass"  # noqa: S105 - enum value, not a credential
    FAIL = "fail"
    ERROR = "error"


class ArtifactState(StrEnum):
    PRESENT = "present"
    DRIFTED = "drifted"
    MISSING = "missing"


_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MilestoneSpec(_Frozen):
    id: str
    description: str
    depends_on: tuple[str, ...] = ()
    verifier_id: str
    verifier_version: str

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(f"invalid milestone id {value!r}")
        return value


class Budgets(_Frozen):
    max_model_calls: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_cost_usd: Decimal = Field(gt=0)
    max_active_seconds: int = Field(gt=0)
    max_recoveries: int = Field(ge=0)


class InputSpec(_Frozen):
    uri: str
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _hex(cls, value: str) -> str:
        value = value.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value


class ModelPolicy(_Frozen):
    primary: str
    fallbacks: tuple[str, ...] = ()
    max_handoffs: int = Field(default=3, ge=0)
    same_provider_retries: int = Field(default=2, ge=0)
    backoff_base_seconds: float = Field(default=1.0, ge=0)
    backoff_max_seconds: float = Field(default=10.0, ge=0)


class TaskSpec(_Frozen):
    spec_version: int = 1
    task_id: str
    goal: str
    instructions: str = ""
    tools_allowlist: tuple[str, ...]
    milestones: tuple[MilestoneSpec, ...]
    budgets: Budgets
    inputs: tuple[InputSpec, ...] = ()
    model_policy: ModelPolicy
    constraints: tuple[str, ...] = ()

    @field_validator("spec_version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("only spec_version 1 is supported")
        return value

    @model_validator(mode="after")
    def _structure(self) -> TaskSpec:
        validate_milestone_graph(self.milestones)
        if not self.tools_allowlist:
            raise ValueError("tools_allowlist must not be empty")
        if len(set(self.tools_allowlist)) != len(self.tools_allowlist):
            raise ValueError("tools_allowlist contains duplicates")
        for item in self.inputs:
            if item.uri.startswith("/") or ".." in item.uri.split("/"):
                raise ValueError(f"input uri must be workspace-relative: {item.uri}")
        return self

    def milestone(self, milestone_id: str) -> MilestoneSpec:
        for m in self.milestones:
            if m.id == milestone_id:
                return m
        raise KeyError(milestone_id)

    def descendants(self, milestone_id: str) -> list[str]:
        """Transitive dependents of ``milestone_id`` in spec order."""
        found: set[str] = set()
        frontier = {milestone_id}
        while frontier:
            nxt = {m.id for m in self.milestones if set(m.depends_on) & frontier} - found
            found |= nxt
            frontier = nxt
        return [m.id for m in self.milestones if m.id in found]

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def validate_milestone_graph(milestones: tuple[MilestoneSpec, ...]) -> None:
    if not milestones:
        raise ValueError("at least one milestone is required")
    ids = [m.id for m in milestones]
    if len(ids) != len(set(ids)):
        raise ValueError("milestone ids must be unique")
    known = set(ids)
    for m in milestones:
        missing = set(m.depends_on) - known
        if missing:
            raise ValueError(f"milestone {m.id} depends on unknown {sorted(missing)}")
        if m.id in m.depends_on:
            raise ValueError(f"milestone {m.id} depends on itself")
    # Kahn's algorithm: every node must be removable for the graph to be acyclic.
    indegree = {m.id: len(set(m.depends_on)) for m in milestones}
    ready = [i for i, d in indegree.items() if d == 0]
    seen = 0
    while ready:
        node = ready.pop()
        seen += 1
        for m in milestones:
            if node in m.depends_on:
                indegree[m.id] -= 1
                if indegree[m.id] == 0:
                    ready.append(m.id)
    if seen != len(milestones):
        raise ValueError("milestone dependencies contain a cycle")


def parse_task_spec(data: dict[str, Any]) -> TaskSpec:
    try:
        return TaskSpec.model_validate(data)
    except ValueError as exc:
        raise SpecValidationError(str(exc)) from exc


def focus_milestone(spec: TaskSpec, statuses: dict[str, MilestoneStatus]) -> str | None:
    """First eligible unverified milestone (all dependencies verified), in spec order."""
    for m in spec.milestones:
        if statuses.get(m.id) == MilestoneStatus.VERIFIED:
            continue
        if all(statuses.get(d) == MilestoneStatus.VERIFIED for d in m.depends_on):
            return m.id
    return None
