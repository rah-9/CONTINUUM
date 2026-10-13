"""Continuum Lab runner: paired B0/C1 experiments over tasks x faults x modes x seeds.

Usage::

    continuum-lab run --suite smoke   --out results/smoke
    continuum-lab run --suite safety  --out results/safety --seeds 2

Each cell gets a fresh PostgreSQL schema, workspace and mock services. Faults
are deterministic, by named boundary. Scoring uses mock-service ground truth
plus the task's verifiers run independently at the end (never agent prose).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from continuum.core.canonical import canonical_json
from continuum.core.clock import FakeClock
from continuum.core.evidence import Workspace
from continuum.core.faults import FaultInjector, SimulatedCrash
from continuum.core.ids import SequentialIds
from continuum.core.runtime import Runtime, RuntimeConfig
from continuum.core.state import (
    TERMINAL_RUN_STATUSES,
    ActionStatus,
    RunStatus,
    VerifierResult,
)
from continuum.core.store import Store
from continuum.models.base import ModelError
from continuum.models.fake import FakeModelAdapter
from continuum.tools.manifest import BindContext, ToolContext, ToolRegistry, ToolResult, TrustedTool
from continuum.verify.registry import VerifierContext
from continuum_lab.baseline import BaselineRunner
from continuum_lab.fault_injection import CrashInjector, CrashRule, for_milestone
from continuum_lab.harness import ModelFaults, fake_router, make_env
from continuum_lab.metrics import Row
from continuum_lab.report import render
from continuum_lab.tasks.catalog import CATALOG, get_task
from continuum_lab.tasks.definition import ServiceEnv, TaskDefinition, score_ground_truth
from continuum_lab.tasks.policy import PlanPolicy

REMOTE_WRITES = frozenset({"store.put", "db.update", "message.send"})
FAULTS = ["none", "commit_then_drop", "commit_late", "kill_after_remote_commit",
          "kill_at_progress", "provider_unavailable"]
REMOTE_FAULTS = frozenset({"commit_then_drop", "commit_late", "kill_after_remote_commit"})
MODES = ["deduped", "observable", "opaque"]
DEFAULT_DSN = "postgresql://continuum:continuum@localhost:5432/continuum"


@dataclass(frozen=True)
class Cell:
    config: str  # "B0" | "C1"
    task: str
    fault: str
    mode: str
    seed: int


class _Recorder:
    """Records successful write dispatches as pre- or post-fault (preserved progress)."""

    def __init__(self) -> None:
        self.phase = "pre"
        self.log: list[tuple[str, str]] = []

    def wrap(self, registry: ToolRegistry) -> ToolRegistry:
        wrapped: list[TrustedTool] = []
        for name in registry.names():
            tool = registry.get(name)
            assert tool is not None
            wrapped.append(_RecordingTool(tool, self) if tool.manifest.effect == "write"
                           else tool)
        return ToolRegistry(wrapped)

    def preserved(self) -> tuple[int, int]:
        pre = {k for p, k in self.log if p == "pre"}
        post = {k for p, k in self.log if p == "post"}
        return len(pre - post), len(pre)


class _RecordingTool(TrustedTool):
    def __init__(self, inner: TrustedTool, recorder: _Recorder) -> None:
        self.inner = inner
        self.recorder = recorder
        self.manifest = inner.manifest

    def bind(self, args: dict[str, Any], ctx: BindContext) -> dict[str, Any]:
        return self.inner.bind(args, ctx)

    def normalize_identity(self, field_name: str, value: Any) -> str:
        return self.inner.normalize_identity(field_name, value)

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = self.inner.invoke(args, ctx)
        ident = "|".join(str(args.get(f)) for f in self.manifest.operation_identity.fields)
        self.recorder.log.append((self.recorder.phase, f"{self.manifest.name}:{ident}"))
        return result


def relevant_cells(tasks: list[str], faults: list[str], modes: list[str], seeds: int,
                   configs: list[str]) -> list[Cell]:
    cells: list[Cell] = []
    for task_id in tasks:
        d = get_task(task_id)
        remote = any(s.tool in REMOTE_WRITES for s in d.plan)
        forced = d.env_overrides.get("msg_mode") or d.env_overrides.get("store_mode")
        task_modes = [forced] if forced else (modes if remote else ["deduped"])
        for fault in faults:
            if fault in REMOTE_FAULTS and not remote:
                continue
            for mode in task_modes:
                for seed in range(1, seeds + 1):
                    cells.extend(Cell(c, task_id, fault, mode, seed) for c in configs)
    return cells


def _env(definition: TaskDefinition, path: Path, mode: str, clock: FakeClock) -> ServiceEnv:
    overrides: dict[str, Any] = {"now_fn": lambda: clock.now().timestamp()}
    for key in ("store_mode", "db_mode", "msg_mode"):
        if key not in definition.env_overrides:
            overrides[key] = mode
    return make_env(definition, path, **overrides)


def _apply_service_fault(env: ServiceEnv, fault: str) -> None:
    if fault in ("commit_then_drop", "commit_late"):
        env.store.script("put", fault)  # type: ignore[arg-type]
        env.db.script("update", fault)  # type: ignore[arg-type]
        env.messages.script("send", fault)  # type: ignore[arg-type]


def _crash_injector(definition: TaskDefinition, cell: Cell) -> FaultInjector | None:
    if cell.fault == "kill_after_remote_commit":
        return CrashInjector(CrashRule("after_response_receipt",
                                       when=lambda c: c.get("tool") in REMOTE_WRITES))
    if cell.fault == "kill_at_progress":
        first = definition.spec.milestones[0].id
        if cell.config == "C1":
            return CrashInjector(CrashRule("after_milestone_verified_commit",
                                           when=for_milestone(first)))
        # B0 equivalent: right after the response that completes the first milestone.
        steps = [s for s in definition.plan if s.milestone == first and not s.expect_reject]
        tools_m1 = {s.tool for s in steps}
        seen = {"n": 0}

        def completes_first(ctx: dict[str, Any]) -> bool:
            if ctx.get("tool") in tools_m1:
                seen["n"] += 1
            return seen["n"] == len(steps)

        return CrashInjector(CrashRule("after_response_receipt", when=completes_first))
    return None


def _model_faults(cell: Cell) -> ModelFaults | None:
    if cell.fault == "provider_unavailable":
        return ModelFaults(fail_always={"A": ModelError("provider_unavailable")})
    return None


def final_verify(definition: TaskDefinition, tools: ToolRegistry, workspace: Workspace) -> bool:
    """Independent end-of-run check: every milestone verifier passes on final state."""
    verifiers = definition.build_verifiers()
    for m in definition.spec.milestones:
        verifier = verifiers.get(m.verifier_id)
        assert verifier is not None

        def observe(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            tool = tools.get(tool_name)
            assert tool is not None
            ctx = ToolContext(run_id="score", action_id="score", attempt_no=1, milestone_id=None,
                              idempotency_key=None, workspace=workspace, timeout_seconds=5)
            data: dict[str, Any] = tool.invoke(args, ctx).data
            return data

        vctx = VerifierContext(run_id="score", milestone_id=m.id, spec=definition.spec,
                               workspace=workspace, confirmed_actions=[],
                               dependency_evidence={}, observe=observe)
        if verifier.verify(vctx).status != VerifierResult.PASS:
            return False
    return True


def _base_row(cell: Cell, definition: TaskDefinition) -> Row:
    return {**asdict(cell), "family": definition.family, "status": "", "model_calls": 0,
            "tokens": 0, "cost_usd": 0.0, "unpriced": False, "handoffs": 0,
            "human_escalations": 0, "unresolved": False, "ambiguous_actions": 0,
            "ambiguous_resolved": False, "blocked_repeat_proposals": 0,
            "preserved_num": 0, "preserved_den": 0}


def _score(row: Row, definition: TaskDefinition, env: ServiceEnv, tools: ToolRegistry,
           workspace: Workspace, reported_complete: bool) -> Row:
    gt = score_ground_truth(env, definition.expected)
    verified = reported_complete and gt.final_state_correct and \
        final_verify(definition, tools, workspace)
    intended = gt.intended_writes
    row.update({
        "reported_complete": reported_complete,
        "verified_completion": verified,
        # Completed correctly AND without any duplicate or unintended committed effect.
        "clean_completion": verified and gt.committed_duplicates == 0
        and gt.detail["unintended_targets"] == 0,
        "false_completion": reported_complete and not verified,
        "final_state_correct": gt.final_state_correct,
        "committed_duplicates": gt.committed_duplicates,
        "intended_writes": intended,
        "write_requests": gt.write_requests,
        "extra_write_requests": max(0, gt.write_requests - intended),
        "policy_bypass": gt.detail["unintended_targets"],
    })
    return row


def run_c1(cell: Cell, definition: TaskDefinition, work: Path, dsn: str) -> Row:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=cell.seed))
    schema = f"lab_{uuid.uuid4().hex[:12]}"
    store = Store(dsn, schema=schema, clock=clock, ids=SequentialIds(f"s{cell.seed}"),
                  pool_size=2)
    row = _base_row(cell, definition)
    try:
        store.migrate()
        env = _env(definition, work / "services", cell.mode, clock)
        _apply_service_fault(env, cell.fault)
        recorder = _Recorder()
        tools = recorder.wrap(definition.build_tools(env))
        router, _ = fake_router(definition, _model_faults(cell))
        input_root = work / "input"
        definition.write_inputs(input_root)
        faults = _crash_injector(definition, cell)
        if faults is not None:
            store.faults = faults

        def runtime(owner: str) -> Runtime:
            return Runtime(store=store, tools=tools, verifiers=definition.build_verifiers(),
                           models=router, clock=clock, ids=SequentialIds(owner), faults=faults,
                           config=RuntimeConfig(workspace_root=work / "ws",
                                                input_root=input_root, owner_id=owner))

        run_id = f"run_{cell.task}_{cell.seed}"
        try:
            result = runtime("ctl-1").start(definition.spec, run_id=run_id)
            status = result.status
        except SimulatedCrash:
            status = RunStatus.RECOVERING
        for attempt in range(2, 6):
            if status in TERMINAL_RUN_STATUSES or \
                    status == RunStatus.AWAITING_RECONCILIATION:
                break
            rt = runtime(f"ctl-{attempt}")
            if status == RunStatus.PAUSED_FOR_APPROVAL:
                for action_id in rt.status(run_id)["awaiting_approval"]:
                    rt.resolve_action(run_id, action_id, "approve", actor="lab-operator",
                                      reason="scripted lab approval")
            recorder.phase = "post"
            clock.advance(31)
            try:
                status = rt.resume(run_id, model_alias="B").status
            except SimulatedCrash:
                status = RunStatus.RECOVERING
        with store.read() as q:
            run = q.run(run_id)
            usage = q.usage(run_id)
            actions = q.actions(run_id)
            events = q.events(run_id)
            ambiguous = [a for a in actions
                         if any(t.status == "ambiguous" for t in q.attempts(a.action_id))]
        row["status"] = run.status.value
        row["model_calls"] = usage.model_calls
        row["tokens"] = usage.tokens_reported
        row["cost_usd"] = float(usage.cost_known_usd)
        row["unpriced"] = usage.has_unpriced_usage
        row["handoffs"] = sum(1 for e in events if e.type.value == "ModelSwitched")
        row["human_escalations"] = sum(1 for e in events if e.type.value in (
            "ActionNeedsHuman", "ApprovalRequested"))
        row["unresolved"] = run.status in (RunStatus.AWAITING_RECONCILIATION,
                                           RunStatus.PAUSED_FOR_APPROVAL)
        row["ambiguous_actions"] = len(ambiguous)
        row["ambiguous_resolved"] = all(a.status in (
            ActionStatus.CONFIRMED, ActionStatus.NEEDS_HUMAN, ActionStatus.SKIPPED,
            ActionStatus.FAILED_CLEAN) for a in ambiguous)
        row["blocked_repeat_proposals"] = sum(1 for e in events
                                              if e.type.value == "ActionProposalDeduped")
        if cell.fault in ("kill_after_remote_commit", "kill_at_progress"):
            row["preserved_num"], row["preserved_den"] = recorder.preserved()
        workspace = Workspace(work / "ws", run_id)
        return _score(row, definition, env, definition.build_tools(env), workspace,
                      run.status == RunStatus.COMPLETED)
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'.encode())


def run_b0(cell: Cell, definition: TaskDefinition, work: Path) -> Row:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=cell.seed))
    row = _base_row(cell, definition)
    env = _env(definition, work / "services", cell.mode, clock)
    _apply_service_fault(env, cell.fault)
    recorder = _Recorder()
    tools = recorder.wrap(definition.build_tools(env))
    router, _ = fake_router(definition, _model_faults(cell))
    # Same agent policy, but with the fair-baseline "check before repeating writes" rule.
    for alias in router.aliases():
        adapter = router.get(alias).adapter
        if isinstance(adapter, FakeModelAdapter):
            adapter.policy = PlanPolicy(definition.plan, check_before_repeat=True)
    definition.write_inputs(work / "input")
    workspace = Workspace(work / "ws", "b0")
    workspace.init()
    workspace.snapshot_inputs(definition.spec, work / "input")
    approvals = {"n": 0}

    def approver(name: str, args: dict[str, Any]) -> bool:
        approvals["n"] += 1
        return True

    runner = BaselineRunner(definition, tools=tools, router=router, workspace=workspace,
                            transcript_path=work / "b0" / "transcript.json", clock=clock,
                            faults=_crash_injector(definition, cell), approver=approver)
    result = None
    handoff = False
    for _ in range(5):
        try:
            result = runner.run(handoff=handoff)
            break
        except SimulatedCrash:
            recorder.phase = "post"
            handoff = True
    finished = bool(result and result.finished)
    row["status"] = (result.reason if result else "crashed")
    if result is not None:
        row.update(model_calls=result.model_calls, tokens=result.tokens,
                   cost_usd=result.cost_usd, unpriced=result.unpriced,
                   handoffs=result.handoffs)
    row["human_escalations"] = approvals["n"]
    if cell.fault in ("kill_after_remote_commit", "kill_at_progress"):
        row["preserved_num"], row["preserved_den"] = recorder.preserved()
    return _score(row, definition, env, definition.build_tools(env), workspace, finished)


def run_cell(cell: Cell, work_root: Path, dsn: str) -> Row:
    definition = get_task(cell.task)
    work = work_root / f"{cell.config}-{cell.task}-{cell.fault}-{cell.mode}-{cell.seed}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    started = time.monotonic()
    try:
        row = run_c1(cell, definition, work, dsn) if cell.config == "C1" else \
            run_b0(cell, definition, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    row["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return row


SUITES: dict[str, dict[str, Any]] = {
    "smoke": {"tasks": ["inventory_upload_v1", "db_update_selected", "notify_welcome"],
              "faults": ["none"], "modes": ["deduped"], "seeds": 1},
    "safety": {"tasks": sorted(CATALOG), "faults": FAULTS, "modes": MODES, "seeds": 2},
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="continuum-lab")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run a paired B0/C1 experiment matrix")
    run.add_argument("--suite", choices=sorted(SUITES), default="smoke")
    run.add_argument("--tasks", nargs="*")
    run.add_argument("--faults", nargs="*", choices=FAULTS)
    run.add_argument("--modes", nargs="*", choices=MODES)
    run.add_argument("--seeds", type=int)
    run.add_argument("--configs", nargs="*", default=["B0", "C1"], choices=["B0", "C1"])
    run.add_argument("--out", type=Path, default=Path("results"))
    run.add_argument("--dsn", default=os.environ.get("CONTINUUM_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args(argv)

    suite = SUITES[args.suite]
    cells = relevant_cells(args.tasks or suite["tasks"], args.faults or suite["faults"],
                           args.modes or suite["modes"], args.seeds or suite["seeds"],
                           args.configs)
    args.out.mkdir(parents=True, exist_ok=True)
    rows: list[Row] = []
    progress: Callable[[str], None] = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    for i, cell in enumerate(cells, 1):
        row = run_cell(cell, args.out / "work", args.dsn)
        rows.append(row)
        progress(f"[{i}/{len(cells)}] {cell.config} {cell.task} {cell.fault} {cell.mode} "
                 f"s{cell.seed}: {row['status']} dup={row['committed_duplicates']}")
    shutil.rmtree(args.out / "work", ignore_errors=True)
    meta = {"suite": args.suite, "adapter": "deterministic fake (PlanPolicy)",
            "seeds": args.seeds or suite["seeds"],
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "command": "continuum-lab " + " ".join(argv or sys.argv[1:])}
    (args.out / "results.json").write_text(canonical_json({"meta": meta, "rows": rows}))
    (args.out / "report.md").write_text(render(rows, meta))
    print(json.dumps({"cells": len(rows), "out": str(args.out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
