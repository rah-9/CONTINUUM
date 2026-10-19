# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Pure event -> projection row changes.

This is the *only* place projection semantics live. The store applies these
row changes in SQL inside the event's transaction; rebuild/inspection applies
them to an in-memory :class:`ProjectionState`. Because both paths share this
function, a rebuild is deterministic and comparable with durable rows (I2).

Projections derive only from the event (payload + envelope). Timestamps come
from ``occurred_at``, never from ``now()`` during rebuild.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

from continuum.core.events import EventType
from continuum.core.state import (
    ActionStatus,
    ArtifactState,
    AttemptStatus,
    MilestoneStatus,
    RunStatus,
)

PROJECTION_TABLES: dict[str, tuple[str, ...]] = {
    "runs": ("run_id",),
    "model_sessions": ("session_id",),
    "milestones": ("run_id", "milestone_id"),
    "actions": ("action_id",),
    "action_attempts": ("action_id", "attempt_no"),
    "artifacts": ("artifact_id",),
    "approvals": ("action_id",),
    "usage_totals": ("run_id",),
}

COST_QUANTUM = Decimal("1e-10")  # matches NUMERIC(20,10) in schema.sql


@dataclass(frozen=True)
class RowChange:
    table: str
    key: dict[str, Any]
    op: Literal["insert", "update"]
    values: dict[str, Any] = field(default_factory=dict)
    increments: dict[str, int | Decimal] = field(default_factory=dict)


def _usage_changes(run_id: str, usage: dict[str, Any]) -> list[RowChange]:
    tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    increments: dict[str, int | Decimal] = {}
    values: dict[str, Any] = {}
    if tokens:
        increments["tokens_reported"] = tokens
    cost = usage.get("cost_usd")
    if cost is not None:
        # the column is NUMERIC(20,10): round here too, or replay (full precision) and
        # the durable row (rounded by PostgreSQL) never agree (I2)
        increments["cost_known_usd"] = Decimal(cost).quantize(COST_QUANTUM, ROUND_HALF_EVEN)
    if cost is None or usage.get("input_tokens") is None:
        # Unknown usage is flagged, never represented as zero.
        values["has_unpriced_usage"] = True
    if not increments and not values:
        return []
    return [RowChange("usage_totals", {"run_id": run_id}, "update", values, increments)]


