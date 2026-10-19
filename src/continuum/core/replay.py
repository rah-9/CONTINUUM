# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Side-effect-free diagnostic replay and trace export (R11, I8).

Everything here reads committed events and content-addressed blobs only. It
never calls a provider or a tool and never appends events. ``resume`` is the
only real continuation mechanism.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from continuum.core.events import Event, EventType
from continuum.core.evidence import BLOB_PREFIX, Workspace

_REF_KEYS = ("response_ref", "result_ref", "evidence_ref", "outcome_ref", "profile_ref")


def _blob(workspace: Workspace, ref: Any) -> Any:
    if not isinstance(ref, str) or not ref.startswith(BLOB_PREFIX):
        return None
    try:
        return workspace.get_blob(ref)
    except (FileNotFoundError, ValueError):
        return {"unavailable": ref}


def timeline(events: Sequence[Event], workspace: Workspace) -> list[dict[str, Any]]:
    """Human-oriented steps: model decisions, action outcomes, verification, run status."""
    steps: list[dict[str, Any]] = []
    writes: set[str] = set()  # outcomes of verifier/reconciler reads are omitted
    for e in events:
        p = e.payload
        item: dict[str, Any] = {"seq": e.seq, "at": e.occurred_at.isoformat()}
        match e.type:
            case EventType.MODEL_SESSION_STARTED:
                item |= {"kind": "session", "model": p["model_id"], "alias": p["model_alias"]}
            case EventType.MODEL_SWITCHED:
                item |= {"kind": "handoff", "to": p["to_model_alias"], "reason": p["reason"]}
            case EventType.MODEL_CALL_FINISHED:
                response = _blob(workspace, p["response_ref"]) or {}
                proposals = (response.get("message") or {}).get("tool_proposals") or []
                item |= {"kind": "model_decision",
                         "proposals": [{"tool": x["tool_name"], "arguments": x["arguments"]}
                                       for x in proposals],
                         "text": (response.get("message") or {}).get("content")}
            case EventType.MODEL_CALL_FAILED:
                item |= {"kind": "model_error", "category": p["error_category"]}
            case EventType.ACTION_PROPOSED if p["effect"] == "write":
                writes.add(p["action_id"])
                item |= {"kind": "action", "tool": p["tool_name"],
                         "operation_key": p["operation_key"], "caller": p["caller"]}
            case EventType.ACTION_PROPOSAL_DEDUPED:
                item |= {"kind": "dedupe", "action_id": p["action_id"],
                         "existing_status": p["existing_status"]}
            case EventType.ACTION_REJECTED:
                item |= {"kind": "rejected", "tool": p["tool_name"], "code": p["code"]}
            case (EventType.ACTION_ATTEMPT_CONFIRMED | EventType.ACTION_ATTEMPT_AMBIGUOUS
                  | EventType.ACTION_ATTEMPT_FAILED_CLEAN) if p["action_id"] in writes:
                item |= {"kind": "outcome", "action_id": p["action_id"],
                         "attempt": p["attempt_no"], "result": e.type.value}
            case EventType.ACTION_RECONCILED | EventType.ACTION_NEEDS_HUMAN:
                item |= {"kind": "reconciliation", "action_id": p["action_id"],
                         "result": p.get("to_status", "needs_human"),
                         "method": p.get("method", p.get("reason"))}
            case EventType.MILESTONE_VERIFIED | EventType.MILESTONE_INVALIDATED:
                item |= {"kind": "milestone", "milestone_id": p["milestone_id"],
                         "result": e.type.value}
            case (EventType.RUN_STATUS_CHANGED | EventType.RUN_COMPLETED | EventType.RUN_FAILED
                  | EventType.RUN_CANCELLED | EventType.RECOVERY_STARTED):
                item |= {"kind": "run", "result": e.type.value,
                         "detail": p.get("to_status") or p.get("failure_code")}
            case _:
                continue
        steps.append(item)
    return steps


def export_trace(events: Sequence[Event], workspace: Workspace) -> Iterator[dict[str, Any]]:
    """Full JSON-lines trace: each event with its referenced (redacted) blobs inlined."""
    for e in events:
        record: dict[str, Any] = {"run_id": e.run_id, "seq": e.seq, "event_id": e.event_id,
                                  "type": e.type.value, "actor": e.actor,
                                  "occurred_at": e.occurred_at.isoformat(),
                                  "payload": e.payload}
        blobs = {k: _blob(workspace, e.payload.get(k)) for k in _REF_KEYS
                 if isinstance(e.payload.get(k), str)}
        blobs = {k: v for k, v in blobs.items() if v is not None}
        if blobs:
            record["blobs"] = blobs
        yield record
