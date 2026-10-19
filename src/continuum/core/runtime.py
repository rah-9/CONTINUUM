# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Run controller: start, resume, step loop and human decisions (docs/02).

The controller owns run lifecycle and the step loop. It composes focused
components, each with one responsibility:

* :class:`~continuum.tools.gateway.ToolGateway` — the only tool dispatch path
* :class:`~continuum.core.reconciliation.Reconciler` — unknown-effect resolution
* :class:`~continuum.core.verification.MilestoneVerification` — the only source
  of milestone progress, drift and invalidation
* :class:`~continuum.core.sessions.ModelSessions` — model sessions and handoff
* :class:`~continuum.core.budgets.BudgetGuard` — aggregate hard budgets
* :class:`~continuum.core.journal.RunJournal` — fenced event appends

No database transaction is held across a model or tool call.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from continuum.core.budgets import BudgetGuard
from continuum.core.canonical import canonical_json
from continuum.core.clock import Clock
from continuum.core.errors import (
    AlreadyOwnedError,
    ContinuumError,
    InvalidTransitionError,
    NotFoundError,
    SpecValidationError,
    StaleOwnerError,
    VerificationUnavailableError,
)
from continuum.core.events import (
    ActionAuthorized,
    ActionNeedsHuman,
    ActionReconciled,
    ActionRejected,
    ActionSkipped,
    ApprovalDecided,
    FaultInjected,
    ModelSessionEnded,
    NewEvent,
    RecoveryStarted,
    RunCancelled,
    RunCompleted,
    RunCreated,
    RunFailed,
    RunStarted,
    RunStatusChanged,
    make_event,
)
from continuum.core.evidence import Workspace
from continuum.core.faults import FaultInjector, NoFaults
from continuum.core.handoff import PacketBuilder
from continuum.core.ids import IdGenerator
from continuum.core.journal import RunJournal, RunStopped
from continuum.core.leases import Lease, LeaseKeeper, LeaseManager
from continuum.core.ledger import idempotency_key, mark_interrupted_attempts
from continuum.core.reconciliation import Reconciler
from continuum.core.redaction import Redactor
from continuum.core.replay import export_trace, timeline
from continuum.core.sessions import ModelSessions, Session, SessionSettings
from continuum.core.state import (
    TERMINAL_RUN_STATUSES,
    UNRESOLVED_ACTION_STATUSES,
    ActionStatus,
    MilestoneStatus,
    RunStatus,
    TaskSpec,
)
from continuum.core.store import Store
from continuum.core.verification import MilestoneVerification
from continuum.models.base import Message, ModelResponse, ModelRouter
from continuum.tools.gateway import GatewayOutcome, RunContext, ToolGateway
from continuum.tools.manifest import ToolRegistry
from continuum.verify.registry import VerifierRegistry


@dataclass(frozen=True)
class RuntimeConfig:
    workspace_root: Path
    input_root: Path
    owner_id: str = field(default_factory=lambda: f"ctl-{socket.gethostname()}")
    lease_ttl_seconds: int = 30
    packet_context_fraction: float = 0.25
    max_output_tokens: int = 2048
    model_timeout_seconds: float = 60.0
    invalid_output_handoff_threshold: int = 2
    stalled_turns_handoff_threshold: int = 3
    active_time_increment_cap_seconds: float = 60.0
    data_refs_limit: int = 5
    # Background lease renewal during long model/tool calls. None disables it
    # (deterministic tests drive lease time with a fake clock instead).
    heartbeat_interval_seconds: float | None = None


# Runs claimed by an operation in this process: (dsn, schema, owner_id, run_id).
# See Runtime._owned.
_CLAIMS: set[tuple[str, str, str, str]] = set()
_CLAIMS_LOCK = threading.Lock()


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    failure_code: str | None = None
    message: str = ""
    needs_human: list[str] = field(default_factory=list)
    awaiting_approval: list[str] = field(default_factory=list)


Decision = Literal["confirmed", "abort", "retry_at_risk", "approve", "reject"]


