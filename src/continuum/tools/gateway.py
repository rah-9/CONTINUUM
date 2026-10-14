"""Permission + tool gateway: the single dispatch path (docs/02 section 4, docs/04 section 4).

Every model proposal and every trusted verifier/reconciler read goes through
:meth:`ToolGateway.propose`, is validated, ledgered and fenced. Calls are
serialized: the gateway dispatches one attempt at a time.

Algorithm (one fenced transaction before dispatch, one after):

1. validate tool/caller permission, JSON schema, path prefix; bind immutable args
2. TX: find-or-create the logical action by ``(run_id, operation_key)``;
   conflict / dedupe / block / approval as appropriate; authorize; persist
   ``ActionAttemptStarted``; commit
3. re-check lease ownership and cancellation immediately before invocation
4. invoke the trusted tool with persisted args and the original idempotency key
5. TX: persist confirmed / failed_clean / ambiguous outcome
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NoReturn

import jsonschema
from pydantic import BaseModel

from continuum.core.canonical import canonical_json, digest
from continuum.core.clock import Clock
from continuum.core.errors import PathContainmentError, StaleOwnerError
from continuum.core.events import (
    ActionAttemptAmbiguous,
    ActionAttemptConfirmed,
    ActionAttemptFailedClean,
    ActionAttemptStarted,
    ActionAuthorized,
    ActionProposalDeduped,
    ActionProposed,
    ActionRejected,
    ApprovalRequested,
    ArtifactRecorded,
    RunStatusChanged,
    make_event,
)
from continuum.core.evidence import Workspace, is_blob_ref
from continuum.core.faults import FaultInjector, NoFaults
from continuum.core.ids import IdGenerator
from continuum.core.leases import Lease, LeaseManager
from continuum.core.ledger import assert_transition, idempotency_key, operation_key
from continuum.core.redaction import Redactor
from continuum.core.state import (
    TERMINAL_RUN_STATUSES,
    UNRESOLVED_ACTION_STATUSES,
    ActionStatus,
    RunStatus,
    TaskSpec,
)
from continuum.core.store import ActionRow, RunTx, Store
from continuum.core.tracing import span
from continuum.tools.manifest import (
    BindContext,
    ToolAmbiguousFailure,
    ToolCleanFailure,
    ToolContext,
    ToolRegistry,
    ToolResult,
    TrustedTool,
)

Caller = Literal["model", "verifier", "reconciler", "runtime"]
OutcomeStatus = Literal[
    "ok",                 # dispatched and confirmed
    "deduped",            # existing confirmed action; recorded outcome returned
    "rejected",           # validation / permission / policy rejection, no dispatch
    "conflict",           # same operation identity with different immutable args
    "awaiting_approval",  # paused for a human approval decision
    "blocked",            # unresolved effect exists; no new write allowed
    "failed_clean",       # target rejected the call before any effect
    "unknown",            # ambiguous outcome; reconciliation required
    "lease_lost",         # ownership lost before dispatch or outcome commit
]

MAX_RESULT_CHARS = 4000


@dataclass(frozen=True)
class RunContext:
    run_id: str
    lease: Lease
    spec: TaskSpec
    workspace: Workspace


class GatewayOutcome(BaseModel):
    status: OutcomeStatus
    tool_name: str
    action_id: str | None = None
    operation_key: str | None = None
    result_ref: str | None = None
    data: Any = None
    code: str | None = None
    message: str = ""

    def for_model(self) -> dict[str, Any]:
        """Bounded result shown to a model, labelled as untrusted tool data."""
        body: dict[str, Any] = {"status": self.status, "tool": self.tool_name,
                                "untrusted_tool_data": True}
        if self.code:
            body["code"] = self.code
        if self.message:
            body["message"] = self.message
        if self.data is not None:
            text = canonical_json(self.data)
            if len(text) > MAX_RESULT_CHARS:
                body["data_truncated"] = True
                body["data_preview"] = text[:MAX_RESULT_CHARS]
                body["result_ref"] = self.result_ref
            else:
                body["data"] = self.data
        return body


class _Stop(Exception):
    def __init__(self, outcome: GatewayOutcome) -> None:
        self.outcome = outcome


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(_has_nul(v) for v in value)
    return False


class ToolGateway:
    def __init__(self, *, store: Store, leases: LeaseManager, registry: ToolRegistry,
                 clock: Clock, ids: IdGenerator, redactor: Redactor | None = None,
                 faults: FaultInjector | None = None) -> None:
        self.store = store
        self.leases = leases
        self.registry = registry
        self.clock = clock
        self.ids = ids
        self.redactor = redactor or Redactor()
        self.faults: FaultInjector = faults or NoFaults()

    # ---- public API ----------------------------------------------------------------

    def tool_schemas(self, spec: TaskSpec) -> list[dict[str, Any]]:
        """Provider tool schemas: derived only from the task allowlist."""
        schemas = []
        for name in spec.tools_allowlist:
            tool = self.registry.get(name)
            if tool is not None:
                schemas.append(tool.manifest.provider_schema())
        return schemas

    def propose(self, run: RunContext, tool_name: str, args: dict[str, Any], *,
                caller: Caller, milestone_id: str | None,
                provider_call_id: str | None = None) -> GatewayOutcome:
        actor = f"{caller}:{run.lease.owner_id}"
        tool = self.registry.get(tool_name)
        try:
            if tool is None:
                raise _Stop(self._reject(run, tool_name, "unknown_tool",
                                         f"tool {tool_name!r} is not registered", actor))
            bound = self._validate_and_bind(run, tool, args, caller, milestone_id, actor)
            action_id, attempt_no, idem_key = self._prepare_attempt(
                run, tool, bound, caller, milestone_id, provider_call_id, actor)
        except _Stop as stop:
            return stop.outcome
        except StaleOwnerError as exc:
            return GatewayOutcome(status="lease_lost", tool_name=tool_name, code="stale_owner",
                                  message=str(exc))
        return self._dispatch(run, tool, action_id, attempt_no, idem_key, bound, actor)

    def dispatch_authorized(self, run: RunContext, action_id: str, *, actor: str,
                            retry_reason: str | None = None) -> GatewayOutcome:
        """Start a new attempt for an existing action already in an eligible state.

        Used by the reconciler (``unknown -> in_flight`` idempotent same-key retry)
        and for human-authorized ``retry_at_risk`` actions.
        """
        with self.store.read() as q:
            action = q.action(action_id)
        tool = self.registry.get(action.tool_name)
        if tool is None:
            return GatewayOutcome(status="rejected", tool_name=action.tool_name,
                                  action_id=action_id, code="unknown_tool")
        try:
            with self.store.run_tx(run.run_id, run.lease.fence) as tx:
                action = tx.action(action_id)
                attempt_no = self._start_attempt(tx, run, action, actor, retry_reason)
        except StaleOwnerError as exc:
            return GatewayOutcome(status="lease_lost", tool_name=action.tool_name,
                                  code="stale_owner", message=str(exc))
        self.faults.checkpoint("after_attempt_start_commit", run_id=run.run_id,
                               action_id=action_id, tool=action.tool_name)
        return self._dispatch(run, tool, action_id, attempt_no, action.idempotency_key,
                              action.args_json, actor)

    # ---- validation ------------------------------------------------------------------

    def _validate_and_bind(self, run: RunContext, tool: TrustedTool, args: dict[str, Any],
                           caller: Caller, milestone_id: str | None, actor: str
                           ) -> dict[str, Any]:
        m = tool.manifest
        if caller == "model" and m.name not in run.spec.tools_allowlist:
            raise _Stop(self._reject(run, m.name, "not_allowlisted",
                                     f"tool {m.name} is not in the task allowlist", actor))
        if caller in ("verifier", "reconciler") and m.effect != "read":
            raise _Stop(self._reject(run, m.name, "read_only_caller",
                                     f"{caller} may only use read tools", actor))
        if not isinstance(args, dict):
            raise _Stop(self._reject(run, m.name, "invalid_args", "arguments must be an object",
                                     actor))
        if _has_nul(args):  # PostgreSQL text/JSONB cannot store U+0000
            raise _Stop(self._reject(run, m.name, "invalid_args",
                                     "arguments contain a NUL character", actor))
        try:
            m.validate_args(args)
        except jsonschema.ValidationError as exc:
            raise _Stop(self._reject(run, m.name, "invalid_args", exc.message, actor)) from exc
        prefix = m.permissions.allowed_path_prefix
        for f in m.permissions.path_fields:
            value = str(args.get(f, ""))
            if ".." in value.split("/") or value.startswith("/") or \
                    (prefix is not None and not value.startswith(prefix)):
                raise _Stop(self._reject(run, m.name, "path_not_permitted",
                                         f"{f}={value!r} outside {prefix!r}", actor))
        try:
            bound = tool.bind(args, BindContext(run.run_id, milestone_id, run.workspace))
        except ToolCleanFailure as exc:
            raise _Stop(self._reject(run, m.name, exc.code, str(exc), actor)) from exc
        except PathContainmentError as exc:  # e.g. a model-chosen `source` escaping
            raise _Stop(self._reject(run, m.name, "path_not_permitted", str(exc),
                                     actor)) from exc
        if self.redactor(bound) != bound:
            raise _Stop(self._reject(run, m.name, "secret_in_args",
                                     "arguments contain secret material", actor))
        return bound

    def _reject(self, run: RunContext, tool_name: str, code: str, message: str, actor: str,
                action: ActionRow | None = None, existing_action_id: str | None = None
                ) -> GatewayOutcome:
        # reasons can quote argument values (schema/path errors): redact before persisting
        message = self.redactor.text(message).replace("\x00", "�")
        with self.store.run_tx(run.run_id, run.lease.fence) as tx:
            tx.append(make_event(ActionRejected(
                action_id=action.action_id if action else None, tool_name=tool_name,
                reason=message, code=code, existing_action_id=existing_action_id), actor=actor))
        status: OutcomeStatus = "conflict" if code == "intent_conflict" else "rejected"
        return GatewayOutcome(status=status, tool_name=tool_name, code=code, message=message,
                              action_id=existing_action_id)

    # ---- transaction before dispatch ----------------------------------------------------

    def _prepare_attempt(self, run: RunContext, tool: TrustedTool, bound: dict[str, Any],
                         caller: Caller, milestone_id: str | None,
                         provider_call_id: str | None, actor: str
                         ) -> tuple[str, int, str | None]:
        m = tool.manifest
        args_sha = digest(bound)
        new_action_id = self.ids.new("act")
        if m.effect == "read":
            op_key = f"read:{new_action_id}"  # every fresh read is its own action
        else:
            try:
                op_key = operation_key(
                    tool_name=m.name, milestone_id=milestone_id,
                    milestone_scoped=m.operation_identity.milestone_scoped,
                    fields=list(m.operation_identity.fields), args=bound,
                    normalize=tool.normalize_identity)
            except ValueError as exc:
                raise _Stop(self._reject(run, m.name, "no_milestone", str(exc), actor)) from exc

        stop: GatewayOutcome | None = None
        with self.store.run_tx(run.run_id, run.lease.fence) as tx:
            run_row = tx.run(run.run_id)
            if run_row.status in TERMINAL_RUN_STATUSES:
                stop = GatewayOutcome(status="rejected", tool_name=m.name, code="run_terminal",
                                      message=f"run is {run_row.status}")
            existing = tx.action_by_key(run.run_id, op_key)
            if stop is None and existing is not None:
                stop = self._existing(tx, run, existing, args_sha, provider_call_id, actor)
                if stop is None:
                    action = existing
            if stop is None and m.effect == "write":
                # No write may start an attempt while any *other* effect is unresolved.
                blockers = [a for a in tx.actions(run.run_id, [s.value for s in
                                                               UNRESOLVED_ACTION_STATUSES])
                            if existing is None or a.action_id != existing.action_id]
                if blockers:
                    tx.append(make_event(ActionRejected(
                        action_id=None, tool_name=m.name, code="blocked_unresolved",
                        reason="unresolved external effects must be reconciled first",
                        existing_action_id=blockers[0].action_id), actor=actor))
                    stop = GatewayOutcome(
                        status="blocked", tool_name=m.name, code="blocked_unresolved",
                        action_id=blockers[0].action_id,
                        message="an earlier effect is unresolved; no new writes")
            if stop is None and existing is None:
                tx.append(make_event(ActionProposed(
                    action_id=new_action_id, milestone_id=milestone_id, operation_key=op_key,
                    tool_name=m.name, manifest_version=m.version, effect=m.effect,
                    caller=caller, args=bound, args_sha256=args_sha,
                    provider_call_id=provider_call_id), actor=actor))
                self.faults.checkpoint("after_action_created", run_id=run.run_id,
                                       action_id=new_action_id, tool=m.name)
                action = tx.action(new_action_id)
                if m.permissions.requires_approval and caller == "model":
                    tx.append(make_event(ApprovalRequested(action_id=new_action_id),
                                         actor=actor))
                    if run_row.status == RunStatus.RUNNING:
                        tx.append(make_event(RunStatusChanged(
                            from_status=run_row.status.value,
                            to_status=RunStatus.PAUSED_FOR_APPROVAL.value,
                            reason=f"approval required for {m.name}"), actor=actor))
                    stop = GatewayOutcome(status="awaiting_approval", tool_name=m.name,
                                          action_id=new_action_id, operation_key=op_key,
                                          message="a human must approve this action")
            if stop is None:
                attempt_no = self._start_attempt(tx, run, action, actor, None)
                idem = tx.action(action.action_id).idempotency_key
        if stop is not None:
            return self._raise(stop)
        self.faults.checkpoint("after_attempt_start_commit", run_id=run.run_id,
                               action_id=action.action_id, tool=m.name)
        return action.action_id, attempt_no, idem

    @staticmethod
    def _raise(outcome: GatewayOutcome) -> NoReturn:
        raise _Stop(outcome)

    def _existing(self, tx: RunTx, run: RunContext, existing: ActionRow, args_sha: str,
                  provider_call_id: str | None, actor: str) -> GatewayOutcome | None:
        """Decide what a repeated proposal of an existing operation does."""
        name = existing.tool_name
        if existing.args_sha256 != args_sha:
            tx.append(make_event(ActionRejected(
                action_id=None, tool_name=name, code="intent_conflict",
                reason="same operation identity proposed with different arguments",
                existing_action_id=existing.action_id), actor=actor))
            return GatewayOutcome(status="conflict", tool_name=name, code="intent_conflict",
                                  action_id=existing.action_id,
                                  operation_key=existing.operation_key,
                                  message="operation already exists with different arguments")
        status = existing.status
        if status in (ActionStatus.AUTHORIZED, ActionStatus.PROPOSED):
            return None
        if status == ActionStatus.FAILED_CLEAN:
            if len(tx.attempts(existing.action_id)) >= self._manifest_attempts(name):
                return GatewayOutcome(status="rejected", tool_name=name, code="max_attempts",
                                      action_id=existing.action_id,
                                      message="retry limit reached for this operation")
            return None
        tx.append(make_event(ActionProposalDeduped(
            action_id=existing.action_id, existing_status=status.value,
            provider_call_id=provider_call_id), actor=actor))
        base = {"tool_name": name, "action_id": existing.action_id,
                "operation_key": existing.operation_key}
        if status == ActionStatus.CONFIRMED:
            data = run.workspace.get_blob(existing.outcome_ref) \
                if is_blob_ref(existing.outcome_ref) else None
            return GatewayOutcome(status="deduped", result_ref=existing.outcome_ref,
                                  data=data, message="operation already confirmed; not re-sent",
                                  **base)
        if status in UNRESOLVED_ACTION_STATUSES:
            return GatewayOutcome(status="blocked", code="unresolved_action",
                                  message="operation outcome is unresolved", **base)
        if status == ActionStatus.AWAITING_APPROVAL:
            return GatewayOutcome(status="awaiting_approval", **base)
        return GatewayOutcome(status="rejected", code=status.value,
                              message=f"operation is {status.value}", **base)

    def _manifest_attempts(self, tool_name: str) -> int:
        tool = self.registry.get(tool_name)
        return tool.manifest.max_attempts if tool else 1

    def _start_attempt(self, tx: RunTx, run: RunContext, action: ActionRow, actor: str,
                       retry_reason: str | None) -> int:
        tool = self.registry.get(action.tool_name)
        assert tool is not None
        status = action.status
        if status in (ActionStatus.PROPOSED, ActionStatus.FAILED_CLEAN):
            assert_transition(status, ActionStatus.AUTHORIZED)
            key = action.idempotency_key
            if key is None and tool.manifest.idempotency.supported:
                key = idempotency_key(run.run_id, action.action_id)
            tx.append(make_event(ActionAuthorized(action_id=action.action_id,
                                                  idempotency_key=key), actor=actor))
            status = ActionStatus.AUTHORIZED
        assert_transition(status, ActionStatus.IN_FLIGHT)
        attempt_no = len(tx.attempts(action.action_id)) + 1
        tx.append(make_event(ActionAttemptStarted(
            action_id=action.action_id, attempt_no=attempt_no, owner_epoch=run.lease.epoch,
            retry_reason=retry_reason), actor=actor))
        return attempt_no

    # ---- dispatch and outcome -----------------------------------------------------------

    def _dispatch(self, run: RunContext, tool: TrustedTool, action_id: str, attempt_no: int,
                  idem_key: str | None, bound: dict[str, Any], actor: str) -> GatewayOutcome:
        m = tool.manifest
        base = {"tool_name": m.name, "action_id": action_id}
        # Immediately before invocation: ownership + cancellation. If lost, the
        # persisted started attempt is classified ambiguous by the next owner.
        with self.store.read() as q:
            run_status = q.run(run.run_id).status
        if not self.leases.is_active(run.lease) or run_status == RunStatus.CANCELLED:
            return GatewayOutcome(status="lease_lost", code="not_owner_or_cancelled",
                                  message="ownership lost before dispatch", **base)
        self.faults.checkpoint("before_dispatch", run_id=run.run_id, action_id=action_id,
                               tool=m.name)
        ctx = ToolContext(run_id=run.run_id, action_id=action_id, attempt_no=attempt_no,
                          milestone_id=None, idempotency_key=idem_key, workspace=run.workspace,
                          timeout_seconds=m.timeout_seconds)
        result: ToolResult | None = None
        failure: tuple[str, str, str] | None = None  # (kind, code, message)
        with span("tool_dispatch", run_id=run.run_id, action_id=action_id, tool=m.name,
                  attempt=attempt_no, effect=m.effect) as fields:
            try:
                result = tool.invoke(dict(bound), ctx)
            except ToolCleanFailure as exc:
                failure = ("clean", exc.code, self.redactor.text(str(exc)))
            except ToolAmbiguousFailure as exc:
                failure = ("ambiguous", exc.code, self.redactor.text(str(exc)))
            except Exception as exc:  # any unclassified failure is ambiguous
                failure = ("ambiguous", type(exc).__name__, self.redactor.text(str(exc)))
            fields["outcome"] = "ok" if failure is None else failure[0]
        if failure and failure[0] == "ambiguous" and m.effect == "read":
            failure = ("clean", failure[1], failure[2])  # reads cannot commit an effect
        if result is not None and result.artifacts:
            self.faults.checkpoint("after_artifact_write", run_id=run.run_id,
                                   action_id=action_id, tool=m.name)
        self.faults.checkpoint("after_response_receipt", run_id=run.run_id,
                               action_id=action_id, tool=m.name)
        try:
            outcome = self._record_outcome(run, action_id, attempt_no, result, failure, actor,
                                           base)
        except StaleOwnerError as exc:
            return GatewayOutcome(status="lease_lost", code="stale_owner", message=str(exc),
                                  **base)
        self.faults.checkpoint("after_action_outcome_commit", run_id=run.run_id,
                               action_id=action_id, tool=m.name, status=outcome.status)
        return outcome

    def _record_outcome(self, run: RunContext, action_id: str, attempt_no: int,
                        result: ToolResult | None, failure: tuple[str, str, str] | None,
                        actor: str, base: dict[str, Any]) -> GatewayOutcome:
        if failure is None:
            assert result is not None
            data = self.redactor(result.data)
            result_ref = run.workspace.put_blob(data)
            artifacts = [(p, run.workspace.hash(p)) for p in result.artifacts]
            with self.store.run_tx(run.run_id, run.lease.fence) as tx:
                for path, sha in artifacts:
                    if sha is None:
                        continue
                    tx.append(make_event(ArtifactRecorded(
                        artifact_id=self.ids.new("art"), relative_path=path, sha256=sha,
                        byte_size=run.workspace.resolve(path).stat().st_size,
                        producing_action_id=action_id), actor=actor))
                tx.append(make_event(ActionAttemptConfirmed(
                    action_id=action_id, attempt_no=attempt_no, result_ref=result_ref),
                    actor=actor))
                op_key = tx.action(action_id).operation_key
            return GatewayOutcome(status="ok", result_ref=result_ref, data=data,
                                  operation_key=op_key, **base)
        kind, code, message = failure
        with self.store.run_tx(run.run_id, run.lease.fence) as tx:
            if kind == "clean":
                tx.append(make_event(ActionAttemptFailedClean(
                    action_id=action_id, attempt_no=attempt_no, error_code=code,
                    message=message), actor=actor))
            else:
                tx.append(make_event(ActionAttemptAmbiguous(
                    action_id=action_id, attempt_no=attempt_no, error_code=code,
                    message=message), actor=actor))
            op_key = tx.action(action_id).operation_key
        status: OutcomeStatus = "failed_clean" if kind == "clean" else "unknown"
        return GatewayOutcome(status=status, code=code, message=message, operation_key=op_key,
                              **base)
