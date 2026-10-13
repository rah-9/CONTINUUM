"""Unknown-action reconciliation through the full runtime (R5, R6, U2, U3, I4)."""

import pytest

from continuum.core.state import ActionStatus, RunStatus
from continuum_lab.tasks.definition import score_ground_truth


def _upload_actions(lab, run_id):
    with lab.store.read() as q:
        return [a for a in q.actions(run_id) if a.tool_name == "store.put"]


@pytest.mark.parametrize("mode", ["deduped", "observable"])
def test_commit_then_drop_is_confirmed_by_authoritative_query(make_lab, mode):
    lab = make_lab("inventory_upload_v1", store_mode=mode)
    lab.env.store.script("put", "commit_then_drop")
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    assert lab.env.store.commits_for("uploads/inventory.csv") == 1  # no duplicate commit
    [put] = _upload_actions(lab, result.run_id)
    assert put.status == ActionStatus.CONFIRMED and put.reconciliation_ref


def test_drop_before_commit_with_valid_key_retries_same_action_and_key(make_lab):
    lab = make_lab("inventory_upload_v1", store_mode="deduped")
    lab.env.store.script("put", "drop_before_commit")
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    attempts = lab.env.store.attempts_log()
    assert len(attempts) == 2
    assert attempts[0]["idempotency_key"] == attempts[1]["idempotency_key"] is not None
    assert lab.env.store.commits_for("uploads/inventory.csv") == 1
    [put] = _upload_actions(lab, result.run_id)
    with lab.store.read() as q:
        assert [a.status for a in q.attempts(put.action_id)] == ["ambiguous", "confirmed"]


def test_deduped_retry_while_original_still_processing_does_not_duplicate(make_lab):
    lab = make_lab("inventory_upload_v1", store_mode="deduped", pending_ops=10)
    lab.env.store.script("put", "commit_late")
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    lab.env.store.settle()
    assert lab.env.store.commits_for("uploads/inventory.csv") == 1


def test_absent_but_pending_commit_is_not_retried_on_negative_read(make_lab):
    """Observable store: the write lands late; an immediate negative read must not retry."""
    lab = make_lab("inventory_upload_v1", store_mode="observable")
    lab.env.store.script("put", "commit_late")
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.AWAITING_RECONCILIATION
    [put] = _upload_actions(lab, result.run_id)
    assert put.status == ActionStatus.NEEDS_HUMAN
    assert len(lab.env.store.attempts_log()) == 1  # never re-dispatched
    lab.env.store.settle()
    assert lab.env.store.commits_for("uploads/inventory.csv") == 1


def test_expired_dedupe_key_falls_back_to_query_then_human(make_lab):
    # Manifest retention mirrors the connector's (10s); the lease TTL is 30s.
    lab = make_lab("inventory_upload_v1", store_mode="deduped", retention_seconds=10)
    lab.env.store.script("put", "commit_late")

    class ExpireAfterAmbiguity:
        def checkpoint(self, boundary, **ctx):
            if boundary == "after_action_outcome_commit" and ctx.get("status") == "unknown":
                lab.clock.advance(20)  # past the dedupe retention, within the lease

    lab.runtime.gateway.faults = ExpireAfterAmbiguity()
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.AWAITING_RECONCILIATION
    assert len(lab.env.store.attempts_log()) == 1  # expired key is not relied upon


def test_opaque_timeout_pauses_for_human_and_never_auto_repeats(make_lab):
    lab = make_lab("notify_opaque")
    lab.env.messages.script("send", "commit_then_drop")
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.AWAITING_RECONCILIATION
    assert len(result.needs_human) == 1
    assert len(lab.env.messages.attempts_log()) == 1
    # A resume without a human decision still does not repeat the send.
    again = lab.new_runtime().resume(result.run_id)
    assert again.status == RunStatus.AWAITING_RECONCILIATION
    assert len(lab.env.messages.attempts_log()) == 1


def test_human_confirmed_with_receipt_then_milestone_still_verified_independently(make_lab):
    lab = make_lab("notify_opaque")
    lab.env.messages.script("send", "commit_then_drop")
    result = lab.runtime.start(lab.definition.spec)
    [action_id] = result.needs_human
    lab.runtime.resolve_action(result.run_id, action_id, "confirmed", actor="ops",
                               reason="seen in provider console", evidence_ref="receipt:msg-1")
    done = lab.new_runtime().resume(result.run_id)
    assert done.status == RunStatus.COMPLETED
    score = score_ground_truth(lab.env, lab.definition.expected)
    assert score.final_state_correct and score.committed_duplicates == 0


def test_human_confirmed_requires_receipt(make_lab):
    lab = make_lab("notify_opaque")
    lab.env.messages.script("send", "commit_then_drop")
    result = lab.runtime.start(lab.definition.spec)
    with pytest.raises(Exception, match="receipt"):
        lab.runtime.resolve_action(result.run_id, result.needs_human[0], "confirmed",
                                   actor="ops", reason="trust me")


def test_human_abort_fails_run_without_verified_completion(make_lab):
    lab = make_lab("notify_opaque")
    lab.env.messages.script("send", "commit_then_drop")
    result = lab.runtime.start(lab.definition.spec)
    out = lab.runtime.resolve_action(result.run_id, result.needs_human[0], "abort",
                                     actor="ops", reason="customer complained")
    assert out.status == RunStatus.FAILED and out.failure_code == "human_abort"


def test_retry_at_risk_is_explicit_and_may_duplicate(make_lab):
    lab = make_lab("notify_opaque")
    lab.env.messages.script("send", "commit_then_drop")
    result = lab.runtime.start(lab.definition.spec)
    action_id = result.needs_human[0]
    lab.runtime.resolve_action(result.run_id, action_id, "retry_at_risk", actor="ops",
                               reason="provider says unsent")
    done = lab.new_runtime().resume(result.run_id)
    assert done.status == RunStatus.COMPLETED
    score = score_ground_truth(lab.env, lab.definition.expected)
    assert score.committed_duplicates == 1  # the disclosed risk materialised
    with lab.store.read() as q:
        events = q.events(result.run_id)
    overrides = [e for e in events if e.type.value == "ActionAuthorized"
                 and e.payload.get("at_risk_override")]
    assert len(overrides) == 1 and "AT-RISK" in overrides[0].payload["reason"]
