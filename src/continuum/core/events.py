# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Event types, typed payloads and the canonical event envelope (docs/04 section 1).

Every event payload is validated against its Pydantic model before it is
appended, and carries ``schema_version``. Payloads hold enough values to
rebuild every durable projection deterministically, including timestamps
(which come from the envelope's ``occurred_at``).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from continuum.core.canonical import to_jsonable

EVENT_SCHEMA_VERSION = 1


class EventType(StrEnum):
    # Run
    RUN_CREATED = "RunCreated"
    RUN_STARTED = "RunStarted"
    RUN_STATUS_CHANGED = "RunStatusChanged"
    RUN_COMPLETED = "RunCompleted"
    RUN_FAILED = "RunFailed"
    RUN_CANCELLED = "RunCancelled"
    # Model
    MODEL_SESSION_STARTED = "ModelSessionStarted"
    MODEL_CALL_STARTED = "ModelCallStarted"
    MODEL_CALL_FINISHED = "ModelCallFinished"
    MODEL_CALL_FAILED = "ModelCallFailed"
    MODEL_SWITCHED = "ModelSwitched"
    MODEL_SESSION_ENDED = "ModelSessionEnded"
    # Action
    ACTION_PROPOSED = "ActionProposed"
    ACTION_PROPOSAL_DEDUPED = "ActionProposalDeduped"
    ACTION_REJECTED = "ActionRejected"
    APPROVAL_REQUESTED = "ApprovalRequested"
    APPROVAL_DECIDED = "ApprovalDecided"
    ACTION_AUTHORIZED = "ActionAuthorized"
    ACTION_ATTEMPT_STARTED = "ActionAttemptStarted"
    ACTION_ATTEMPT_CONFIRMED = "ActionAttemptConfirmed"
    ACTION_ATTEMPT_FAILED_CLEAN = "ActionAttemptFailedClean"
    ACTION_ATTEMPT_AMBIGUOUS = "ActionAttemptAmbiguous"
    ACTION_RECONCILED = "ActionReconciled"
    ACTION_NEEDS_HUMAN = "ActionNeedsHuman"
    ACTION_SKIPPED = "ActionSkipped"
    # Verification / artifacts
    ARTIFACT_RECORDED = "ArtifactRecorded"
    ARTIFACT_DRIFT = "ArtifactDrift"
    VERIFICATION_STARTED = "VerificationStarted"
    MILESTONE_VERIFIED = "MilestoneVerified"
    MILESTONE_FAILED = "MilestoneFailed"
    MILESTONE_INVALIDATED = "MilestoneInvalidated"
    VERIFIER_ERROR = "VerifierError"
    # Runtime
    RECOVERY_STARTED = "RecoveryStarted"
    RUN_ACTIVE_TIME_RECORDED = "RunActiveTimeRecorded"
    BUDGET_EXCEEDED = "BudgetExceeded"
    FAULT_INJECTED = "FaultInjected"


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = EVENT_SCHEMA_VERSION


class UsagePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: str | None = None
    source: Literal["reported", "estimated", "unknown"] = "unknown"


# ---- run -------------------------------------------------------------------


class RunCreated(Payload):
    task_id: str
    spec: dict[str, Any]
    owner_id: str


class RunStarted(Payload):
    owner_id: str
    epoch: int


class RunStatusChanged(Payload):
    from_status: str
    to_status: str
    reason: str


class RunCompleted(Payload):
    verified_milestones: list[str]


class RunFailed(Payload):
    failure_code: str
    reason: str


class RunCancelled(Payload):
    actor: str
    reason: str


# ---- model -----------------------------------------------------------------


class ModelSessionStarted(Payload):
    session_id: str
    model_alias: str
    model_id: str
    profile: dict[str, Any]
    packet_sha256: str


class ModelCallStarted(Payload):
    session_id: str
    call_no: int


class ModelCallFinished(Payload):
    session_id: str
    call_no: int
    response_ref: str
    finish_reason: str
    proposal_count: int
    usage: UsagePayload


class ModelCallFailed(Payload):
    session_id: str
    call_no: int
    error_category: str
    message: str
    usage: UsagePayload


class ModelSwitched(Payload):
    from_session_id: str | None
    from_model_id: str | None
    to_model_alias: str
    to_model_id: str
    reason: str
    packet_sha256: str
    profile_ref: str
    truncation: list[str]


class ModelSessionEnded(Payload):
    session_id: str
    finish_reason: str


# ---- actions ---------------------------------------------------------------


class ActionProposed(Payload):
    action_id: str
    milestone_id: str | None
    operation_key: str
    tool_name: str
    manifest_version: str
    effect: Literal["read", "write"]
    caller: Literal["model", "verifier", "reconciler", "runtime"]
    args: dict[str, Any]
    args_sha256: str
    provider_call_id: str | None = None


class ActionProposalDeduped(Payload):
    action_id: str
    existing_status: str
    provider_call_id: str | None = None


class ActionRejected(Payload):
    action_id: str | None
    tool_name: str
    reason: str
    code: str
    existing_action_id: str | None = None


class ApprovalRequested(Payload):
    action_id: str


class ApprovalDecided(Payload):
    action_id: str
    decision: Literal["approved", "rejected"]
    actor_id: str
    reason: str


class ActionAuthorized(Payload):
    action_id: str
    idempotency_key: str | None
    at_risk_override: bool = False
    actor: str = "runtime"
    reason: str = ""


class ActionAttemptStarted(Payload):
    action_id: str
    attempt_no: int
    owner_epoch: int
    retry_reason: str | None = None


class ActionAttemptConfirmed(Payload):
    action_id: str
    attempt_no: int
    result_ref: str


class ActionAttemptFailedClean(Payload):
    action_id: str
    attempt_no: int
    error_code: str
    message: str = ""


class ActionAttemptAmbiguous(Payload):
    action_id: str
    attempt_no: int
    error_code: str
    message: str = ""


class ActionReconciled(Payload):
    action_id: str
    to_status: Literal["confirmed", "authorized"]
    method: str
    evidence_ref: str | None
    outcome_ref: str | None = None
    provenance: Literal["automatic", "human_assisted"] = "automatic"
    actor: str = "runtime"


class ActionNeedsHuman(Payload):
    action_id: str
    reason: str


class ActionSkipped(Payload):
    action_id: str
    actor: str
    reason: str


# ---- verification / artifacts -------------------------------------------------


class ArtifactRecorded(Payload):
    artifact_id: str
    relative_path: str
    sha256: str
    byte_size: int
    producing_action_id: str | None


class ArtifactDrift(Payload):
    artifact_id: str
    state: Literal["drifted", "missing"]
    observed_sha256: str | None


class VerificationStarted(Payload):
    milestone_id: str
    verifier_id: str
    verifier_version: str


class MilestoneVerified(Payload):
    milestone_id: str
    verifier_id: str
    verifier_version: str
    input_fingerprint: str
    evidence_ref: str


class MilestoneFailed(Payload):
    milestone_id: str
    evidence_ref: str | None
    message: str


class MilestoneInvalidated(Payload):
    milestone_id: str
    reason: str
    cause_milestone_id: str | None = None


class VerifierError(Payload):
    milestone_id: str
    message: str


# ---- runtime ---------------------------------------------------------------


class RecoveryStarted(Payload):
    owner_id: str
    epoch: int
    ambiguous_attempts: list[str]


class RunActiveTimeRecorded(Payload):
    elapsed_ms: int = Field(ge=0)


class BudgetExceeded(Payload):
    budget: str
    limit: str
    observed: str


class FaultInjected(Payload):
    boundary: str
    detail: dict[str, Any] = Field(default_factory=dict)


PAYLOAD_TYPES: dict[EventType, type[Payload]] = {
    EventType.RUN_CREATED: RunCreated,
    EventType.RUN_STARTED: RunStarted,
    EventType.RUN_STATUS_CHANGED: RunStatusChanged,
    EventType.RUN_COMPLETED: RunCompleted,
    EventType.RUN_FAILED: RunFailed,
    EventType.RUN_CANCELLED: RunCancelled,
    EventType.MODEL_SESSION_STARTED: ModelSessionStarted,
    EventType.MODEL_CALL_STARTED: ModelCallStarted,
    EventType.MODEL_CALL_FINISHED: ModelCallFinished,
    EventType.MODEL_CALL_FAILED: ModelCallFailed,
    EventType.MODEL_SWITCHED: ModelSwitched,
    EventType.MODEL_SESSION_ENDED: ModelSessionEnded,
    EventType.ACTION_PROPOSED: ActionProposed,
    EventType.ACTION_PROPOSAL_DEDUPED: ActionProposalDeduped,
    EventType.ACTION_REJECTED: ActionRejected,
    EventType.APPROVAL_REQUESTED: ApprovalRequested,
    EventType.APPROVAL_DECIDED: ApprovalDecided,
    EventType.ACTION_AUTHORIZED: ActionAuthorized,
    EventType.ACTION_ATTEMPT_STARTED: ActionAttemptStarted,
    EventType.ACTION_ATTEMPT_CONFIRMED: ActionAttemptConfirmed,
    EventType.ACTION_ATTEMPT_FAILED_CLEAN: ActionAttemptFailedClean,
    EventType.ACTION_ATTEMPT_AMBIGUOUS: ActionAttemptAmbiguous,
    EventType.ACTION_RECONCILED: ActionReconciled,
    EventType.ACTION_NEEDS_HUMAN: ActionNeedsHuman,
    EventType.ACTION_SKIPPED: ActionSkipped,
    EventType.ARTIFACT_RECORDED: ArtifactRecorded,
    EventType.ARTIFACT_DRIFT: ArtifactDrift,
    EventType.VERIFICATION_STARTED: VerificationStarted,
    EventType.MILESTONE_VERIFIED: MilestoneVerified,
    EventType.MILESTONE_FAILED: MilestoneFailed,
    EventType.MILESTONE_INVALIDATED: MilestoneInvalidated,
    EventType.VERIFIER_ERROR: VerifierError,
    EventType.RECOVERY_STARTED: RecoveryStarted,
    EventType.RUN_ACTIVE_TIME_RECORDED: RunActiveTimeRecorded,
    EventType.BUDGET_EXCEEDED: BudgetExceeded,
    EventType.FAULT_INJECTED: FaultInjected,
}

_TYPE_BY_PAYLOAD: dict[type[Payload], EventType] = {v: k for k, v in PAYLOAD_TYPES.items()}


class NewEvent(BaseModel):
    """An event not yet assigned a sequence number."""

    model_config = ConfigDict(frozen=True)
    type: EventType
    actor: str
    payload: dict[str, Any]


class Event(BaseModel):
    """A committed event as stored in ``events``."""

    model_config = ConfigDict(frozen=True)
    run_id: str
    seq: int
    event_id: str
    schema_version: int
    type: EventType
    occurred_at: datetime
    actor: str
    payload: dict[str, Any]


def make_event(payload: Payload, *, actor: str) -> NewEvent:
    """Build a validated :class:`NewEvent` from a typed payload model."""
    event_type = _TYPE_BY_PAYLOAD[type(payload)]
    return NewEvent(type=event_type, actor=actor, payload=to_jsonable(payload.model_dump()))


def parse_payload(event_type: EventType, payload: dict[str, Any]) -> Payload:
    return PAYLOAD_TYPES[event_type].model_validate(payload)
