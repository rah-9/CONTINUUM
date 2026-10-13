"""Tool gateway: identity, dedupe, conflict, permissions, blocking (R4, I4, I5)."""

from continuum.core.state import ActionStatus

NORMALIZE = {"source": "inputs/inventory.csv", "output": "artifacts/normalized.csv",
             "lowercase_columns": ["Category"]}
PUT = {"object_path": "uploads/inventory.csv", "artifact_ref": "artifacts/normalized.csv"}


def _prepared(lab):
    ctx = lab.open_run()
    gw = lab.runtime.gateway
    assert gw.propose(ctx, "csv.normalize", NORMALIZE, caller="model",
                      milestone_id="m1").status == "ok"
    return ctx, gw


def _actions(lab, ctx):
    with lab.store.read() as q:
        return [a for a in q.actions(ctx.run_id) if not a.operation_key.startswith("read:")]


def test_duplicate_proposal_reuses_action_and_never_redispatches(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx, gw = _prepared(lab)
    first = gw.propose(ctx, "store.put", PUT, caller="model", milestone_id="m2",
                       provider_call_id="call-A")
    again = gw.propose(ctx, "store.put", PUT, caller="model", milestone_id="m2",
                       provider_call_id="different-provider-id")
    assert first.status == "ok" and again.status == "deduped"
    assert again.action_id == first.action_id and again.data == first.data
    assert lab.env.store.commits_for("uploads/inventory.csv") == 1
    assert len(lab.env.store.attempts_log()) == 1
    puts = [a for a in _actions(lab, ctx) if a.tool_name == "store.put"]
    assert len(puts) == 1 and puts[0].idempotency_key is not None


def test_same_identity_different_args_is_intent_conflict(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx, gw = _prepared(lab)
    assert gw.propose(ctx, "store.put", PUT, caller="model", milestone_id="m2").status == "ok"
    ctx.workspace.write_bytes("artifacts/other.csv", b"different")
    conflict = gw.propose(ctx, "store.put", {**PUT, "artifact_ref": "artifacts/other.csv"},
                          caller="model", milestone_id="m2")
    assert conflict.status == "conflict" and conflict.code == "intent_conflict"
    assert lab.env.store.commits_for("uploads/inventory.csv") == 1


def test_model_cannot_use_unlisted_tool_or_bypass_permissions(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx, gw = _prepared(lab)
    assert gw.propose(ctx, "store.head", {"object_path": "uploads/x"}, caller="model",
                      milestone_id="m2").code == "not_allowlisted"
    assert gw.propose(ctx, "shell.exec", {"cmd": "rm -rf /"}, caller="model",
                      milestone_id="m2").code == "unknown_tool"
    assert gw.propose(ctx, "store.put", {**PUT, "object_path": "secret/x"}, caller="model",
                      milestone_id="m2").code == "path_not_permitted"
    assert gw.propose(ctx, "store.put", {**PUT, "extra": 1}, caller="model",
                      milestone_id="m2").code == "invalid_args"
    assert gw.propose(ctx, "store.put", PUT, caller="verifier",
                      milestone_id="m2").code == "read_only_caller"
    assert lab.env.store.ground_truth() == []


def test_secrets_in_arguments_are_rejected(make_lab, monkeypatch):
    monkeypatch.setenv("STORE_TOKEN", "tok-very-secret-value")
    lab = make_lab("db_update_selected")
    from continuum.core.redaction import Redactor

    lab.runtime.gateway.redactor = Redactor.from_environment()
    ctx = lab.open_run()
    out = lab.runtime.gateway.propose(
        ctx, "db.update", {"record_id": "r1", "fields": {"note": "tok-very-secret-value"}},
        caller="model", milestone_id="m1")
    assert out.code == "secret_in_args"
    assert lab.env.db.ground_truth() == []


def test_ambiguous_write_becomes_unknown_and_blocks_new_writes(make_lab):
    lab = make_lab("db_partial_batch")
    lab.env.db.script("update", "commit_then_drop")
    ctx = lab.open_run()
    gw = lab.runtime.gateway
    out = gw.propose(ctx, "db.update", {"record_id": "r3", "fields": {"status": "shipped"}},
                     caller="model", milestone_id="m1")
    assert out.status == "unknown"  # never reported as a retriable error
    blocked = gw.propose(ctx, "db.update", {"record_id": "r4", "fields": {"status": "shipped"}},
                         caller="model", milestone_id="m1")
    assert blocked.status == "blocked"
    same = gw.propose(ctx, "db.update", {"record_id": "r3", "fields": {"status": "shipped"}},
                      caller="model", milestone_id="m1")
    assert same.status == "blocked"
    assert lab.env.db.commits_for("r3") == 1 and lab.env.db.commits_for("r4") == 0
    reads = gw.propose(ctx, "db.get", {"record_id": "r3"}, caller="verifier",
                       milestone_id="m1")
    assert reads.status == "ok"  # reads are still allowed while a write is unknown


def test_reads_are_fresh_each_time_never_deduped(make_lab):
    lab = make_lab("db_update_selected")
    ctx = lab.open_run()
    gw = lab.runtime.gateway
    a = gw.propose(ctx, "db.get", {"record_id": "r1"}, caller="model", milestone_id="m1")
    b = gw.propose(ctx, "db.get", {"record_id": "r1"}, caller="model", milestone_id="m1")
    assert a.status == b.status == "ok" and a.action_id != b.action_id


def test_clean_rejection_is_failed_clean_and_bounded_retry(make_lab):
    lab = make_lab("db_update_selected")
    lab.env.db.script("update", "reject", "reject", "reject", "reject")
    ctx = lab.open_run()
    gw = lab.runtime.gateway
    args = {"record_id": "r1", "fields": {"status": "shipped"}}
    statuses = [gw.propose(ctx, "db.update", args, caller="model", milestone_id="m1").status
                for _ in range(4)]
    assert statuses == ["failed_clean", "failed_clean", "failed_clean", "rejected"]
    assert lab.env.db.ground_truth() == []


def test_stale_owner_cannot_dispatch(make_lab):
    lab = make_lab("db_update_selected")
    ctx = lab.open_run()
    lab.clock.advance(lab.runtime.leases.ttl_seconds + 1)
    lab.runtime.leases.acquire(ctx.run_id, "ctl-2")  # takeover
    out = lab.runtime.gateway.propose(ctx, "db.update",
                                      {"record_id": "r1", "fields": {"status": "shipped"}},
                                      caller="model", milestone_id="m1")
    assert out.status == "lease_lost"
    assert lab.env.db.attempts_log() == []


def test_lease_lost_between_attempt_commit_and_dispatch_leaves_started_attempt(make_lab):
    from continuum_lab.fault_injection import CrashInjector

    lab = make_lab("db_update_selected")
    ctx = lab.open_run()

    class ExpireBeforeDispatch(CrashInjector):
        def checkpoint(self, boundary, **context):
            if boundary == "after_attempt_start_commit":
                lab.clock.advance(lab.runtime.leases.ttl_seconds + 1)
                lab.runtime.leases.acquire(ctx.run_id, "ctl-2")

    lab.runtime.gateway.faults = ExpireBeforeDispatch()
    out = lab.runtime.gateway.propose(ctx, "db.update",
                                      {"record_id": "r1", "fields": {"status": "shipped"}},
                                      caller="model", milestone_id="m1")
    assert out.status == "lease_lost"
    assert lab.env.db.attempts_log() == []
    with lab.store.read() as q:
        [attempt] = q.started_attempts(ctx.run_id)
        assert q.action(attempt.action_id).status == ActionStatus.IN_FLIGHT


def test_tool_schemas_come_only_from_the_task_allowlist(make_lab):
    lab = make_lab("inventory_upload_v1")
    names = [s["function"]["name"] for s in lab.runtime.gateway.tool_schemas(
        lab.definition.spec)]
    assert names == list(lab.definition.spec.tools_allowlist)
    assert "store.head" not in names  # registered for verifiers, not offered to models


def test_existing_retryable_write_is_blocked_while_another_effect_is_unresolved(make_lab):
    lab = make_lab("db_partial_batch")
    lab.env.db.script("update", "reject", "commit_then_drop")
    ctx = lab.open_run()
    gw = lab.runtime.gateway
    r3 = {"record_id": "r3", "fields": {"status": "shipped"}}
    r4 = {"record_id": "r4", "fields": {"status": "shipped"}}
    assert gw.propose(ctx, "db.update", r3, caller="model", milestone_id="m1").status == \
        "failed_clean"
    assert gw.propose(ctx, "db.update", r4, caller="model", milestone_id="m1").status == \
        "unknown"
    retry = gw.propose(ctx, "db.update", r3, caller="model", milestone_id="m1")
    assert retry.status == "blocked"  # r3 may not retry while r4's effect is unknown
    assert lab.env.db.commits_for("r3") == 0


def test_cancelled_run_dispatches_nothing(make_lab):
    lab = make_lab("db_update_selected")
    ctx = lab.open_run()
    lab.runtime.leases.release(ctx.lease)
    lab.runtime.cancel(ctx.run_id, actor="ops", reason="stop")
    result = lab.new_runtime().resume(ctx.run_id)
    assert result.status.value == "cancelled"
    assert lab.env.db.attempts_log() == []
