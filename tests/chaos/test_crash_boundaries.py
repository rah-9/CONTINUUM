# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Safety matrix: kill at every write-ahead/commit boundary, then resume (docs/02 s9).

After each injected crash the run is resumed (as a new controller) until it is
terminal or explicitly paused. Invariants checked at the end of every cell:

* projections equal a replay of the event log; event sequence is gapless (I1, I2)
* no unintended committed duplicate effect on deduped/observable services
* the run either completed with correct ground truth or paused explicitly
"""

import itertools

import pytest

from continuum.core.faults import BOUNDARIES, SimulatedCrash
from continuum.core.state import RunStatus
from continuum_lab.fault_injection import CrashInjector, CrashRule
from continuum_lab.tasks.definition import score_ground_truth

MODES = ["deduped", "observable"]
# Boundaries hit once per clean run of the task are only crashed at occurrence 1.
SINGLE = {"after_artifact_write", "after_lease_acquired", "after_model_switch_commit"}
CELLS = [(b, n) for b in sorted(BOUNDARIES) for n in ((1,) if b in SINGLE else (1, 3))]


def _nth(n):
    seen = itertools.count(1)
    return lambda ctx: next(seen) == n


@pytest.mark.chaos
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("boundary", "occurrence"), CELLS)
def test_crash_at_boundary_then_resume_preserves_invariants(make_lab, boundary, occurrence,
                                                            mode):
    crash = CrashInjector(CrashRule(boundary, when=_nth(occurrence)))
    lab = make_lab("upload_and_notify", faults=crash, store_mode=mode, msg_mode=mode)
    crashed = False
    try:
        result = lab.runtime.start(lab.definition.spec)
    except SimulatedCrash:
        crashed = True
    # Every cell must actually exercise its boundary (no vacuous passes).
    assert crashed, f"{boundary}#{occurrence} never fired"
    with lab.store.read() as q:
        run_id = q.run_ids()[-1] if q.run_ids() else None
    if run_id is None:
        assert crashed  # crashed before RunCreated committed: nothing to resume
        return
    owner = 2
    for _ in range(4):
        with lab.store.read() as q:
            status = q.run(run_id).status
        if status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.AWAITING_RECONCILIATION):
            break
        lab.clock.advance(31)
        result = lab.new_runtime(owner_id=f"ctl-{owner}").resume(run_id)
        owner += 1
    with lab.store.read() as q:
        status = q.run(run_id).status
        seqs = [e.seq for e in q.events(run_id)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert lab.store.projection_diff(run_id) == []
    score = score_ground_truth(lab.env, lab.definition.expected)
    assert score.committed_duplicates == 0, score.detail
    assert status in (RunStatus.COMPLETED, RunStatus.AWAITING_RECONCILIATION), (status, result)
    if status == RunStatus.COMPLETED:
        assert score.final_state_correct


@pytest.mark.chaos
@pytest.mark.parametrize("boundary", ["after_response_receipt", "before_dispatch",
                                      "after_attempt_start_commit"])
def test_opaque_service_crash_never_auto_repeats(make_lab, boundary):
    def on_send(ctx):
        return ctx.get("tool") == "message.send"

    crash = CrashInjector(CrashRule(boundary, when=on_send))
    lab = make_lab("notify_opaque", faults=crash)
    with pytest.raises(SimulatedCrash):
        lab.runtime.start(lab.definition.spec)
    with lab.store.read() as q:
        run_id = q.run_ids()[-1]
    lab.clock.advance(31)
    result = lab.new_runtime().resume(run_id)
    assert result.status == RunStatus.AWAITING_RECONCILIATION
    sends = lab.env.messages.attempts_log()
    assert len(sends) <= 1  # the ambiguous send was never repeated automatically
