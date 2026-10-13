"""Wire a lab task definition to a Continuum runtime with fake models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from continuum.core.clock import Clock
from continuum.core.faults import FaultInjector
from continuum.core.ids import IdGenerator
from continuum.core.runtime import Runtime, RuntimeConfig
from continuum.core.store import Store
from continuum.models.base import ModelEndpoint, ModelError, ModelRouter
from continuum.models.fake import FakeModelAdapter, fake_profile
from continuum_lab.tasks.definition import ServiceEnv, TaskDefinition
from continuum_lab.tasks.policy import PlanPolicy


@dataclass
class ModelFaults:
    """Per-alias injected model errors keyed by call number, or permanent failure."""

    errors: dict[str, dict[int, ModelError]] = field(default_factory=dict)
    fail_always: dict[str, ModelError] = field(default_factory=dict)
    context_window: dict[str, int] = field(default_factory=dict)
    no_tools: set[str] = field(default_factory=set)


def fake_router(definition: TaskDefinition, faults: ModelFaults | None = None,
                aliases: tuple[str, ...] = ("A", "B", "C")) -> tuple[ModelRouter,
                                                                    dict[str, FakeModelAdapter]]:
    faults = faults or ModelFaults()
    adapters: dict[str, FakeModelAdapter] = {}
    endpoints: dict[str, ModelEndpoint] = {}
    for alias in aliases:
        adapter = FakeModelAdapter(PlanPolicy(definition.plan),
                                   errors=faults.errors.get(alias),
                                   fail_always=faults.fail_always.get(alias))
        adapters[alias] = adapter
        profile = fake_profile(alias,
                               context_window_tokens=faults.context_window.get(alias, 128_000),
                               supports_native_tools=alias not in faults.no_tools)
        endpoints[alias] = ModelEndpoint(profile=profile, adapter=adapter)
    return ModelRouter(endpoints), adapters


def make_env(definition: TaskDefinition, state_dir: Path, *, seed: bool = True,
             **overrides: Any) -> ServiceEnv:
    params = {**definition.env_overrides, **overrides}
    env = ServiceEnv(state_dir=state_dir, **params)
    if seed:
        definition.seed(env)
    return env


def build_runtime(definition: TaskDefinition, *, store: Store, env: ServiceEnv, root: Path,
                  clock: Clock, ids: IdGenerator, router: ModelRouter,
                  faults: FaultInjector | None = None, owner_id: str = "ctl-1",
                  lease_ttl_seconds: int = 30) -> Runtime:
    if faults is not None:
        store.faults = faults  # store-level boundaries (event commit) use the same injector
    input_root = root / "input"
    definition.write_inputs(input_root)
    config = RuntimeConfig(workspace_root=root / "workspaces", input_root=input_root,
                           owner_id=owner_id, lease_ttl_seconds=lease_ttl_seconds)
    return Runtime(store=store, tools=definition.build_tools(env),
                   verifiers=definition.build_verifiers(), models=router, config=config,
                   clock=clock, ids=ids, faults=faults)
