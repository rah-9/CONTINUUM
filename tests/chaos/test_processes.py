# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Real OS processes: lease races, hard kills, and a stale owner returning (U6, I6)."""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

import pytest

from continuum.core.clock import SystemClock
from continuum.core.errors import AlreadyOwnedError
from continuum.core.ids import RandomIds, SequentialIds
from continuum.core.leases import LeaseManager
from continuum.core.state import ActionStatus, RunStatus
from continuum.core.store import Store
from continuum_lab.harness import build_runtime, fake_router, make_env
from continuum_lab.tasks.catalog import get_task
from tests.conftest import DSN

pytestmark = pytest.mark.chaos


def _race_acquire(schema: str, run_id: str, owner: str, barrier, results) -> None:
    store = Store(DSN, schema=schema, clock=SystemClock(), ids=RandomIds(), pool_size=1)
    barrier.wait()
    try:
        lease = LeaseManager(store, ttl_seconds=30).acquire(run_id, owner)
        results.put(("won", owner, lease.epoch))
    except AlreadyOwnedError:
        results.put(("lost", owner, None))
    finally:
        store.close()


def test_two_processes_racing_to_resume_cannot_both_own(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    # Expire the current owner in DB time so the race is between the two newcomers.
    with lab.store.connection() as conn:
        conn.execute(b"UPDATE run_leases SET expires_at_unix = 0 WHERE run_id = %s",
                     (ctx.run_id,))
        conn.commit()
    spawn = mp.get_context("spawn")
    for _ in range(3):
        barrier, results = spawn.Barrier(2), spawn.Queue()
        procs = [spawn.Process(target=_race_acquire,
                               args=(lab.store.schema, ctx.run_id, f"w{i}", barrier, results))
                 for i in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        outcomes = sorted(results.get(timeout=5)[0] for _ in procs)
        assert outcomes == ["lost", "won"]
        with lab.store.connection() as conn:
            conn.execute(b"UPDATE run_leases SET expires_at_unix = 0 WHERE run_id = %s",
                         (ctx.run_id,))
            conn.commit()


def _killed_worker(schema: str, root: str) -> None:
    """Start a run and hard-kill the process right after the upload commits remotely."""
    definition = get_task("inventory_upload_v1")
    store = Store(DSN, schema=schema, clock=SystemClock(), ids=SequentialIds("k"))
    env = make_env(definition, Path(root) / "services", store_mode="observable")
    router, _ = fake_router(definition)

    class HardKill:
        def checkpoint(self, boundary, **ctx):
            if boundary == "after_response_receipt" and ctx.get("tool") == "store.put":
                os._exit(137)  # no cleanup, no exception handlers, lease left behind

    runtime = build_runtime(definition, store=store, env=env, root=Path(root),
                            clock=SystemClock(), ids=SequentialIds("k"), router=router,
                            faults=HardKill(), lease_ttl_seconds=1)
    runtime.start(definition.spec)


def test_hard_process_kill_after_remote_commit_then_resume(schema, tmp_path, store):
    proc = mp.get_context("spawn").Process(target=_killed_worker,
                                           args=(schema, str(tmp_path)))
    proc.start()
    proc.join(60)
    assert proc.exitcode == 137
    definition = get_task("inventory_upload_v1")
    env = make_env(definition, tmp_path / "services", store_mode="observable")
    assert env.store.commits_for("uploads/inventory.csv") == 1
    with store.read() as q:
        [run_id] = q.run_ids()
    real = Store(DSN, schema=schema, clock=SystemClock(), ids=SequentialIds("r"))
    try:
        router, _ = fake_router(definition)
        runtime = build_runtime(definition, store=real, env=env, root=tmp_path,
                                clock=SystemClock(), ids=SequentialIds("r"), router=router,
                                owner_id="ctl-recover")
        import time

        deadline = time.monotonic() + 10
        while True:  # wait out the dead worker's 1s lease in real DB time
            try:
                result = runtime.resume(run_id)
                break
            except AlreadyOwnedError:
                assert time.monotonic() < deadline
                time.sleep(0.2)
        assert result.status == RunStatus.COMPLETED
        assert env.store.commits_for("uploads/inventory.csv") == 1
        assert len(env.store.attempts_log()) == 1
        assert real.projection_diff(run_id) == []
    finally:
        real.close()


def test_old_owner_returning_after_takeover_cannot_overwrite_state(make_lab):
    """Stale worker reply after lease takeover (docs/07 old_owner_returns)."""
    lab = make_lab("inventory_upload_v1", store_mode="observable")
    holder: dict = {}

    class TakeoverDuringDispatch:
        def checkpoint(self, boundary, **ctx):
            if boundary == "after_response_receipt" and ctx.get("tool") == "store.put" \
                    and "done" not in holder:
                holder["done"] = True
                lab.clock.advance(31)  # old owner stalls past its lease
                holder["result"] = lab.new_runtime(owner_id="ctl-new").resume(ctx["run_id"])

    lab.runtime.gateway.faults = TakeoverDuringDispatch()
    old = lab.runtime.start(lab.definition.spec)
    assert holder["result"].status == RunStatus.COMPLETED  # new owner reconciled + finished
    assert old.status == RunStatus.COMPLETED  # old owner observes, but wrote nothing stale
    assert lab.env.store.commits_for("uploads/inventory.csv") == 1
    with lab.store.read() as q:
        put = next(a for a in q.actions(old.run_id) if a.tool_name == "store.put")
        attempts = q.attempts(put.action_id)
    assert put.status == ActionStatus.CONFIRMED
    assert [a.status for a in attempts] == ["ambiguous"]  # stale "confirmed" never landed
    assert lab.store.projection_diff(old.run_id) == []


def test_lease_ttl_is_not_shortened_by_rounding(schema, store):
    """Regression: `extract(epoch)::bigint` rounds, so a lease taken at x.4s expired at
    x.5s — a 1s TTL lasted ~0.1s. Leases must last their full TTL in real DB time."""
    import time

    from continuum.core.events import RunCreated, make_event

    definition = get_task("db_update_selected")
    real = Store(DSN, schema=schema, clock=SystemClock(), ids=RandomIds(), pool_size=2)
    try:
        with real.run_tx("run_ttl", None, create=True) as tx:
            tx.append(make_event(RunCreated(task_id="t", spec=definition.spec.canonical(),
                                            owner_id="x"), actor="test"))
        leases = LeaseManager(real, ttl_seconds=1)
        for i in range(3):
            while not 0.40 <= time.time() % 1 < 0.45:  # the worst-case sub-second offset
                time.sleep(0.005)
            lease = leases.acquire("run_ttl", f"owner-{i}")
            time.sleep(0.3)
            assert leases.is_active(lease), "lease expired long before its 1s TTL"
            leases.release(lease)
    finally:
        real.close()
