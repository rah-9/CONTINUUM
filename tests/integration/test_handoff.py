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


def _packet_from(adapter):
    """The continuation packet the model was given at the start of its session."""
    import json

    first = adapter.seen[0]
    packet_msg = next(m for m in first if m.content and "Continuation packet" in m.content)
    return json.loads(packet_msg.content.split("\n", 1)[1])


def test_context_overflow_prefers_a_model_with_a_larger_window(make_lab):
    faults = ModelFaults(errors={"A": {1: ModelError("context_overflow")}},
                         context_window={"A": 64_000, "B": 32_000, "C": 200_000})
    lab = make_lab("db_update_selected", model_faults=faults)
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    [switch] = _events(lab, result.run_id, "ModelSwitched")
    assert switch.payload["to_model_alias"] == "C"  # larger window wins over policy order
    assert lab.adapters["B"].calls == 0


def test_other_failures_keep_operator_order(make_lab):
    faults = ModelFaults(fail_always={"A": ModelError("provider_unavailable")},
                         context_window={"A": 64_000, "B": 32_000, "C": 200_000})
    lab = make_lab("db_update_selected", model_faults=faults)
    result = lab.runtime.start(lab.definition.spec)
    [switch] = _events(lab, result.run_id, "ModelSwitched")
    assert switch.payload["to_model_alias"] == "B"


def test_packet_carries_handoff_provenance_and_verified_state(make_lab):
    # A finishes m1 (normalize) and m2 (upload), then fails on its 3rd call; B must
    # continue from verified state and only do m3.
    faults = ModelFaults(errors={"A": {3: ModelError("provider_unavailable"),
                                       4: ModelError("provider_unavailable"),
                                       5: ModelError("provider_unavailable")}})
    lab = make_lab("csv_normalize_categories", model_faults=faults)
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    packet = _packet_from(lab.adapters["B"])
    assert packet["handoff"] == {"reason": "provider_unavailable", "handoff_count": 1,
                                 "from_model_id": "fake/A"}
    assert [m["id"] for m in packet["verified_milestones"]] == ["m1", "m2"]
    assert [m["id"] for m in packet["pending_milestones"]] == ["m3"]
    assert {c["operation_key"] for c in packet["confirmed_actions"]} == {
        "m1:csv.normalize:artifacts/normalized.csv", "m2:store.put:uploads/categories.csv"}
    assert lab.env.store.commits_for("uploads/categories.csv") == 1  # B did not redo it
    assert _packet_from(lab.adapters["A"])["handoff"] is None  # a fresh start has none
