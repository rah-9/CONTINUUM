"""Pre-registered task specs and model routing loaded from ``config/``.

``config/tasks/*.yaml`` entries name a trusted factory (``module:function``)
that returns a :class:`TaskBinding`. Only pre-registered tasks can be run by
the CLI, HTTP API and MCP facade.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from continuum.core.errors import NotFoundError, SpecValidationError
from continuum.core.state import TaskSpec
from continuum.models.base import CapabilityProfile, ModelEndpoint, ModelRouter
from continuum.models.litellm_adapter import LiteLLMAdapter
from continuum.tools.manifest import ToolRegistry
from continuum.verify.registry import VerifierRegistry


@dataclass(frozen=True)
class TaskBinding:
    spec: TaskSpec
    tools: ToolRegistry
    verifiers: VerifierRegistry
    input_root: Path
    models: ModelRouter | None = None  # None -> use config/models.yaml (live)


TaskFactory = Callable[[Path, dict[str, Any]], TaskBinding]


def _load_factory(ref: str) -> TaskFactory:
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr:
        raise SpecValidationError(f"factory must be 'module:function', got {ref!r}")
    factory: TaskFactory = getattr(importlib.import_module(module_name), attr)
    return factory


class TaskRegistry:
    def __init__(self, config_dir: Path, state_dir: Path) -> None:
        self.config_dir = config_dir
        self.state_dir = state_dir
        self._entries: dict[str, dict[str, Any]] = {}
        tasks_dir = config_dir / "tasks"
        for path in sorted(tasks_dir.glob("*.yaml")) if tasks_dir.is_dir() else []:
            entry = yaml.safe_load(path.read_text())
            task_id = entry["task_id"]
            if task_id in self._entries:
                raise SpecValidationError(f"task {task_id} registered twice")
            self._entries[task_id] = entry

    def task_ids(self) -> list[str]:
        return sorted(self._entries)

    def describe(self) -> list[dict[str, Any]]:
        return [{"task_id": k, "description": v.get("description", "")}
                for k, v in sorted(self._entries.items())]

    def binding(self, task_id: str) -> TaskBinding:
        entry = self._entries.get(task_id)
        if entry is None:
            raise NotFoundError(f"task {task_id!r} is not pre-registered")
        factory = _load_factory(entry["factory"])
        return factory(self.state_dir / "tasks" / task_id, dict(entry.get("options") or {}))


def load_live_router(path: Path) -> ModelRouter:
    """Build LiteLLM endpoints from ``config/models.yaml``. Profiles are validated at
    session start; placeholder values are rejected there, never silently used."""
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    endpoints: dict[str, ModelEndpoint] = {}
    for alias, cfg in (data.get("models") or {}).items():
        cfg = dict(cfg)
        kwargs = cfg.pop("completion_kwargs", {}) or {}
        profile = CapabilityProfile.model_validate({"alias": alias, **cfg})
        endpoints[alias] = ModelEndpoint(profile=profile,
                                         adapter=LiteLLMAdapter(profile.model_id, **kwargs))
    return ModelRouter(endpoints)
