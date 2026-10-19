# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Milestone verification, drift detection and cascading invalidation (R7, I3, I7).

Only this component records ``MilestoneVerified``. A verifier passes only with
a fingerprint over its inputs, dependency evidence and version; any later
change invalidates the milestone and all its descendants.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from continuum.core.errors import NotFoundError, VerificationUnavailableError
from continuum.core.events import (
    ArtifactDrift,
    MilestoneFailed,
    MilestoneInvalidated,
    MilestoneVerified,
    NewEvent,
    RunFailed,
    VerificationStarted,
    VerifierError,
    make_event,
)
from continuum.core.evidence import input_fingerprint
from continuum.core.faults import FaultInjector
from continuum.core.journal import RunJournal
from continuum.core.redaction import Redactor
from continuum.core.state import (
    ActionStatus,
    ArtifactState,
    MilestoneStatus,
    VerifierResult,
    focus_milestone,
)
from continuum.core.store import MilestoneRow, Store
from continuum.tools.gateway import RunContext, ToolGateway
from continuum.verify.registry import (
    ObservationError,
    VerifierContext,
    VerifierOutcome,
    VerifierRegistry,
)


class MilestoneVerification:
    def __init__(self, *, store: Store, verifiers: VerifierRegistry, gateway: ToolGateway,
                 journal: RunJournal, redactor: Redactor, faults: FaultInjector,
                 is_write: Callable[[str], bool]) -> None:
        self.store = store
        self.verifiers = verifiers
        self.gateway = gateway
        self.journal = journal
        self.redactor = redactor
        self.faults = faults
        self.is_write = is_write

    # ---- queries ------------------------------------------------------------------------

    def statuses(self, ctx: RunContext) -> dict[str, MilestoneStatus]:
        with self.store.read() as q:
            return q.milestone_statuses(ctx.run_id)

    def focus(self, ctx: RunContext) -> str | None:
        return focus_milestone(ctx.spec, self.statuses(ctx))

    def milestone(self, ctx: RunContext, milestone_id: str) -> MilestoneRow:
        with self.store.read() as q:
            for m in q.milestones(ctx.run_id):
                if m.milestone_id == milestone_id:
                    return m
        raise NotFoundError(milestone_id)

    # ---- verification -------------------------------------------------------------------

    def _safe(self, message: str) -> str:
        return self.redactor.text(message).replace("\x00", "�")[:500]

    def evaluate(self, ctx: RunContext, milestone_id: str) -> tuple[VerifierOutcome, str]:
        """Run the verifier read-only and compute the input fingerprint (no events)."""
        spec_m = ctx.spec.milestone(milestone_id)
        verifier = self.verifiers.get(spec_m.verifier_id)
        assert verifier is not None
        with self.store.read() as q:
            rows = {m.milestone_id: m for m in q.milestones(ctx.run_id)}
            confirmed = q.actions(ctx.run_id, [ActionStatus.CONFIRMED.value])
        dep_refs = {d: rows[d].evidence_ref for d in spec_m.depends_on}

        def observe(tool: str, args: dict[str, Any]) -> dict[str, Any]:
            out = self.gateway.propose(ctx, tool, args, caller="verifier",
                                       milestone_id=milestone_id)
            if out.status != "ok" or not isinstance(out.data, dict):
                raise ObservationError(f"{tool}: {out.status} {out.code or ''}")
            return out.data

        vctx = VerifierContext(run_id=ctx.run_id, milestone_id=milestone_id, spec=ctx.spec,
                               workspace=ctx.workspace, confirmed_actions=confirmed,
                               dependency_evidence=dep_refs, observe=observe)
        try:
            outcome = verifier.verify(vctx)
        except ObservationError as exc:
            outcome = VerifierOutcome(VerifierResult.ERROR, str(exc))
        fingerprint = input_fingerprint(
            input_artifact_hashes=outcome.input_artifacts,
            external_input_versions=outcome.external_versions,
            dependency_evidence_refs=dep_refs,
            verifier_id=verifier.verifier_id, verifier_version=verifier.version,
            relevant_task_spec_fields=spec_m.model_dump(mode="json"))
        return outcome, fingerprint

    def verify(self, ctx: RunContext, milestone_id: str) -> VerifierOutcome:
        spec_m = ctx.spec.milestone(milestone_id)
        self.journal.append(ctx, VerificationStarted(
            milestone_id=milestone_id, verifier_id=spec_m.verifier_id,
            verifier_version=spec_m.verifier_version), actor="verifier")
        try:
            outcome, fingerprint = self.evaluate(ctx, milestone_id)
        except Exception as exc:  # a verifier bug must never verify anything
            message = self._safe(str(exc))
            self.journal.append(ctx, VerifierError(milestone_id=milestone_id, message=message),
                                actor="verifier")
            return VerifierOutcome(VerifierResult.ERROR, message)
        # the message is persisted and shown to the model as feedback: redact and bound it
        outcome = dataclasses.replace(outcome, message=self._safe(outcome.message))
        if outcome.status == VerifierResult.ERROR:
            self.journal.append(ctx, VerifierError(milestone_id=milestone_id,
                                                   message=outcome.message), actor="verifier")
            return outcome
        evidence_ref = ctx.workspace.put_blob({
            "milestone_id": milestone_id, "verifier_id": spec_m.verifier_id,
            "verifier_version": spec_m.verifier_version, "status": outcome.status.value,
            "message": outcome.message, "evidence": self.redactor(outcome.evidence),
            "input_artifacts": outcome.input_artifacts,
            "external_versions": outcome.external_versions, "input_fingerprint": fingerprint})
        if outcome.status == VerifierResult.PASS:
            self.journal.append(ctx, MilestoneVerified(
                milestone_id=milestone_id, verifier_id=spec_m.verifier_id,
                verifier_version=spec_m.verifier_version, input_fingerprint=fingerprint,
                evidence_ref=evidence_ref), actor="verifier")
            self.faults.checkpoint("after_milestone_verified_commit", run_id=ctx.run_id,
                                   milestone_id=milestone_id)
        else:
            self.journal.append(ctx, MilestoneFailed(milestone_id=milestone_id,
                                                     evidence_ref=evidence_ref,
                                                     message=outcome.message),
                                actor="verifier")
        return outcome

    def verify_ready(self, ctx: RunContext) -> tuple[list[str], VerifierOutcome | None]:
        """Verify the focus milestone repeatedly until one fails or none remain."""
        verified: list[str] = []
        last: VerifierOutcome | None = None
        while True:
            focus = self.focus(ctx)
            if focus is None:
                return verified, last
            last = self.verify(ctx, focus)
            if last.status != VerifierResult.PASS:
                return verified, last
            verified.append(focus)

    # ---- drift ----------------------------------------------------------------------------

    def check_drift(self, ctx: RunContext) -> list[str]:
        """Record artifact drift, then re-verify every verified milestone in spec order."""
        with self.store.read() as q:
            artifacts = q.artifacts(ctx.run_id)
        latest = {art.relative_path: art for art in artifacts}
        drift_events: list[NewEvent] = []
        for art in latest.values():
            if art.state != ArtifactState.PRESENT:
                continue
            observed = ctx.workspace.hash(art.relative_path)
            if observed != art.sha256:
                drift_events.append(make_event(ArtifactDrift(
                    artifact_id=art.artifact_id,
                    state="missing" if observed is None else "drifted",
                    observed_sha256=observed), actor="runtime"))
        if drift_events:
            self.journal.append_events(ctx, drift_events)
        return self.recheck_verified(ctx)

    def recheck_verified(self, ctx: RunContext) -> list[str]:
        """Re-run verifiers for verified milestones; invalidate changed ones + descendants.

        Returns every milestone invalidated by this check (including descendants). Only a
        verifier FAIL or a changed fingerprint invalidates; a verifier ERROR raises
        :class:`VerificationUnavailableError` after recording ``VerifierError``.
        """
        invalidated: list[str] = []
        for m in ctx.spec.milestones:
            row = self.milestone(ctx, m.id)
            if row.status != MilestoneStatus.VERIFIED:
                continue
            try:
                outcome, fingerprint = self.evaluate(ctx, m.id)
            except Exception as exc:  # a verifier bug is not evidence of drift
                outcome, fingerprint = VerifierOutcome(VerifierResult.ERROR, str(exc)), ""
            if outcome.status == VerifierResult.ERROR:
                # A failed observation (timeout, unreachable service) proves nothing:
                # record it and stop without invalidating verified work (invariant 7).
                message = self._safe(outcome.message)
                self.journal.append(ctx, VerifierError(milestone_id=m.id, message=message),
                                    actor="verifier")
                raise VerificationUnavailableError(
                    f"could not re-verify {m.id}: {message}; resume again later")
            if outcome.status == VerifierResult.PASS and fingerprint == row.input_fingerprint:
                continue
            reason = ("verifier no longer passes" if outcome.status != VerifierResult.PASS
                      else "input/dependency fingerprint changed")
            invalidated.extend(self.invalidate(ctx, m.id, reason))
        return invalidated

    def invalidate(self, ctx: RunContext, milestone_id: str, reason: str) -> list[str]:
        events = [make_event(MilestoneInvalidated(milestone_id=milestone_id, reason=reason),
                             actor="verifier")]
        affected = [milestone_id]
        for dep in ctx.spec.descendants(milestone_id):
            if self.milestone(ctx, dep).status == MilestoneStatus.VERIFIED:
                events.append(make_event(MilestoneInvalidated(
                    milestone_id=dep, reason="upstream milestone invalidated",
                    cause_milestone_id=milestone_id), actor="verifier"))
                affected.append(dep)
        self.journal.append_events(ctx, events)
        return affected

    def drift_requires_remediation(self, ctx: RunContext, invalidated: list[str]) -> bool:
        """Stop explicitly when redo would need an already-confirmed operation again.

        Confirmed effects are never re-sent (their operation keys dedupe), so a
        drifted milestone whose writes are confirmed cannot be redone by the
        model (docs/03 s5): stop and require a new spec version or human
        remediation instead of looping until models are exhausted.
        """
        if not invalidated:
            return False
        with self.store.read() as q:
            confirmed = q.actions(ctx.run_id, [ActionStatus.CONFIRMED.value])
        blocked = sorted({a.milestone_id for a in confirmed
                          if a.milestone_id in invalidated and self.is_write(a.tool_name)})
        if not blocked:
            return False
        self.journal.append(ctx, RunFailed(
            failure_code="drift_requires_remediation",
            reason=f"milestones {blocked} drifted after their effects were confirmed; "
                   "redo needs a new task spec version or human remediation"))
        return True
