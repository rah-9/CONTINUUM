# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from continuum.core.errors import PacketTooLargeError
from continuum.core.handoff import DataRef, PacketBuilder, RunSnapshot
from continuum.core.state import ActionStatus, MilestoneStatus
from continuum.core.store import ActionRow, MilestoneRow, UsageRow
from continuum.models.fake import fake_profile
from continuum_lab.tasks.catalog import csv_upload_inventory

T = datetime(2026, 1, 1, tzinfo=UTC)


def _snapshot(*, unknown=False):
    spec = csv_upload_inventory().spec
    milestones = [
        MilestoneRow(run_id="r", milestone_id="m1", status=MilestoneStatus.VERIFIED,
                     verifier_id="csv_normalized", verifier_version="1",
                     input_fingerprint="sha256:f1", evidence_ref="blob:sha256:e1",
                     verified_at=T),
        MilestoneRow(run_id="r", milestone_id="m2", status=MilestoneStatus.PENDING,
                     verifier_id="store_matches_artifact", verifier_version="1",
                     input_fingerprint=None, evidence_ref=None, verified_at=None)]
    actions = [ActionRow(action_id="a1", run_id="r", milestone_id="m1",
                         operation_key="m1:csv.normalize:artifacts/normalized.csv",
                         tool_name="csv.normalize", manifest_version="1", args_json={},
                         args_sha256="x", idempotency_key=None, status=ActionStatus.CONFIRMED,
                         created_at=T, updated_at=T, outcome_ref="blob:sha256:o1",
                         reconciliation_ref=None)]
    if unknown:
        actions.append(actions[0].model_copy(update={
            "action_id": "a2", "operation_key": "m2:store.put:uploads/inventory.csv",
            "status": ActionStatus.UNKNOWN}))
    usage = UsageRow(run_id="r", model_calls=3, tokens_reported=100,
                     cost_known_usd=Decimal("0.01"), has_unpriced_usage=True, recoveries=1,
                     active_elapsed_ms=5000)
    return RunSnapshot(run_id="r", spec=spec, milestones=milestones, actions=actions,
                       artifacts=[], usage=usage)


def _build(snap, window=128_000, refs=None):
    return PacketBuilder().build(snap, fake_profile("A", context_window_tokens=window),
                                 data_refs=refs or [], tool_schema_tokens=100,
                                 max_output_tokens=500)


def test_packet_is_deterministic_and_carries_verified_state():
    a, b = _build(_snapshot()), _build(_snapshot())
    assert a.text == b.text and a.sha256 == b.sha256
    assert [m["id"] for m in a.body["verified_milestones"]] == ["m1"]
    assert [m["id"] for m in a.body["pending_milestones"]] == ["m2"]
    assert a.body["confirmed_actions"][0]["operation_key"].startswith("m1:csv.normalize")
    assert a.body["remaining_budget"]["model_calls"] == 37
    assert a.body["remaining_budget"]["cost_has_unpriced_usage"] is True
    assert a.writes_allowed


def test_unknown_effect_blocks_writes_in_packet():
    packet = _build(_snapshot(unknown=True))
    assert not packet.writes_allowed
    assert packet.body["open_actions"][0]["status"] == "unknown"


def test_reduction_drops_untrusted_data_first_then_fails_rather_than_drop_state():
    refs = [DataRef(ref=f"blob:{i}", tool="file.read", data="x" * 2000) for i in range(5)]
    full = _build(_snapshot(), refs=refs)
    assert len(full.body["data_refs"]) == 5 and full.truncation == []
    reduced = _build(_snapshot(), window=8000, refs=refs)
    assert reduced.truncation[0] == "data_refs_dropped"
    assert reduced.body["verified_milestones"] and reduced.body["confirmed_actions"]
    with pytest.raises(PacketTooLargeError):
        _build(_snapshot(), window=700, refs=refs)
