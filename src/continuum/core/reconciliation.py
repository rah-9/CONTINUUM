# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Unknown-action reconciliation (docs/04 section 6, R5, I4).

Decision table, applied per ``unknown`` action using its **trusted** manifest:

* read-only                             -> re-executable; mark authorized (no effect)
* authoritative positive observation    -> confirmed with evidence
* idempotent write, key within retention-> same action/key/args retry
* negative observation, race-safe proof -> authorized (safe absence, evidence recorded)
* negative observation otherwise        -> needs_human (absence not proven)
* opaque / retention expired            -> needs_human

A "not found" from a lagging query is never treated as proof of absence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from continuum.core.clock import Clock
from continuum.core.events import ActionNeedsHuman, ActionReconciled, make_event
from continuum.core.ledger import assert_transition
from continuum.core.state import ActionStatus
from continuum.core.store import ActionRow, Store
from continuum.tools.gateway import RunContext, ToolGateway
from continuum.tools.manifest import ToolManifest, ToolRegistry


@dataclass
class ReconcileReport:
    confirmed: list[str] = field(default_factory=list)
    retried: list[str] = field(default_factory=list)
    authorized: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)
    still_unknown: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return not self.needs_human and not self.still_unknown


class Reconciler:
    def __init__(self, *, store: Store, gateway: ToolGateway, registry: ToolRegistry,
                 clock: Clock) -> None:
        self.store = store
        self.gateway = gateway
        self.registry = registry
        self.clock = clock

    def reconcile(self, run: RunContext) -> ReconcileReport:
        report = ReconcileReport()
        with self.store.read() as q:
            unknown = q.actions(run.run_id, [ActionStatus.UNKNOWN.value])
            needs_human = q.actions(run.run_id, [ActionStatus.NEEDS_HUMAN.value])
        report.needs_human.extend(a.action_id for a in needs_human)
        for action in unknown:
            self._one(run, action, report)
        return report

    # ---- per action ------------------------------------------------------------------

    def _one(self, run: RunContext, action: ActionRow, report: ReconcileReport) -> None:
        actor = f"reconciler:{run.lease.owner_id}"
        tool = self.registry.get(action.tool_name)
        if tool is None:
            self._needs_human(run, action, "tool no longer registered", actor, report)
            return
        m = tool.manifest
        if m.effect == "read":
            self._reconciled(run, action, "authorized", "read_only_no_effect",
                             {"reason": "read-only tool; no external effect"}, actor)
            report.authorized.append(action.action_id)
            return

        if m.reconciliation.method == "authoritative_query":
            observation = self._observe(run, m, action)
            if observation is None:
                report.still_unknown.append(action.action_id)  # transient; try again later
                return
            if self._positive(m, action, observation):
                self._reconciled(run, action, "confirmed", "authoritative_query",
                                 {"observation": observation}, actor)
                report.confirmed.append(action.action_id)
                return
            if self._within_retention(m, action):
                self._idempotent_retry(run, action, actor, report)
                return
            if m.reconciliation.negative_proof_is_safe:
                self._reconciled(run, action, "authorized", "race_safe_absence",
                                 {"observation": observation}, actor)
                report.authorized.append(action.action_id)
                return
            self._needs_human(run, action, "negative observation does not prove absence",
                              actor, report)
            return

        if m.idempotency.supported and self._within_retention(m, action):
            self._idempotent_retry(run, action, actor, report)
            return
        reason = ("opaque effect; cannot infer outcome" if m.reconciliation.method == "opaque"
                  else "idempotency retention expired and no authoritative query")
        self._needs_human(run, action, reason, actor, report)

    def _within_retention(self, m: ToolManifest, action: ActionRow) -> bool:
        if not m.idempotency.supported or action.idempotency_key is None:
            return False
        retention = m.idempotency.retention_seconds
        if retention is None:
            return True
        with self.store.read() as q:
            attempts = q.attempts(action.action_id)
        if not attempts:
            return False
        first = attempts[0].started_at
        # The key was first used at the first attempt; dedupe is only relied on
        # strictly inside the documented window.
        return self.clock.now() < first + timedelta(seconds=retention)

    def _observe(self, run: RunContext, m: ToolManifest, action: ActionRow
                 ) -> dict[str, Any] | None:
        check = m.reconciliation.check_tool
        assert check is not None
        args = {k: action.args_json[k] for k in m.reconciliation.check_args}
        outcome = self.gateway.propose(run, check, args, caller="reconciler",
                                       milestone_id=action.milestone_id)
        if outcome.status != "ok" or not isinstance(outcome.data, dict):
            return None
        return outcome.data

    @staticmethod
    def _positive(m: ToolManifest, action: ActionRow, obs: dict[str, Any]) -> bool:
        if not obs.get("exists"):
            return False
        return all(obs.get(f) == action.args_json.get(f) for f in m.reconciliation.positive_proof)

    def _idempotent_retry(self, run: RunContext, action: ActionRow, actor: str,
                          report: ReconcileReport) -> None:
        outcome = self.gateway.dispatch_authorized(run, action.action_id, actor=actor,
                                                   retry_reason="idempotent_same_key")
        report.retried.append(action.action_id)
        if outcome.status == "ok":
            report.confirmed.append(action.action_id)
        elif outcome.status == "unknown":
            # A second ambiguous outcome: stop automating and ask a human.
            with self.store.read() as q:
                fresh = q.action(action.action_id)
            self._needs_human(run, fresh, "idempotent retry outcome also ambiguous", actor,
                              report)
        elif outcome.status != "failed_clean":
            report.still_unknown.append(action.action_id)

    def _reconciled(self, run: RunContext, action: ActionRow, to_status: str, method: str,
                    evidence: dict[str, Any], actor: str) -> None:
        target = ActionStatus(to_status)
        assert_transition(action.status, target)
        evidence_ref = run.workspace.put_blob({"action_id": action.action_id,
                                               "method": method, **evidence})
        with self.store.run_tx(run.run_id, run.lease.fence) as tx:
            tx.append(make_event(ActionReconciled(
                action_id=action.action_id, to_status=to_status,
                method=method, evidence_ref=evidence_ref,
                outcome_ref=evidence_ref if target == ActionStatus.CONFIRMED else None),
                actor=actor))

    def _needs_human(self, run: RunContext, action: ActionRow, reason: str, actor: str,
                     report: ReconcileReport) -> None:
        if action.status == ActionStatus.UNKNOWN:
            assert_transition(action.status, ActionStatus.NEEDS_HUMAN)
            with self.store.run_tx(run.run_id, run.lease.fence) as tx:
                tx.append(make_event(ActionNeedsHuman(action_id=action.action_id,
                                                      reason=reason), actor=actor))
        report.needs_human.append(action.action_id)
