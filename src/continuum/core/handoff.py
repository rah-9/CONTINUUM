# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Deterministic portable continuation packet (docs/05 section 4).

The packet is canonical JSON derived only from the task spec, durable
projections and verifier evidence. Unverified model narrative never appears in
authoritative sections; recent tool outputs appear only in ``data_refs``,
explicitly labelled untrusted. Same snapshot + profile + builder version =>
byte-identical packet (no timestamps, no random ordering).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from continuum.core.canonical import canonical_json, sha256_hex
from continuum.core.errors import PacketTooLargeError
from continuum.core.state import (
    UNRESOLVED_ACTION_STATUSES,
    ActionStatus,
    ArtifactState,
    MilestoneStatus,
    TaskSpec,
)
from continuum.core.store import ActionRow, ArtifactRow, MilestoneRow, UsageRow
from continuum.models.base import CapabilityProfile, estimate_tokens

PACKET_VERSION = 1
BUILDER_VERSION = "1"


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    spec: TaskSpec
    milestones: list[MilestoneRow]
    actions: list[ActionRow]
    artifacts: list[ArtifactRow]
    usage: UsageRow


@dataclass(frozen=True)
class DataRef:
    ref: str
    tool: str
    data: Any


@dataclass(frozen=True)
class Packet:
    body: dict[str, Any]
    text: str
    sha256: str
    tokens_estimate: int
    truncation: list[str]
    writes_allowed: bool


def remaining_budget(spec: TaskSpec, usage: UsageRow) -> dict[str, Any]:
    b = spec.budgets
    return {
        "model_calls": max(b.max_model_calls - usage.model_calls, 0),
        "tokens": max(b.max_tokens - usage.tokens_reported, 0),
        "cost_usd": str(max(b.max_cost_usd - usage.cost_known_usd, Decimal(0))),
        "cost_has_unpriced_usage": usage.has_unpriced_usage,
        "active_seconds": max(b.max_active_seconds - usage.active_elapsed_ms // 1000, 0),
    }


class PacketBuilder:
    def __init__(self, *, context_fraction: float = 0.25) -> None:
        if not 0 < context_fraction <= 1:
            raise ValueError("context_fraction must be in (0, 1]")
        self.context_fraction = context_fraction

    def capacity(self, profile: CapabilityProfile, *, tool_schema_tokens: int,
                 max_output_tokens: int) -> int:
        window = profile.context_window_tokens
        hard = window - max_output_tokens - tool_schema_tokens
        return min(int(window * self.context_fraction), hard)

    def build(self, snap: RunSnapshot, profile: CapabilityProfile, *,
              data_refs: list[DataRef], tool_schema_tokens: int, max_output_tokens: int,
              handoff: dict[str, Any] | None = None,
              count_tokens: Callable[[str], int] = estimate_tokens) -> Packet:
        """Build the packet. ``handoff`` is runtime provenance (never model narrative);
        ``count_tokens`` is the target provider's tokenizer when available."""
        estimate = count_tokens
        body = self._body(snap, data_refs, with_descriptions=True)
        body["handoff"] = handoff
        capacity = self.capacity(profile, tool_schema_tokens=tool_schema_tokens,
                                 max_output_tokens=max_output_tokens)
        truncation: list[str] = []
        text = canonical_json(body)
        # Reduction order: drop oldest untrusted data, then compact descriptions
        # (references are always retained), then fail rather than drop invariants.
        while estimate(text) > capacity and body["data_refs"]:
            body["data_refs"].pop(0)
            if "data_refs_dropped" not in truncation:
                truncation.append("data_refs_dropped")
            text = canonical_json(body)
        if estimate(text) > capacity:
            for m in body["pending_milestones"]:
                m.pop("description", None)
            truncation.append("descriptions_compacted")
            text = canonical_json(body)
        tokens = estimate(text)
        if tokens > capacity:
            raise PacketTooLargeError(
                f"packet needs ~{tokens} tokens but {profile.alias} allows {capacity}")
        return Packet(body=body, text=text, sha256="sha256:" + sha256_hex(text),
                      tokens_estimate=tokens, truncation=truncation,
                      writes_allowed=not body["open_actions"])

    def _body(self, snap: RunSnapshot, data_refs: list[DataRef], *,
              with_descriptions: bool) -> dict[str, Any]:
        spec = snap.spec
        by_id = {m.milestone_id: m for m in snap.milestones}
        verified = [
            {"id": m.id, "evidence_ref": by_id[m.id].evidence_ref,
             "fingerprint": by_id[m.id].input_fingerprint}
            for m in spec.milestones if by_id[m.id].status == MilestoneStatus.VERIFIED]
        pending = []
        for m in spec.milestones:
            if by_id[m.id].status != MilestoneStatus.VERIFIED:
                item: dict[str, Any] = {"id": m.id, "depends_on": list(m.depends_on)}
                if with_descriptions:
                    item["description"] = m.description
                pending.append(item)
        confirmed = sorted(
            ({"operation_key": a.operation_key, "result_ref": a.outcome_ref}
             for a in snap.actions
             if a.status == ActionStatus.CONFIRMED and not a.operation_key.startswith("read:")),
            key=lambda x: str(x["operation_key"]))
        open_actions = sorted(
            ({"action_id": a.action_id, "operation_key": a.operation_key,
              "status": a.status.value}
             for a in snap.actions
             if a.status in UNRESOLVED_ACTION_STATUSES
             or a.status == ActionStatus.AWAITING_APPROVAL),
            key=lambda x: str(x["operation_key"]))
        artifacts = sorted(
            ({"path": a.relative_path, "sha256": a.sha256} for a in snap.artifacts
             if a.state == ArtifactState.PRESENT),
            key=lambda x: (x["path"], x["sha256"]))
        return {
            "packet_version": PACKET_VERSION,
            "builder_version": BUILDER_VERSION,
            "run_id": snap.run_id,
            "task": {"goal": spec.goal, "instructions": spec.instructions,
                     "constraints": list(spec.constraints)},
            "verified_milestones": verified,
            "pending_milestones": pending,
            "artifacts": artifacts,
            "confirmed_actions": confirmed,
            "open_actions": open_actions,
            "allowed_tools": list(spec.tools_allowlist),
            "remaining_budget": remaining_budget(spec, snap.usage),
            "data_refs": [{"ref": d.ref, "tool": d.tool, "untrusted": True, "data": d.data}
                          for d in data_refs],
        }
