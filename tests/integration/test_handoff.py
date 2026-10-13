"""Model failure handling, fallbacks, capability limits (R8, U4, W3)."""

from continuum.core.state import RunStatus
from continuum.models.base import ModelError
from continuum_lab.harness import ModelFaults


def _events(lab, run_id, kind):
    with lab.store.read() as q:
        return [e for e in q.events(run_id) if e.type.value == kind]


def test_transient_rate_limit_retries_same_provider(make_lab):
    faults = ModelFaults(errors={"A": {1: ModelError("rate_limited", retry_after=2)}})
    lab = make_lab("db_update_selected", model_faults=faults)
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    assert _events(lab, result.run_id, "ModelSwitched") == []
    assert lab.adapters["B"].calls == 0


def test_persistent_failure_hands_off_with_packet_and_preserves_budget(make_lab):
    faults = ModelFaults(fail_always={"A": ModelError("provider_unavailable")})
    lab = make_lab("csv_normalize_categories", model_faults=faults)
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    [switch] = _events(lab, result.run_id, "ModelSwitched")
    assert switch.payload["to_model_alias"] == "B"
    assert switch.payload["reason"] == "provider_unavailable"
    with lab.store.read() as q:
        usage = q.usage(result.run_id)
    # Failed provider calls (1 + 2 retries) still count against the aggregate budget.
    assert usage.model_calls == lab.adapters["A"].calls + lab.adapters["B"].calls
    assert usage.has_unpriced_usage  # failed calls had unknown usage: never zero


def test_replacement_model_repeating_confirmed_operation_is_deduped(make_lab):
    # Model A completes the upload, then fails before m2 is verified; B re-proposes it.
    faults = ModelFaults(errors={"A": {3: ModelError("context_overflow")}})
    lab = make_lab("db_update_selected", model_faults=faults)
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    assert lab.env.db.commits_for("r1") == 1 and lab.env.db.commits_for("r2") == 1


def test_duplicate_proposal_in_plan_is_blocked_not_recommitted(make_lab):
    lab = make_lab("db_duplicate_proposal")
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    assert lab.env.db.commits_for("r7") == 1


def test_incompatible_models_are_skipped_explicitly(make_lab):
    faults = ModelFaults(no_tools={"A"}, context_window={"B": 600})
    lab = make_lab("inventory_upload_v1", model_faults=faults)
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    [session] = _events(lab, result.run_id, "ModelSessionStarted")
    assert session.payload["model_alias"] == "C"


def test_all_models_unusable_fails_explicitly(make_lab):
    faults = ModelFaults(fail_always={a: ModelError("bad_request") for a in "ABC"})
    lab = make_lab("inventory_upload_v1", model_faults=faults)
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.FAILED
    assert result.failure_code == "models_exhausted"


def test_malformed_tool_output_is_retried_then_handed_off(make_lab):
    faults = ModelFaults(fail_always={"A": ModelError("invalid_tool_output")})
    lab = make_lab("db_update_selected", model_faults=faults)
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    assert lab.adapters["A"].calls == 2  # threshold, then handoff
