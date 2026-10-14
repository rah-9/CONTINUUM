"""Non-functional requirements (docs/01 section 5) as executable checks.

* NF1 no database lock is held across a tool or model call
* NF2 secrets never reach events, blobs or logs, even when tools/models echo them
* NF3 reproducibility: same inputs, ids and clock give the same event stream and packets
* NF4 concurrency: many runs progress in parallel processes on one database
* NF5 performance baselines (generous bounds; measured values are printed)
"""

from __future__ import annotations

import io
import multiprocessing as mp
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

from continuum.core.clock import FakeClock, SystemClock
from continuum.core.events import RunActiveTimeRecorded, make_event
from continuum.core.ids import SequentialIds
from continuum.core.state import RunStatus
from continuum.core.store import Store
from continuum.core.tracing import configure_logging
from continuum.models.base import ModelEndpoint, ModelRouter
from continuum.models.fake import FakeModelAdapter, fake_profile
from continuum.tools.manifest import ToolRegistry, TrustedTool
from continuum_lab.harness import build_runtime, fake_router, make_env
from continuum_lab.tasks.catalog import get_task
from continuum_lab.tasks.policy import PlanPolicy
from tests.conftest import DSN

pytestmark = pytest.mark.nonfunctional


def _probe_unlocked(schema: str, run_id: str) -> bool:
    """From an independent connection: can the run row and lease row be locked NOW?"""
    with psycopg.connect(DSN, options=f"-c search_path={schema}") as conn:
        conn.execute(b"SET lock_timeout = '0'")
        try:
            conn.execute(b"SELECT 1 FROM runs WHERE run_id = %s FOR NO KEY UPDATE NOWAIT",
                         (run_id,))
            conn.execute(b"SELECT 1 FROM run_leases WHERE run_id = %s FOR UPDATE NOWAIT",
                         (run_id,))
            return True
        except psycopg.errors.LockNotAvailable:
            return False
        finally:
            conn.rollback()