class Runtime:
    def __init__(self, *, store: Store, tools: ToolRegistry, verifiers: VerifierRegistry,
                 models: ModelRouter, config: RuntimeConfig, clock: Clock, ids: IdGenerator,
                 faults: FaultInjector | None = None, redactor: Redactor | None = None) -> None:
        self.store = store
        self.tools = tools
        self.verifiers = verifiers
        self.models = models
        self.config = config
        self.clock = clock
        self.ids = ids
        self.faults: FaultInjector = faults or NoFaults()
        self.redactor = redactor or Redactor.from_environment()
        self.leases = LeaseManager(store, ttl_seconds=config.lease_ttl_seconds)
        self.journal = RunJournal(store)
        self.gateway = ToolGateway(store=store, leases=self.leases, registry=tools, clock=clock,
                                   ids=ids, redactor=self.redactor, faults=self.faults)
        self.reconciler = Reconciler(store=store, gateway=self.gateway, registry=tools,
                                     clock=clock)
        self.budgets = BudgetGuard(store=store, journal=self.journal, clock=clock,
                                   increment_cap_seconds=config.active_time_increment_cap_seconds)
        self.verification = MilestoneVerification(
            store=store, verifiers=verifiers, gateway=self.gateway, journal=self.journal,
            redactor=self.redactor, faults=self.faults, is_write=self._is_write)
        self.sessions = ModelSessions(
            store=store, models=models, gateway=self.gateway, journal=self.journal,
            packets=PacketBuilder(context_fraction=config.packet_context_fraction), ids=ids,
            clock=clock, redactor=self.redactor, faults=self.faults,
            settings=SessionSettings(
                max_output_tokens=config.max_output_tokens,
                model_timeout_seconds=config.model_timeout_seconds,
                invalid_output_handoff_threshold=config.invalid_output_handoff_threshold,
                data_refs_limit=config.data_refs_limit),
            budget_exhausted=self.budgets.exhausted)

    # ===== public operations ============================================================

    def validate_spec(self, spec: TaskSpec) -> None:
        """Validate a spec against registered tools, verifiers and model routes."""
        self.tools.validate_references()
        for name in spec.tools_allowlist:
            if self.tools.get(name) is None:
                raise SpecValidationError(f"allowlisted tool {name} is not registered")
        self.verifiers.validate_spec(spec)
        for alias in (spec.model_policy.primary, *spec.model_policy.fallbacks):
            self.models.get(alias)

    def start(self, spec: TaskSpec, *, run_id: str | None = None,
              model_alias: str | None = None) -> RunResult:
        self.validate_spec(spec)
        if model_alias is not None:
            self.models.get(model_alias)  # unknown alias: refuse before any run exists
        self.budgets.start_clock()
        run_id = run_id or self.ids.new("run")
        with self.store.read() as q:
            if q.run_exists(run_id):
                raise SpecValidationError(f"run {run_id} already exists; use resume",
                                          code="run_exists")
        workspace = Workspace(self.config.workspace_root, run_id)
        workspace.init()
        workspace.snapshot_inputs(spec, self.config.input_root)
        with self.store.run_tx(run_id, None, create=True) as tx:
            tx.append(make_event(RunCreated(task_id=spec.task_id, spec=spec.canonical(),
                                            owner_id=self.config.owner_id), actor="runtime"))
        with self._owned(run_id) as lease, self._keeper(lease):
            ctx = RunContext(run_id, lease, spec, workspace)
            self.journal.append(ctx, RunStarted(owner_id=lease.owner_id, epoch=lease.epoch))
            return self._drive(ctx, model_alias=model_alias, resumed=False)

    def resume(self, run_id: str, *, model_alias: str | None = None) -> RunResult:
        """Real continuation under lease and ledger safeguards (never live replay)."""
        if model_alias is not None:
            self.models.get(model_alias)  # unknown alias: refuse before taking the lease
        self.budgets.start_clock()  # reconciliation and re-verification are active time
        with self._owned(run_id) as lease, self._keeper(lease):
            return self._resume_owned(run_id, lease, model_alias)

    def _resume_owned(self, run_id: str, lease: Lease, model_alias: str | None) -> RunResult:
        with self.store.read() as q:
            run = q.run(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return self._result(run_id)
        ctx = RunContext(run_id, lease, run.spec, Workspace(self.config.workspace_root, run_id))
        try:
            return self._recover_and_drive(ctx, model_alias)
        finally:
            self._record_time(ctx)

    def _record_time(self, ctx: RunContext) -> None:
        with contextlib.suppress(ContinuumError):  # e.g. the lease was lost meanwhile
            self.budgets.record_active_time(ctx)

    def _recover_and_drive(self, ctx: RunContext, model_alias: str | None) -> RunResult:
        run_id = ctx.run_id
        lease = ctx.lease
        self.validate_spec(ctx.spec)
        if self.store.projection_diff(run_id):
            self.store.rebuild_projection(run_id, lease.fence)
        self._begin_recovery(ctx)
        if self.budgets.exhausted(ctx, include_recoveries=True):
            return self._result(run_id)
        try:
            drifted = self.verification.check_drift(ctx)
        except VerificationUnavailableError as exc:
            self.journal.set_status(ctx, RunStatus.AWAITING_RECONCILIATION, str(exc)[:500])
            return self._result(run_id)
        if self.verification.drift_requires_remediation(ctx, drifted):
            return self._result(run_id)
        if not self._settle_effects(ctx):
            return self._result(run_id)
        if self._pending_approvals(ctx):
            self.journal.set_status(ctx, RunStatus.PAUSED_FOR_APPROVAL, "awaiting approval")
            return self._result(run_id)
        self.journal.set_status(ctx, RunStatus.RUNNING, "recovered")
        return self._drive(ctx, model_alias=model_alias, resumed=True)

    def resolve_action(self, run_id: str, action_id: str, decision: Decision, *,
                       actor: str, reason: str, evidence_ref: str | None = None) -> RunResult:
        """Record an explicit human decision (docs/04 section 8). Never implied by model text."""
        if not actor:
            raise SpecValidationError("a human actor id is required")
        with self._owned(run_id) as lease:
            with self.store.read() as q:
                run = q.run(run_id)
                action = q.action(action_id)
            if action.run_id != run_id:
                raise NotFoundError(f"action {action_id} is not part of run {run_id}")
            ctx = RunContext(run_id, lease, run.spec,
                             Workspace(self.config.workspace_root, run_id))
            who = f"human:{actor}"
            events: list[NewEvent] = []
            status = action.status
            if decision in ("approve", "reject"):
                if status != ActionStatus.AWAITING_APPROVAL:
                    raise InvalidTransitionError(f"action is {status}, not awaiting approval")
                events.append(make_event(ApprovalDecided(
                    action_id=action_id,
                    decision="approved" if decision == "approve" else "rejected",
                    actor_id=actor, reason=reason), actor=who))
                if decision == "approve":
                    events.append(make_event(ActionAuthorized(
                        action_id=action_id, idempotency_key=self._idem_for(action_id, run_id),
                        actor=who, reason=reason), actor=who))
                else:
                    events.append(make_event(ActionRejected(
                        action_id=action_id, tool_name=action.tool_name, code="approval_denied",
                        reason=reason), actor=who))
            else:
                if status not in (ActionStatus.UNKNOWN, ActionStatus.NEEDS_HUMAN):
                    raise InvalidTransitionError(f"action is {status}; nothing to resolve")
                if status == ActionStatus.UNKNOWN and decision != "confirmed":
                    events.append(make_event(ActionNeedsHuman(
                        action_id=action_id, reason=f"human decision: {decision}"), actor=who))
                if decision == "confirmed":
                    if not evidence_ref:
                        raise SpecValidationError("confirmed requires an external receipt")
                    if evidence_ref.startswith("blob:") and not ctx.workspace.has_blob(
                            evidence_ref):
                        raise SpecValidationError(
                            f"evidence {evidence_ref[:80]!r} is not a blob of this run; give "
                            "an external receipt id (e.g. receipt:123) or an existing blob")
                    events.append(make_event(ActionReconciled(
                        action_id=action_id, to_status="confirmed", method="human_receipt",
                        evidence_ref=evidence_ref, outcome_ref=evidence_ref,
                        provenance="human_assisted", actor=who), actor=who))
                elif decision == "abort":
                    events.append(make_event(ActionSkipped(action_id=action_id, actor=actor,
                                                           reason=reason), actor=who))
                    if run.status not in TERMINAL_RUN_STATUSES:  # never rewrite how it ended
                        events.append(make_event(RunFailed(
                            failure_code="human_abort",
                            reason=f"required action {action_id} aborted by {actor}: {reason}"),
                            actor=who))
                elif run.status in TERMINAL_RUN_STATUSES and decision == "retry_at_risk":
                    raise InvalidTransitionError(
                        f"run {run_id} is {run.status}; nothing will retry this action")
                elif decision == "retry_at_risk":
                    events.append(make_event(ActionAuthorized(
                        action_id=action_id, idempotency_key=action.idempotency_key,
                        at_risk_override=True, actor=who,
                        reason=f"AT-RISK RETRY (may duplicate): {reason}"), actor=who))
                else:
                    raise SpecValidationError(f"unknown decision {decision}")
            with self.store.run_tx(ctx.run_id, ctx.lease.fence) as tx:
                tx.append_all(events)
            return self._result(run_id)

    def cancel(self, run_id: str, *, actor: str, reason: str) -> RunResult:
        """Prevent new dispatches. Cannot roll back calls already sent."""
        with self._owned(run_id) as lease:
            with self.store.read() as q:
                run = q.run(run_id)
            if run.status not in TERMINAL_RUN_STATUSES:
                with self.store.run_tx(run_id, lease.fence) as tx:
                    tx.append(make_event(RunCancelled(actor=actor, reason=reason),
                                         actor=f"human:{actor}"))
            return self._result(run_id)

    def record_fault(self, run_id: str, boundary: str, detail: dict[str, Any]) -> None:
        """Test-only: record that a fault was injected (Continuum Lab)."""
        with self._owned(run_id) as lease, self.store.run_tx(run_id, lease.fence) as tx:
            tx.append(make_event(FaultInjected(boundary=boundary, detail=detail),
                                 actor="lab"))

    def status(self, run_id: str) -> dict[str, Any]:
        with self.store.read() as q:
            run = q.run(run_id)
            milestones = q.milestones(run_id)
            actions = q.actions(run_id)
            usage = q.usage(run_id)
            lease = q.lease(run_id)
        return {
            "run_id": run_id, "task_id": run.task_id, "status": run.status.value,
            "failure_code": run.failure_code, "last_event_seq": run.last_event_seq,
            "milestones": [{"id": m.milestone_id, "status": m.status.value,
                            "evidence_ref": m.evidence_ref} for m in milestones],
            "actions": [{"action_id": a.action_id, "operation_key": a.operation_key,
                         "tool": a.tool_name, "status": a.status.value} for a in actions
                        if not a.operation_key.startswith("read:")],
            "needs_human": [a.action_id for a in actions
                            if a.status in (ActionStatus.NEEDS_HUMAN, ActionStatus.UNKNOWN)],
            "awaiting_approval": [a.action_id for a in actions
                                  if a.status == ActionStatus.AWAITING_APPROVAL],
            "usage": {"model_calls": usage.model_calls, "tokens_reported": usage.tokens_reported,
                      "cost_known_usd": str(usage.cost_known_usd),
                      "has_unpriced_usage": usage.has_unpriced_usage,
                      "recoveries": usage.recoveries,
                      "active_elapsed_ms": usage.active_elapsed_ms},
            "lease": None if lease is None else {"owner_id": lease.owner_id,
                                                 "epoch": lease.epoch},
        }

    def inspect(self, run_id: str) -> dict[str, Any]:
        """Side-effect-free diagnostic replay: reads events only; invokes no tool/model."""
        with self.store.read() as q:
            events = q.events(run_id)
        replayed = self.store.replay_projection(run_id)
        workspace = Workspace(self.config.workspace_root, run_id)
        return {
            "run_id": run_id,
            "events": [{"seq": e.seq, "type": e.type.value, "actor": e.actor,
                        "occurred_at": e.occurred_at.isoformat()} for e in events],
            "timeline": timeline(events, workspace),
            "replayed_status": next(iter(replayed.tables["runs"].values()))["status"],
            "projection_diff": self.store.projection_diff(run_id),
        }

    def trace_export(self, run_id: str) -> list[dict[str, Any]]:
        """Full JSON trace (events + redacted blobs) for Continuum Lab. Read-only."""
        with self.store.read() as q:
            events = q.events(run_id)
        return list(export_trace(events, Workspace(self.config.workspace_root, run_id)))

    @contextlib.contextmanager
    def _owned(self, run_id: str) -> Iterator[Lease]:
        """Lease the run for one operation. The run is also claimed locally per controller
        id: the lease is re-entrant for the same id, so two concurrent operations under
        one id (e.g. two HTTP requests in one server) would otherwise share one lease
        and epoch, and one's release would expire the other's lease."""
        key = (self.store.dsn, self.store.schema, self.config.owner_id, run_id)
        with _CLAIMS_LOCK:
            if key in _CLAIMS:
                raise AlreadyOwnedError(f"run {run_id} is busy in this process; retry later")
            _CLAIMS.add(key)
        try:
            lease = self.leases.acquire(run_id, self.config.owner_id)
            try:
                yield lease
            finally:
                self.leases.release(lease)
        finally:
            with _CLAIMS_LOCK:
                _CLAIMS.discard(key)

    @contextlib.contextmanager
    def _keeper(self, lease: Lease) -> Iterator[None]:
        interval = self.config.heartbeat_interval_seconds
        if interval is None:
            yield
            return
        with LeaseKeeper(self.leases, lease, interval):
            yield

    def pending_decisions(self, run_id: str) -> list[dict[str, Any]]:
        """Read-only list of decisions only a human may make, with how to make them."""
        with self.store.read() as q:
            actions = q.actions(run_id, [ActionStatus.NEEDS_HUMAN.value,
                                         ActionStatus.UNKNOWN.value,
                                         ActionStatus.AWAITING_APPROVAL.value])
        out = []
        for a in actions:
            approval = a.status == ActionStatus.AWAITING_APPROVAL
            out.append({
                "action_id": a.action_id, "tool": a.tool_name, "status": a.status.value,
                "operation_key": a.operation_key,
                "decisions": ["approve", "reject"] if approval
                else ["confirmed", "abort", "retry_at_risk"],
                "how": f"continuum resolve {run_id} {a.action_id} <decision> "
                       "--actor <you> --reason <why>"
                       + ("" if approval else " [--evidence <receipt> for confirmed]"),
            })
        return out

    # ===== recovery ======================================================================

    def _begin_recovery(self, ctx: RunContext) -> None:
        actor = f"runtime:{ctx.lease.owner_id}"
        with self.store.run_tx(ctx.run_id, ctx.lease.fence) as tx:
            run = tx.run(ctx.run_id)
            ambiguous = mark_interrupted_attempts(tx, actor=actor)
            tx.append(make_event(RecoveryStarted(owner_id=ctx.lease.owner_id,
                                                 epoch=ctx.lease.epoch,
                                                 ambiguous_attempts=ambiguous), actor=actor))
            if run.active_session_id:
                tx.append(make_event(ModelSessionEnded(session_id=run.active_session_id,
                                                       finish_reason="interrupted"),
                                     actor=actor))
            if run.status == RunStatus.CREATED:
                tx.append(make_event(RunStarted(owner_id=ctx.lease.owner_id,
                                                epoch=ctx.lease.epoch), actor=actor))
            if tx.run(ctx.run_id).status != RunStatus.RECOVERING:
                tx.append(make_event(RunStatusChanged(
                    from_status=tx.run(ctx.run_id).status.value,
                    to_status=RunStatus.RECOVERING.value, reason="resume"), actor=actor))

    def _settle_effects(self, ctx: RunContext) -> bool:
        """Process human decisions, then reconcile. True if writes may proceed."""
        actor = f"runtime:{ctx.lease.owner_id}"
        with self.store.read() as q:
            authorized = q.actions(ctx.run_id, [ActionStatus.AUTHORIZED.value])
            retry = []
            for a in authorized:
                approval = q.approval(a.action_id)
                approved = approval is not None and approval.decision == "approved"
                if self._is_write(a.tool_name) and (q.attempts(a.action_id) or approved):
                    retry.append(a)
        for action in retry:  # human-approved or explicitly authorized retries first
            outcome = self.gateway.dispatch_authorized(ctx, action.action_id, actor=actor,
                                                       retry_reason="authorized_retry")
            if outcome.status == "lease_lost":
                return False
        report = self.reconciler.reconcile(ctx)
        if not report.resolved:
            self.journal.set_status(ctx, RunStatus.AWAITING_RECONCILIATION,
                                    "unresolved external effects require reconciliation")
            return False
        with self.store.read() as q:
            if q.actions(ctx.run_id, [s.value for s in UNRESOLVED_ACTION_STATUSES]):
                self.journal.set_status(ctx, RunStatus.AWAITING_RECONCILIATION,
                                        "unresolved effects")
                return False
        return True

    def _pending_approvals(self, ctx: RunContext) -> bool:
        with self.store.read() as q:
            return bool(q.actions(ctx.run_id, [ActionStatus.AWAITING_APPROVAL.value]))

    def _is_write(self, tool_name: str) -> bool:
        tool = self.tools.get(tool_name)
        return tool is not None and tool.manifest.effect == "write"

    def _idem_for(self, action_id: str, run_id: str) -> str | None:
        with self.store.read() as q:
            action = q.action(action_id)
        tool = self.tools.get(action.tool_name)
        if tool is None or not tool.manifest.idempotency.supported:
            return None
        return action.idempotency_key or idempotency_key(run_id, action_id)

    # ===== step loop =======================================================================

    def _drive(self, ctx: RunContext, *, model_alias: str | None, resumed: bool) -> RunResult:
        self.budgets.ensure_clock()
        session: Session | None = None
        tried: list[str] = []
        try:
            self.verification.verify_ready(ctx)  # work may already be done
            while True:
                ctx = self._heartbeat(ctx)
                self.budgets.record_active_time(ctx)
                if self._stopped(ctx) or self.budgets.exhausted(ctx):
                    break
                focus = self.verification.focus(ctx)
                if focus is None:
                    if self._finalize(ctx):
                        break
                    continue
                if session is None:
                    session = self.sessions.open(
                        ctx, model_alias or ctx.spec.model_policy.primary, tried,
                        reason="resume" if resumed else "start", previous=None)
                    if session is None:
                        break
                response = self.sessions.call(ctx, session)
                self._record_time(ctx)  # a call can take minutes: count it before the cap
                if response is None:  # the model failed: hand off to the next compatible one
                    session = self.sessions.handoff(ctx, session, tried,
                                                    reason=self.sessions.last_error)
                    if session is None:
                        break
                    continue
                if response.message.tool_proposals:
                    session.stalled_turns = 0
                    proceed = self._run_proposals(ctx, session, response, focus)
                    self._record_time(ctx)
                    if not proceed:
                        break
                    newly, _ = self.verification.verify_ready(ctx)
                    if newly:
                        session.messages.append(Message(role="user", content=(
                            f"Runtime: verified {newly}. Continue with the next pending "
                            "milestone, if any.")))
                    continue
                newly, outcome = self.verification.verify_ready(ctx)
                if newly:
                    session.stalled_turns = 0
                    session.messages.append(Message(role="user", content=(
                        f"Runtime: verified {newly}. Continue with the next pending milestone.")))
                    continue
                session.stalled_turns += 1
                feedback = outcome.message if outcome else "milestone not yet verifiable"
                session.messages.append(Message(role="user", content=(
                    f"Runtime verifier: milestone {focus} is not verified: {feedback}. "
                    "Use the tools to complete it.")))
                if session.stalled_turns >= self.config.stalled_turns_handoff_threshold:
                    session = self.sessions.handoff(ctx, session, tried, reason="no_progress")
                    if session is None:
                        break
        except RunStopped:
            pass
        except StaleOwnerError:
            return self._result(ctx.run_id, message="lease lost; another controller owns run")
        finally:
            self._record_time(ctx)
            if session is not None:
                self.sessions.end(ctx, session, "stopped")
        return self._result(ctx.run_id)

    def _heartbeat(self, ctx: RunContext) -> RunContext:
        lease: Lease = self.leases.renew(ctx.lease)
        return RunContext(ctx.run_id, lease, ctx.spec, ctx.workspace)

    def _stopped(self, ctx: RunContext) -> bool:
        status = self.journal.status(ctx.run_id)
        return status in TERMINAL_RUN_STATUSES or status in (
            RunStatus.AWAITING_RECONCILIATION, RunStatus.PAUSED_FOR_APPROVAL)

    def _finalize(self, ctx: RunContext) -> bool:
        """Complete only if all milestones still verify and nothing is unresolved."""
        try:
            invalidated = self.verification.recheck_verified(ctx)
        except VerificationUnavailableError as exc:
            self.journal.set_status(ctx, RunStatus.AWAITING_RECONCILIATION, str(exc)[:500])
            return True
        if invalidated:
            return self.verification.drift_requires_remediation(ctx, invalidated)
        with self.store.read() as q:
            if q.actions(ctx.run_id, [s.value for s in UNRESOLVED_ACTION_STATUSES]):
                self.journal.set_status(ctx, RunStatus.AWAITING_RECONCILIATION,
                                        "unresolved effects")
                return True
            statuses = q.milestone_statuses(ctx.run_id)
        if any(s != MilestoneStatus.VERIFIED for s in statuses.values()):
            return False
        self.journal.append(ctx, RunCompleted(verified_milestones=sorted(statuses)))
        return True

    def _run_proposals(self, ctx: RunContext, session: Session, response: ModelResponse,
                       focus: str) -> bool:
        """Dispatch proposals serially. False if the run must stop (pause/unknown/lease)."""
        for proposal in response.message.tool_proposals:
            outcome: GatewayOutcome = self.gateway.propose(
                ctx, proposal.tool_name, proposal.arguments, caller="model", milestone_id=focus,
                provider_call_id=proposal.provider_call_id)
            session.messages.append(Message(
                role="tool", reply_to_call_id=proposal.provider_call_id,
                content=canonical_json(outcome.for_model())))
            if outcome.status == "lease_lost":
                raise StaleOwnerError(outcome.message)
            if outcome.status == "awaiting_approval":
                return False
            if outcome.status in ("unknown", "blocked") and not self._settle_effects(ctx):
                return False
        return True

    def _result(self, run_id: str, message: str = "") -> RunResult:
        with self.store.read() as q:
            run = q.run(run_id)
            actions = q.actions(run_id)
        return RunResult(
            run_id=run_id, status=run.status, failure_code=run.failure_code, message=message,
            needs_human=[a.action_id for a in actions
                         if a.status in (ActionStatus.NEEDS_HUMAN, ActionStatus.UNKNOWN)],
            awaiting_approval=[a.action_id for a in actions
                               if a.status == ActionStatus.AWAITING_APPROVAL])
