"""Stateful property testing of the effect ledger (I1, I2, I4, I5, R5, R6, U2, U3).

Hypothesis drives random interleavings of:

* model proposals for a few records, with random service behaviour per call
  (ok, commit-then-drop, drop-before-commit, late commit, clean rejection) and
  occasionally *changed arguments* for the same record;
* controller crashes followed by takeover + recovery (lease expiry, new epoch);
* reconciliation passes;
* a correct human operator resolving ``needs_human`` actions from ground truth.

After every step the invariants below must hold, for each connector mode.
"""

from __future__ import annotations

import itertools
import os
import uuid
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from continuum.core.ids import SequentialIds
from continuum.core.ledger import mark_interrupted_attempts
from continuum.core.state import ActionStatus
from continuum_lab.harness import build_runtime, fake_router, make_env
from continuum_lab.tasks.catalog import get_task
from tests.conftest import Lab

pytestmark = pytest.mark.property

RECORDS = ["r3", "r4", "r5"]
BEHAVIOURS = ["ok", "ok", "commit_then_drop", "drop_before_commit", "commit_late", "reject"]
WRITES_BLOCKING = {ActionStatus.UNKNOWN, ActionStatus.NEEDS_HUMAN, ActionStatus.IN_FLIGHT}


def _machine(store, clock, tmp_path, mode: str) -> type[RuleBasedStateMachine]:
    definition = get_task("db_partial_batch")
    counter = itertools.count()

    class Ledger(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            n = next(counter)
            self.env = make_env(definition, tmp_path / f"svc-{mode}-{n}", db_mode=mode,
                                pending_ops=3, now_fn=lambda: clock.now().timestamp())
            router, _ = fake_router(definition)
            self.owner = 1
            self.runtime = build_runtime(definition, store=store, env=self.env,
                                         root=tmp_path / f"root-{mode}-{n}", clock=clock,
                                         ids=SequentialIds(f"p{n}x"), router=router,
                                         owner_id="ctl-1")
            lab = Lab(definition, self.env, router, {}, self.runtime, tmp_path, store, clock)
            self.ctx = lab.open_run(f"run_{mode}_{n}_{uuid.uuid4().hex[:6]}")
            self.human_retries: dict[str, int] = dict.fromkeys(RECORDS, 0)

        # ---- helpers ------------------------------------------------------------------
        def _actions(self) -> list[Any]:
            with store.read() as q:
                return q.actions(self.ctx.run_id)

        def _committed(self, record: str) -> int:
            return self.env.db.commits_for(record)

        def _pending(self, record: str) -> bool:
            with self.env.db._txn() as s:
                return any(p["target"] == record for p in s["pending"])

        # ---- rules ---------------------------------------------------------------------
        @rule(record=st.sampled_from(RECORDS), behaviour=st.sampled_from(BEHAVIOURS),
              variant=st.sampled_from(["shipped", "shipped", "returned"]))
        def propose(self, record: str, behaviour: str, variant: str) -> None:
            unresolved = [a for a in self._actions() if a.status in WRITES_BLOCKING]
            requests_before = len(self.env.db.attempts_log())
            self.env.db.script("update", behaviour)  # type: ignore[arg-type]
            out = self.runtime.gateway.propose(
                self.ctx, "db.update", {"record_id": record, "fields": {"status": variant}},
                caller="model", milestone_id="m1")
            dispatched = len(self.env.db.attempts_log()) > requests_before
            # I4: while any effect is unresolved, no proposal may reach the service.
            assert not (unresolved and dispatched), (out, unresolved)

        @rule()
        def crash_and_take_over(self) -> None:
            clock.advance(31)  # the controller died; its lease expires
            self.owner += 1
            lease = self.runtime.leases.acquire(self.ctx.run_id, f"ctl-{self.owner}")
            self.ctx = type(self.ctx)(self.ctx.run_id, lease, self.ctx.spec, self.ctx.workspace)
            with store.run_tx(self.ctx.run_id, lease.fence) as tx:
                mark_interrupted_attempts(tx, actor="recovery")

        @rule()
        def reconcile(self) -> None:
            self.runtime.reconciler.reconcile(self.ctx)

        @rule()
        def human_resolves_from_ground_truth(self) -> None:
            # A run waiting on a human has released its controller lease; resolution
            # never steals a live lease (that refusal is itself tested elsewhere).
            self.runtime.leases.release(self.ctx.lease)
            for a in self._actions():
                if a.status not in (ActionStatus.NEEDS_HUMAN, ActionStatus.UNKNOWN):
                    continue
                record = a.args_json["record_id"]
                if self._pending(record):
                    continue  # a careful operator waits for an in-progress request
                self.runtime.leases.release(self.ctx.lease)
                if self._committed(record) >= 1:
                    self.runtime.resolve_action(self.ctx.run_id, a.action_id, "confirmed",
                                                actor="op", reason="seen in db",
                                                evidence_ref=f"db:{record}")
                else:
                    self.runtime.resolve_action(self.ctx.run_id, a.action_id,
                                                "retry_at_risk", actor="op",
                                                reason="db shows no write")
                    self.human_retries[record] += 1
                    self.ctx = self._reacquire()
                    self.runtime.gateway.dispatch_authorized(self.ctx, a.action_id,
                                                             actor="human:op")
                    self.runtime.leases.release(self.ctx.lease)
            self.ctx = self._reacquire()

        def _reacquire(self) -> Any:
            lease = self.runtime.leases.acquire(self.ctx.run_id,
                                                self.runtime.config.owner_id)
            return type(self.ctx)(self.ctx.run_id, lease, self.ctx.spec, self.ctx.workspace)

        # ---- invariants ------------------------------------------------------------------
        @invariant()
        def projections_equal_replay(self) -> None:  # I1, I2
            assert store.projection_diff(self.ctx.run_id) == []

        @invariant()
        def one_action_per_operation(self) -> None:  # I5
            keys = [a.operation_key for a in self._actions()
                    if not a.operation_key.startswith("read:")]
            assert len(keys) == len(set(keys))

        @invariant()
        def no_duplicate_commit(self) -> None:  # R5/R6: no unsafe repeats
            for record in RECORDS:
                assert self._committed(record) <= 1, (record, self.env.db.ground_truth())

        @invariant()
        def no_blind_redispatch(self) -> None:
            """Without dedupe, every extra physical request is explained by a definitive
            rejection or an explicit human at-risk retry — never an automatic guess."""
            if mode == "deduped":
                return
            log = self.env.db.attempts_log()
            for record in RECORDS:
                requests = sum(1 for r in log if r["target"] == record)
                rejected = sum(1 for r in log
                               if r["target"] == record and r["behaviour"] == "reject")
                allowed = 1 + rejected + self.human_retries[record]
                assert requests <= allowed, (record, requests, allowed)

    return Ledger


@pytest.mark.parametrize("mode", ["deduped", "observable", "opaque"])
def test_ledger_invariants_hold_under_random_interleavings(store, clock, tmp_path, mode):
    machine = _machine(store, clock, tmp_path, mode)
    run_state_machine_as_test(machine, settings=settings(
        # CONTINUUM_PROPERTY_EXAMPLES scales the search (CI default 20; deep runs 150+).
        max_examples=int(os.environ.get("CONTINUUM_PROPERTY_EXAMPLES", "20")),
        stateful_step_count=12, deadline=None,
        suppress_health_check=list(HealthCheck)))
