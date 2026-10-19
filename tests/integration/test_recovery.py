# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Crash, restart and model handoff continuity (U1, U2, R3, R8)."""

import pytest

from continuum.core.faults import SimulatedCrash
from continuum.core.state import ActionStatus, RunStatus
from continuum_lab.fault_injection import CrashInjector, CrashRule, for_milestone, for_tool


def _attempts_by_tool(lab, run_id):
    out = {}
    with lab.store.read() as q:
        for a in q.actions(run_id):
            if not a.operation_key.startswith("read:"):
                out[a.tool_name] = out.get(a.tool_name, 0) + len(q.attempts(a.action_id))
    return out


def _run_id(lab):
    with lab.store.read() as q:
        return q.run_ids()[-1]


def test_u1_kill_after_milestone_2_resume_with_provider_b_reuses_verified_work(make_lab):
    crash = CrashInjector(CrashRule("after_milestone_verified_commit", for_milestone("m2")))
    lab = make_lab("csv_normalize_categories", faults=crash)
    with pytest.raises(SimulatedCrash):
        lab.runtime.start(lab.definition.spec)
    run_id = _run_id(lab)
    calls_a = lab.adapters["A"].calls
    lab.clock.advance(31)  # old controller's lease expires; process is gone

    result = lab.new_runtime().resume(run_id, model_alias="B")
    assert result.status == RunStatus.COMPLETED
    assert lab.adapters["A"].calls == calls_a  # provider A not used after the switch
    assert lab.adapters["B"].calls > 0
    assert _attempts_by_tool(lab, run_id) == {"csv.normalize": 1, "store.put": 1,
                                              "report.write": 1}
    with lab.store.read() as q:
        events = q.events(run_id)
    switched = [e for e in events if e.type.value == "ModelSwitched"]
    assert switched[-1].payload["to_model_alias"] == "B"
    verified = [e.payload["milestone_id"] for e in events if e.type.value == "MilestoneVerified"]
    assert verified.count("m1") == 1 and verified.count("m2") == 1  # not redone
    assert lab.store.projection_diff(run_id) == []


def test_u2_commit_then_crash_before_outcome_recovers_without_duplicate(make_lab):
    crash = CrashInjector(CrashRule("after_response_receipt", for_tool("store.put")))
    lab = make_lab("inventory_upload_v1", faults=crash, store_mode="observable")
    with pytest.raises(SimulatedCrash):
        lab.runtime.start(lab.definition.spec)
    run_id = _run_id(lab)
    assert lab.env.store.commits_for("uploads/inventory.csv") == 1
    lab.clock.advance(31)
    result = lab.new_runtime().resume(run_id, model_alias="B")
    assert result.status == RunStatus.COMPLETED
    assert lab.env.store.commits_for("uploads/inventory.csv") == 1
    assert len(lab.env.store.attempts_log()) == 1
    with lab.store.read() as q:
        put = next(a for a in q.actions(run_id) if a.tool_name == "store.put")
        assert put.status == ActionStatus.CONFIRMED
        assert [t.status for t in q.attempts(put.action_id)] == ["ambiguous"]


def test_concurrent_resume_only_one_owner(make_lab):
    from continuum.core.errors import AlreadyOwnedError

    crash = CrashInjector(CrashRule("after_milestone_verified_commit", for_milestone("m1")))
    lab = make_lab("inventory_upload_v1", faults=crash)
    with pytest.raises(SimulatedCrash):
        lab.runtime.start(lab.definition.spec)
    run_id = _run_id(lab)
    lab.clock.advance(31)
    holder = lab.new_runtime(owner_id="ctl-2")
    lease = holder.leases.acquire(run_id, "ctl-2")
    with pytest.raises(AlreadyOwnedError):
        lab.new_runtime(owner_id="ctl-3").resume(run_id)
    holder.leases.release(lease)


def test_resume_of_completed_run_is_a_noop(make_lab):
    lab = make_lab("db_update_selected")
    result = lab.runtime.start(lab.definition.spec)
    before = len(lab.env.db.attempts_log())
    again = lab.new_runtime().resume(result.run_id)
    assert again.status == RunStatus.COMPLETED
    assert len(lab.env.db.attempts_log()) == before


def test_starting_an_existing_run_id_is_a_typed_error(make_lab):
    from continuum.core.errors import SpecValidationError

    lab = make_lab("db_update_selected")
    lab.runtime.start(lab.definition.spec, run_id="run_fixed")
    with pytest.raises(SpecValidationError) as exc:
        lab.new_runtime().start(lab.definition.spec, run_id="run_fixed")
    assert exc.value.code == "run_exists"
