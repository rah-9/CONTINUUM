# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Event store atomicity, fencing, immutability and rebuild (I1, I2, I6)."""

import psycopg
import pytest

from continuum.core.errors import StaleOwnerError
from continuum.core.events import (
    EventType,
    FaultInjected,
    NewEvent,
    RunActiveTimeRecorded,
    make_event,
)
from continuum.core.store import Fence


def _evt(ms=1):
    return make_event(RunActiveTimeRecorded(elapsed_ms=ms), actor="test")


def test_sequences_are_gapless_and_strictly_increasing(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    for _ in range(5):
        lab.store.append(ctx.run_id, ctx.lease.fence, [_evt()])
    with lab.store.read() as q:
        seqs = [e.seq for e in q.events(ctx.run_id)]
        run = q.run(ctx.run_id)
    assert seqs == list(range(1, len(seqs) + 1))
    assert run.last_event_seq == seqs[-1]


def test_event_and_projection_commit_or_roll_back_together(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    with lab.store.read() as q:
        before_seq, before_ms = q.run(ctx.run_id).last_event_seq, q.usage(
            ctx.run_id).active_elapsed_ms
    with pytest.raises(RuntimeError), lab.store.run_tx(ctx.run_id, ctx.lease.fence) as tx:
        tx.append(_evt(500))
        raise RuntimeError("crash inside transaction")
    with lab.store.read() as q:
        assert q.run(ctx.run_id).last_event_seq == before_seq
        assert q.usage(ctx.run_id).active_elapsed_ms == before_ms
    assert lab.store.projection_diff(ctx.run_id) == []


def test_invalid_payload_is_rejected_before_commit(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    bad = NewEvent(type=EventType.RUN_ACTIVE_TIME_RECORDED, actor="t", payload={"oops": 1})
    with pytest.raises(ValueError):
        lab.store.append(ctx.run_id, ctx.lease.fence, [bad])


def test_stale_or_missing_fence_cannot_write(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    with pytest.raises(StaleOwnerError):
        lab.store.append(ctx.run_id, Fence("ctl-1", ctx.lease.epoch + 1), [_evt()])
    with pytest.raises(StaleOwnerError):
        lab.store.append(ctx.run_id, Fence("intruder", ctx.lease.epoch), [_evt()])
    with pytest.raises(StaleOwnerError), lab.store.run_tx(ctx.run_id, None):
        pass
    lab.clock.advance(lab.runtime.leases.ttl_seconds + 1)  # lease expires
    with pytest.raises(StaleOwnerError):
        lab.store.append(ctx.run_id, ctx.lease.fence, [_evt()])


def test_events_are_append_only(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    with lab.store.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute(b"UPDATE events SET actor = 'x' WHERE run_id = %s", (ctx.run_id,))
    with lab.store.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute(b"DELETE FROM events WHERE run_id = %s", (ctx.run_id,))


def test_damaged_projection_is_detected_and_rebuilt_exactly(make_lab):
    lab = make_lab("inventory_upload_v1")
    result = lab.runtime.start(lab.definition.spec)
    run_id = result.run_id
    assert lab.store.projection_diff(run_id) == []
    with lab.store.connection() as conn:
        conn.execute(b"UPDATE milestones SET status = 'pending' WHERE run_id = %s", (run_id,))
        conn.execute(b"UPDATE usage_totals SET model_calls = 999 WHERE run_id = %s", (run_id,))
        conn.commit()
    diff = lab.store.projection_diff(run_id)
    assert any("milestones" in d for d in diff) and any("model_calls" in d for d in diff)
    lease = lab.runtime.leases.acquire(run_id, "repair")
    lab.store.rebuild_projection(run_id, lease.fence)
    assert lab.store.projection_diff(run_id) == []


def test_fault_events_are_audit_only(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    lab.store.append(ctx.run_id, ctx.lease.fence,
                     [make_event(FaultInjected(boundary="x"), actor="lab")])
    assert lab.store.projection_diff(ctx.run_id) == []
