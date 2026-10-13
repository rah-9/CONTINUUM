"""Task factory used by ``config/tasks/*.yaml`` to expose lab tasks to the SDK/CLI.

Options:
  task:         lab catalog task id (defaults to the registered task id)
  fake_models:  true (default) uses the deterministic plan-following fake agent;
                false routes to live providers from ``config/models.yaml``
  store_mode / db_mode / msg_mode: mock connector contract modes
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from continuum.interfaces.registry import TaskBinding
from continuum_lab.harness import fake_router, make_env
from continuum_lab.tasks.catalog import get_task


def lab_task(state_dir: Path, options: dict[str, Any]) -> TaskBinding:
    definition = get_task(options["task"])
    modes = {k: options[k] for k in ("store_mode", "db_mode", "msg_mode") if k in options}
    services = state_dir / "services"
    # Seed mock services once; later processes (resume) must see the same ground truth.
    env = make_env(definition, services, seed=not services.exists(), **modes)
    input_root = state_dir / "inputs"
    definition.write_inputs(input_root)
    router = fake_router(definition)[0] if options.get("fake_models", True) else None
    return TaskBinding(spec=definition.spec, tools=definition.build_tools(env),
                       verifiers=definition.build_verifiers(), input_root=input_root,
                       models=router)