def project(
    *,
    run_id: str,
    seq: int,
    event_type: EventType,
    occurred_at: datetime,
    payload: dict[str, Any],
) -> list[RowChange]:
    """Row changes produced by one committed event, in application order."""
    p = payload
    t = occurred_at
    changes: list[RowChange] = []
    run_key = {"run_id": run_id}

    def run_update(**values: Any) -> None:
        changes.append(RowChange("runs", run_key, "update", values))

    def action_update(action_id: str, **values: Any) -> None:
        changes.append(RowChange("actions", {"action_id": action_id}, "update",
                                 {**values, "updated_at": t}))

    def attempt_update(action_id: str, attempt_no: int, **values: Any) -> None:
        changes.append(RowChange("action_attempts",
                                 {"action_id": action_id, "attempt_no": attempt_no},
                                 "update", values))

    match event_type:
        case EventType.RUN_CREATED:
            spec = p["spec"]
            changes.append(RowChange("runs", run_key, "insert", {
                "task_id": p["task_id"], "spec_json": spec, "status": RunStatus.CREATED.value,
                "active_session_id": None, "last_event_seq": 0, "created_at": t,
                "updated_at": t, "finished_at": None, "failure_code": None,
            }))
            for m in spec["milestones"]:
                changes.append(RowChange(
                    "milestones", {"run_id": run_id, "milestone_id": m["id"]}, "insert", {
                        "status": MilestoneStatus.PENDING.value,
                        "verifier_id": m["verifier_id"],
                        "verifier_version": m["verifier_version"],
                        "input_fingerprint": None, "evidence_ref": None, "verified_at": None,
                    }))
            changes.append(RowChange("usage_totals", run_key, "insert", {
                "model_calls": 0, "tokens_reported": 0, "cost_known_usd": Decimal(0),
                "has_unpriced_usage": False, "recoveries": 0, "active_elapsed_ms": 0,
            }))
        case EventType.RUN_STARTED:
            run_update(status=RunStatus.RUNNING.value)
        case EventType.RUN_STATUS_CHANGED:
            run_update(status=p["to_status"])
        case EventType.RUN_COMPLETED:
            run_update(status=RunStatus.COMPLETED.value, finished_at=t, active_session_id=None)
        case EventType.RUN_FAILED:
            run_update(status=RunStatus.FAILED.value, finished_at=t,
                       failure_code=p["failure_code"], active_session_id=None)
        case EventType.RUN_CANCELLED:
            run_update(status=RunStatus.CANCELLED.value, finished_at=t, active_session_id=None)
        case EventType.MODEL_SESSION_STARTED:
            changes.append(RowChange("model_sessions", {"session_id": p["session_id"]}, "insert", {
                "run_id": run_id, "model_id": p["model_id"], "profile_json": p["profile"],
                "packet_sha256": p["packet_sha256"], "started_at": t, "ended_at": None,
                "finish_reason": None,
            }))
            run_update(active_session_id=p["session_id"])
        case EventType.MODEL_CALL_STARTED:
            changes.append(RowChange("usage_totals", run_key, "update", {}, {"model_calls": 1}))
        case EventType.MODEL_CALL_FINISHED | EventType.MODEL_CALL_FAILED:
            changes.extend(_usage_changes(run_id, p["usage"]))
        case EventType.MODEL_SESSION_ENDED:
            changes.append(RowChange("model_sessions", {"session_id": p["session_id"]}, "update",
                                     {"ended_at": t, "finish_reason": p["finish_reason"]}))
            run_update(active_session_id=None)
        case EventType.ACTION_PROPOSED:
            changes.append(RowChange("actions", {"action_id": p["action_id"]}, "insert", {
                "run_id": run_id, "milestone_id": p["milestone_id"],
                "operation_key": p["operation_key"], "tool_name": p["tool_name"],
                "manifest_version": p["manifest_version"], "args_json": p["args"],
                "args_sha256": p["args_sha256"], "idempotency_key": None,
                "status": ActionStatus.PROPOSED.value, "created_at": t, "updated_at": t,
                "outcome_ref": None, "reconciliation_ref": None,
            }))
        case EventType.ACTION_REJECTED:
            if p.get("action_id"):
                action_update(p["action_id"], status=ActionStatus.REJECTED.value)
        case EventType.APPROVAL_REQUESTED:
            action_update(p["action_id"], status=ActionStatus.AWAITING_APPROVAL.value)
            changes.append(RowChange("approvals", {"action_id": p["action_id"]}, "insert", {
                "decision": None, "actor_id": None, "decided_at": None, "reason": None,
            }))
        case EventType.APPROVAL_DECIDED:
            changes.append(RowChange("approvals", {"action_id": p["action_id"]}, "update", {
                "decision": p["decision"], "actor_id": p["actor_id"], "decided_at": t,
                "reason": p["reason"],
            }))
        case EventType.ACTION_AUTHORIZED:
            action_update(p["action_id"], status=ActionStatus.AUTHORIZED.value,
                          idempotency_key=p["idempotency_key"])
        case EventType.ACTION_ATTEMPT_STARTED:
            changes.append(RowChange(
                "action_attempts",
                {"action_id": p["action_id"], "attempt_no": p["attempt_no"]}, "insert", {
                    "status": AttemptStatus.STARTED.value,
                    "dispatch_owner_epoch": p["owner_epoch"], "started_at": t,
                    "finished_at": None, "result_ref": None, "error_code": None,
                }))
            action_update(p["action_id"], status=ActionStatus.IN_FLIGHT.value)
        case EventType.ACTION_ATTEMPT_CONFIRMED:
            attempt_update(p["action_id"], p["attempt_no"], status=AttemptStatus.CONFIRMED.value,
                           finished_at=t, result_ref=p["result_ref"])
            action_update(p["action_id"], status=ActionStatus.CONFIRMED.value,
                          outcome_ref=p["result_ref"])
        case EventType.ACTION_ATTEMPT_FAILED_CLEAN:
            attempt_update(p["action_id"], p["attempt_no"],
                           status=AttemptStatus.FAILED_CLEAN.value, finished_at=t,
                           error_code=p["error_code"])
            action_update(p["action_id"], status=ActionStatus.FAILED_CLEAN.value)
        case EventType.ACTION_ATTEMPT_AMBIGUOUS:
            attempt_update(p["action_id"], p["attempt_no"], status=AttemptStatus.AMBIGUOUS.value,
                           finished_at=t, error_code=p["error_code"])
            action_update(p["action_id"], status=ActionStatus.UNKNOWN.value)
        case EventType.ACTION_RECONCILED:
            values: dict[str, Any] = {"status": p["to_status"],
                                      "reconciliation_ref": p["evidence_ref"]}
            if p["to_status"] == ActionStatus.CONFIRMED.value:
                values["outcome_ref"] = p.get("outcome_ref") or p["evidence_ref"]
            action_update(p["action_id"], **values)
        case EventType.ACTION_NEEDS_HUMAN:
            action_update(p["action_id"], status=ActionStatus.NEEDS_HUMAN.value)
        case EventType.ACTION_SKIPPED:
            action_update(p["action_id"], status=ActionStatus.SKIPPED.value)
        case EventType.ARTIFACT_RECORDED:
            changes.append(RowChange("artifacts", {"artifact_id": p["artifact_id"]}, "insert", {
                "run_id": run_id, "relative_path": p["relative_path"], "sha256": p["sha256"],
                "byte_size": p["byte_size"], "producing_action_id": p["producing_action_id"],
                "created_at": t, "state": ArtifactState.PRESENT.value,
            }))
        case EventType.ARTIFACT_DRIFT:
            changes.append(RowChange("artifacts", {"artifact_id": p["artifact_id"]}, "update",
                                     {"state": p["state"]}))
        case EventType.MILESTONE_VERIFIED:
            changes.append(RowChange(
                "milestones", {"run_id": run_id, "milestone_id": p["milestone_id"]}, "update", {
                    "status": MilestoneStatus.VERIFIED.value,
                    "verifier_version": p["verifier_version"],
                    "input_fingerprint": p["input_fingerprint"],
                    "evidence_ref": p["evidence_ref"], "verified_at": t,
                }))
        case EventType.MILESTONE_INVALIDATED:
            changes.append(RowChange(
                "milestones", {"run_id": run_id, "milestone_id": p["milestone_id"]}, "update", {
                    "status": MilestoneStatus.INVALIDATED.value, "input_fingerprint": None,
                    "evidence_ref": None, "verified_at": None,
                }))
        case EventType.RECOVERY_STARTED:
            changes.append(RowChange("usage_totals", run_key, "update", {}, {"recoveries": 1}))
        case EventType.RUN_ACTIVE_TIME_RECORDED:
            changes.append(RowChange("usage_totals", run_key, "update", {},
                                     {"active_elapsed_ms": p["elapsed_ms"]}))
        case (EventType.MODEL_SWITCHED | EventType.ACTION_PROPOSAL_DEDUPED
              | EventType.VERIFICATION_STARTED | EventType.MILESTONE_FAILED
              | EventType.VERIFIER_ERROR | EventType.BUDGET_EXCEEDED
              | EventType.FAULT_INJECTED):
            pass  # audit-only events; no projection change
        case _:  # pragma: no cover - guarded by the exhaustive enum
            raise ValueError(f"no projection rule for {event_type}")

    # Every event advances the run's sequence watermark.
    changes.append(RowChange("runs", run_key, "update",
                             {"last_event_seq": seq, "updated_at": t}))
    return changes


Row = dict[str, Any]


@dataclass
class ProjectionState:
    """In-memory projection of one run, keyed by table then primary-key tuple."""

    tables: dict[str, dict[tuple[Any, ...], Row]] = field(
        default_factory=lambda: {name: {} for name in PROJECTION_TABLES})

    def apply(self, change: RowChange) -> None:
        key_cols = PROJECTION_TABLES[change.table]
        key = tuple(change.key[c] for c in key_cols)
        table = self.tables[change.table]
        if change.op == "insert":
            if key in table:
                raise ValueError(f"duplicate insert into {change.table} {key}")
            table[key] = {**change.key, **change.values}
            return
        row = table.get(key)
        if row is None:
            raise ValueError(f"update of missing row {change.table} {key}")
        row.update(change.values)
        for col, inc in change.increments.items():
            row[col] = row[col] + inc
