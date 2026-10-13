"""Shared fixtures. Every test gets an isolated PostgreSQL schema.

Set ``CONTINUUM_TEST_DATABASE_URL`` to point at a disposable database.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from continuum.core.clock import FakeClock
from continuum.core.events import RunCreated, RunStarted, make_event
from continuum.core.evidence import Workspace
from continuum.core.faults import FaultInjector, NoFaults
from continuum.core.ids import SequentialIds
from continuum.core.runtime import Runtime
from continuum.core.store import Store
from continuum.models.base import ModelRouter
from continuum.models.fake import FakeModelAdapter
from continuum.tools.gateway import RunContext
from continuum_lab.harness import ModelFaults, build_runtime, fake_router, make_env
from continuum_lab.tasks.catalog import get_task
from continuum_lab.tasks.definition import ServiceEnv, TaskDefinition

DSN = os.environ.get("CONTINUUM_TEST_DATABASE_URL",
                     "postgresql://continuum:continuum@localhost:5432/continuum_test")


@pytest.fixture
def schema() -> Iterator[str]:
    name = f"t_{uuid.uuid4().hex[:12]}"
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'.encode())


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(schema: str, clock: FakeClock) -> Iterator[Store]:
    s = Store(DSN, schema=schema, clock=clock, ids=SequentialIds("e"))
    s.migrate()
    yield s
    s.close()


@dataclass
class Lab:
    """A task wired to a runtime, mock services and fake models."""

    definition: TaskDefinition
    env: ServiceEnv
    router: ModelRouter
    adapters: dict[str, FakeModelAdapter]
    runtime: Runtime
    root: Path
    store: Store
    clock: FakeClock

    def open_run(self, run_id: str = "run_t") -> RunContext:
        """Create and lease a run without driving the model loop (gateway-level tests)."""
        spec, rt = self.definition.spec, self.runtime
        ws = Workspace(rt.config.workspace_root, run_id)
        ws.init()
        ws.snapshot_inputs(spec, rt.config.input_root)
        with self.store.run_tx(run_id, None, create=True) as tx:
            tx.append(make_event(RunCreated(task_id=spec.task_id, spec=spec.canonical(),
                                            owner_id="ctl-1"), actor="test"))
        lease = rt.leases.acquire(run_id, "ctl-1")
        self.store.append(run_id, lease.fence, [make_event(
            RunStarted(owner_id="ctl-1", epoch=lease.epoch), actor="test")])
        return RunContext(run_id, lease, spec, ws)

    def new_runtime(self, *, owner_id: str = "ctl-2", faults: FaultInjector | None = None,
                    router: ModelRouter | None = None) -> Runtime:
        """A fresh controller process view over the same DB, workspace and services."""
        self.store.faults = faults or NoFaults()
        return build_runtime(self.definition, store=self.store, env=self.env, root=self.root,
                             clock=self.clock, ids=SequentialIds(owner_id),
                             router=router or self.router, faults=faults, owner_id=owner_id)


LabFactory = Callable[..., Lab]


@pytest.fixture
def make_lab(store: Store, clock: FakeClock, tmp_path: Path) -> LabFactory:
    def factory(task_id: str, *, faults: FaultInjector | None = None,
                model_faults: ModelFaults | None = None, **env_overrides: Any) -> Lab:
        definition = get_task(task_id)
        env_overrides.setdefault("now_fn", lambda: clock.now().timestamp())
        env = make_env(definition, tmp_path / "services", **env_overrides)
        router, adapters = fake_router(definition, model_faults)
        runtime = build_runtime(definition, store=store, env=env, root=tmp_path,
                                clock=clock, ids=SequentialIds("a"), router=router,
                                faults=faults, owner_id="ctl-1")
        return Lab(definition, env, router, adapters, runtime, tmp_path, store, clock)

    return factory
