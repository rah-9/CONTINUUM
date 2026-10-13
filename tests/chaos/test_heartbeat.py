"""A background heartbeat keeps ownership through a tool call longer than the lease TTL."""

import time
from pathlib import Path

import pytest

from continuum.core.clock import SystemClock
from continuum.core.ids import SequentialIds
from continuum.core.runtime import Runtime, RuntimeConfig
from continuum.core.state import RunStatus
from continuum.core.store import Store
from continuum.tools.manifest import ToolRegistry
from continuum.tools.trusted.remote import DbUpdate
from continuum_lab.harness import fake_router, make_env
from continuum_lab.tasks.catalog import get_task
from tests.conftest import DSN

pytestmark = pytest.mark.chaos


class SlowDbUpdate(DbUpdate):
    def invoke(self, args, ctx):
        time.sleep(2.5)  # longer than the 2s lease TTL
        return super().invoke(args, ctx)


def _runtime(schema: str, root: Path, heartbeat: float | None) -> tuple[Runtime, object]:
    definition = get_task("db_update_selected")
    env = make_env(definition, root / "services")
    store = Store(DSN, schema=schema, clock=SystemClock(), ids=SequentialIds("h"))
    store.migrate()
    registry = definition.build_tools(env)
    slow = SlowDbUpdate(env.db)
    tools = ToolRegistry([slow if n == "db.update" else registry.get(n)
                          for n in registry.names()])
    router, _ = fake_router(definition)
    config = RuntimeConfig(workspace_root=root / "ws", input_root=root / "in",
                           owner_id="ctl-hb", lease_ttl_seconds=2,
                           heartbeat_interval_seconds=heartbeat)
    rt = Runtime(store=store, tools=tools, verifiers=definition.build_verifiers(),
                 models=router, config=config, clock=SystemClock(), ids=SequentialIds("h"))
    return rt, env


def test_heartbeat_keeps_lease_through_slow_tool(schema, tmp_path):
    rt, env = _runtime(schema, tmp_path, heartbeat=0.5)
    try:
        result = rt.start(get_task("db_update_selected").spec)
        assert result.status == RunStatus.COMPLETED
        assert env.db.commits_for("r1") == 1
    finally:
        rt.store.close()


def test_without_heartbeat_slow_tool_loses_lease_and_stops_safely(schema, tmp_path):
    rt, env = _runtime(schema, tmp_path, heartbeat=None)
    try:
        result = rt.start(get_task("db_update_selected").spec)
        assert result.status != RunStatus.COMPLETED
        assert "lease lost" in result.message
        assert env.db.commits_for("r1") == 1  # the in-flight effect committed remotely...
        with rt.store.read() as q:  # ...but this stale owner could not record it
            [attempt] = q.started_attempts(result.run_id)
            assert attempt.status == "started"
    finally:
        rt.store.close()