class ProbingTool(TrustedTool):
    def __init__(self, inner: TrustedTool, probes: list[bool], schema: str) -> None:
        self.inner, self.probes, self.schema = inner, probes, schema
        self.manifest = inner.manifest

    def bind(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return self.inner.bind(args, ctx)

    def invoke(self, args: dict[str, Any], ctx: Any) -> Any:
        self.probes.append(_probe_unlocked(self.schema, ctx.run_id))
        return self.inner.invoke(args, ctx)


def test_nf1_no_db_lock_held_across_tool_or_model_calls(make_lab):
    lab = make_lab("upload_and_notify")
    tool_probes: list[bool] = []
    model_probes: list[bool] = []
    tools = ToolRegistry([ProbingTool(lab.runtime.tools.get(n), tool_probes, lab.store.schema)
                          for n in lab.runtime.tools.names()])
    policy = PlanPolicy(lab.definition.plan)
    run_ids: list[str] = []

    def probing_policy(messages: Any, schemas: Any) -> Any:
        if run_ids:
            model_probes.append(_probe_unlocked(lab.store.schema, run_ids[0]))
        return policy(messages, schemas)

    adapter = FakeModelAdapter(probing_policy)
    router = ModelRouter({a: ModelEndpoint(profile=fake_profile(a), adapter=adapter)
                          for a in "ABC"})
    rt = lab.new_runtime(owner_id="ctl-1", router=router)
    rt.tools = tools
    rt.gateway.registry = tools
    original = rt.journal.append_events

    def remember(ctx: Any, events: Any) -> None:
        if not run_ids:
            run_ids.append(ctx.run_id)
        original(ctx, events)

    rt.journal.append_events = remember  # type: ignore[method-assign]
    assert rt.start(lab.definition.spec).status == RunStatus.COMPLETED
    assert tool_probes and all(tool_probes), "a DB lock was held during a tool call"
    assert model_probes and all(model_probes), "a DB lock was held during a model call"


SECRET = "sk-live-continuum-test-9f8e7d6c5b4a"


def test_nf2_secrets_never_persisted_or_logged(make_lab, monkeypatch, tmp_path):
    monkeypatch.setenv("PAYMENTS_API_KEY", SECRET)
    lab = make_lab("db_update_selected")
    from continuum.core.redaction import Redactor

    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    try:
        policy = PlanPolicy(lab.definition.plan)

        def leaky_model(messages: Any, schemas: Any) -> Any:  # a model echoing a secret
            response = policy(messages, schemas)
            return response.model_copy(update={"message": response.message.model_copy(
                update={"content": f"using key {SECRET}"})})

        router = ModelRouter({a: ModelEndpoint(profile=fake_profile(a),
                                               adapter=FakeModelAdapter(leaky_model))
                              for a in "ABC"})
        rt = lab.new_runtime(owner_id="ctl-1", router=router)
        rt.redactor = Redactor.from_environment()
        rt.gateway.redactor = rt.redactor
        rt.sessions.redactor = rt.redactor
        rt.verification.redactor = rt.redactor
        lab.env.db.seed({"r1": {"status": "pending", "note": SECRET}})  # tool output leak
        result = rt.start(lab.definition.spec)
        assert result.status == RunStatus.COMPLETED
    finally:
        configure_logging("WARNING")
    with lab.store.connection() as conn:
        dump = conn.execute(b"SELECT string_agg(payload_json::text, '') AS t FROM events"
                            ).fetchone()["t"]
    assert SECRET not in dump
    blobs = "".join(p.read_text() for p in (lab.root / "workspaces").rglob("*.json"))
    assert "[REDACTED]" in blobs and SECRET not in blobs
    assert SECRET not in stream.getvalue()


def _event_stream(store_schema: str, root: Path) -> tuple[list[tuple[str, str]], list[str]]:
    definition = get_task("upload_and_notify")
    clock = FakeClock()
    store = Store(DSN, schema=store_schema, clock=clock, ids=SequentialIds("d"))
    try:
        store.migrate()
        env = make_env(definition, root / "svc", now_fn=lambda: clock.now().timestamp())
        router, _ = fake_router(definition)
        rt = build_runtime(definition, store=store, env=env, root=root, clock=clock,
                           ids=SequentialIds("d"), router=router)
        result = rt.start(definition.spec, run_id="run_det")
        with store.read() as q:
            events = q.events(result.run_id)
        return ([(e.type.value, e.actor) for e in events],
                [e.payload.get("packet_sha256") or e.payload.get("response_ref") or ""
                 for e in events])
    finally:
        store.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{store_schema}" CASCADE'.encode())


def test_nf3_runs_are_reproducible(tmp_path, schema):
    first = _event_stream(schema + "a", tmp_path / "a")
    second = _event_stream(schema + "b", tmp_path / "b")
    assert first[0] == second[0]  # identical event types and actors, in order
    assert first[1] == second[1]  # identical packet hashes and model-response digests


def _worker(schema: str, root: str, task_id: str, out: Any) -> None:
    definition = get_task(task_id)
    store = Store(DSN, schema=schema, clock=SystemClock(), ids=SequentialIds(f"w{task_id}"),
                  pool_size=2)
    try:
        env = make_env(definition, Path(root) / "svc")
        router, _ = fake_router(definition)
        rt = build_runtime(definition, store=store, env=env, root=Path(root),
                           clock=SystemClock(), ids=SequentialIds(task_id), router=router,
                           owner_id=f"ctl-{task_id}")
        started = time.monotonic()
        result = rt.start(definition.spec, run_id=f"run_{task_id}")
        out.put((task_id, result.status.value, time.monotonic() - started,
                 store.projection_diff(result.run_id)))
    finally:
        store.close()


def test_nf4_parallel_runs_on_one_database(schema, store, tmp_path):
    tasks = ["inventory_upload_v1", "db_partial_batch", "notify_welcome", "upload_and_notify",
             "csv_dedupe_report", "db_update_report"]
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(schema, str(tmp_path / t), t, out))
             for t in tasks]
    started = time.monotonic()
    for p in procs:
        p.start()
    results = [out.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(30)
    wall = time.monotonic() - started
    print(f"\nNF4: {len(tasks)} concurrent runs in {wall:.2f}s; per-run "
          f"{', '.join(f'{t}={d:.2f}s' for t, _, d, _ in sorted(results))}")
    assert {(t, s) for t, s, _, _ in results} == {(t, "completed") for t in tasks}
    assert all(diff == [] for *_, diff in results)


def test_nf5_performance_baselines(store, clock, make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    n = 500
    started = time.perf_counter()
    for _ in range(n):
        store.append(ctx.run_id, ctx.lease.fence,
                     [make_event(RunActiveTimeRecorded(elapsed_ms=1), actor="bench")])
    append_rate = n / (time.perf_counter() - started)

    durations = []
    for i in range(5):
        run = make_lab("inventory_upload_v1")
        t0 = time.perf_counter()
        rt = run.new_runtime(owner_id=f"perf{i}")  # distinct deterministic id namespace
        result = rt.start(run.definition.spec, run_id=f"run_perf_{i}")
        assert result.status == RunStatus.COMPLETED
        durations.append(time.perf_counter() - t0)
    durations.sort()
    print(f"\nNF5: fenced append+projection {append_rate:.0f} events/s; "
          f"end-to-end run p50 {durations[2] * 1000:.0f} ms, max {durations[-1] * 1000:.0f} ms")
    assert append_rate > 100  # each append is one fenced transaction with projections
    assert durations[2] < 5.0


def test_nf5_coding_observation_cost(tmp_path):
    """Every coding operation fingerprints the repository: keep it to a fixed, small number
    of git processes regardless of repository size (and report the time)."""
    import subprocess

    import continuum.coding.repo as repo

    root = tmp_path / "big"
    root.mkdir()
    for d in range(50):
        (root / f"pkg{d}").mkdir()
        for f in range(40):
            (root / f"pkg{d}" / f"m{f}.py").write_text(f"x = {f}\n" * 20)
    for args in (["init", "-q"], ["config", "user.email", "d@e.x"], ["config", "user.name", "d"],
                 ["add", "."], ["commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    for d in range(20):
        (root / f"pkg{d}" / "m0.py").write_text("changed\n")
        (root / f"pkg{d}" / "new.py").write_text("new\n")
    calls: list[str] = []
    real = repo._git

    def counting(r: Path, *args: str) -> bytes | None:
        calls.append(args[0])
        return real(r, *args)

    repo._git = counting  # type: ignore[assignment]
    try:
        started = time.perf_counter()
        state = repo.observe(root)
        elapsed = time.perf_counter() - started
    finally:
        repo._git = real  # type: ignore[assignment]
    print(f"\nNF5 coding observe: 2,000 files, {len(state.changed_files)} changed: "
          f"{elapsed * 1000:.0f} ms, {len(calls)} git calls")
    assert len(calls) <= 5 and len(state.changed_files) == 40
    assert elapsed < 5.0
