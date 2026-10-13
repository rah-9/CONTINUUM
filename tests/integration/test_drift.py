"""Evidence drift invalidates milestones and dependents (U5, R7, I3, I7)."""

import pytest

from continuum.core.evidence import Workspace
from continuum.core.faults import SimulatedCrash
from continuum.core.state import ActionStatus, MilestoneStatus, RunStatus
from continuum_lab.fault_injection import CrashInjector, CrashRule, for_milestone


def test_artifact_drift_invalidates_milestone_and_descendants_keeps_history(make_lab):
    crash = CrashInjector(CrashRule("after_milestone_verified_commit", for_milestone("m2")))
    lab = make_lab("csv_normalize_categories", faults=crash)
    with pytest.raises(SimulatedCrash):
        lab.runtime.start(lab.definition.spec)
    with lab.store.read() as q:
        run_id = q.run_ids()[-1]
    # Someone tampers with the verified artifact while the process is down.
    Workspace(lab.runtime.config.workspace_root, run_id).write_bytes(
        "artifacts/normalized.csv", b"tampered\n")
    lab.clock.advance(31)
    result = lab.new_runtime().resume(run_id)
    with lab.store.read() as q:
        events = q.events(run_id)
        statuses = q.milestone_statuses(run_id)
        actions = q.actions(run_id)
    invalidated = [e.payload["milestone_id"] for e in events
                   if e.type.value == "MilestoneInvalidated"]
    assert invalidated[:2] == ["m1", "m2"]
    assert any(e.type.value == "ArtifactDrift" for e in events)
    # Confirmed effects are never deleted or re-sent; the run cannot falsely complete.
    assert all(a.status == ActionStatus.CONFIRMED for a in actions
               if a.tool_name in ("csv.normalize", "store.put"))
    assert lab.env.store.commits_for("uploads/categories.csv") == 1
    assert result.status != RunStatus.COMPLETED
    assert statuses["m1"] != MilestoneStatus.VERIFIED


def test_model_claim_alone_never_verifies(make_lab):
    from continuum.models.base import ModelEndpoint, ModelRouter
    from continuum.models.fake import ScriptedModelAdapter, fake_profile, text_response

    lab = make_lab("inventory_upload_v1")
    liar = ScriptedModelAdapter([text_response("All milestones are verified!")] * 20)
    router = ModelRouter({a: ModelEndpoint(profile=fake_profile(a), adapter=liar)
                          for a in "ABC"})
    rt = lab.new_runtime(owner_id="ctl-1", router=router)
    result = rt.start(lab.definition.spec)
    assert result.status == RunStatus.FAILED
    with lab.store.read() as q:
        assert all(s != MilestoneStatus.VERIFIED
                   for s in q.milestone_statuses(result.run_id).values())
