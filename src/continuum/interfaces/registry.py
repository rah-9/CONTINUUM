"""Pre-registered task specs and model routing loaded from ``config/``.

``config/tasks/*.yaml`` entries name a trusted factory (``module:function``)
that returns a :class:`TaskBinding`. Only pre-registered tasks can be run by
the CLI, HTTP API and MCP facade.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from continuum.core.errors import NotFoundError, SpecValidationError, UnsupportedCapabilityError
from continuum.core.state import TaskSpec
from continuum.models.base import CapabilityProfile, ModelEndpoint, ModelRouter
from continuum.models.litellm_adapter import LiteLLMAdapter
from continuum.models.profiles import registry_profile
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


def _load_factory(ref: Any) -> TaskFactory:
    module_name, _, attr = str(ref).partition(":")
    if not module_name or not attr:
        raise SpecValidationError(f"factory must be 'module:function', got {ref!r}")
    try:
        factory: TaskFactory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise SpecValidationError(f"task factory {ref!r} cannot be loaded: "
                                  f"{type(exc).__name__}") from exc
    return factory


class TaskRegistry:
    def __init__(self, config_dir: Path, state_dir: Path) -> None:
        self.config_dir = config_dir
        self.state_dir = state_dir
        self._entries: dict[str, dict[str, Any]] = {}
        # Malformed task files are reported (``errors``, ``continuum doctor``) instead of
        # making every command, including read-only inspection, fail at startup.
        self.errors: dict[Path, str] = {}
        tasks_dir = config_dir / "tasks"
        for path in sorted(tasks_dir.glob("*.yaml")) if tasks_dir.is_dir() else []:
            try:
                entry = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
                self.errors[path] = f"unreadable (YAML must be UTF-8): {type(exc).__name__}"
                continue
            task_id = entry.get("task_id") if isinstance(entry, dict) else None
            if not isinstance(task_id, str) or not task_id:
                self.errors[path] = "not a mapping with a task_id"
                continue
            if task_id in self._entries:  # the first file (sorted by name) wins
                self.errors[path] = f"task {task_id} is already registered by another file"
                continue
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
        factory = _load_factory(entry.get("factory"))
        options = entry.get("options") or {}
        if not isinstance(options, dict):
            raise SpecValidationError(f"task {task_id}: options must be a mapping")
        binding = factory(self.state_dir / "tasks" / task_id, dict(options))
        if not isinstance(binding, TaskBinding):
            raise SpecValidationError(f"task {task_id}: factory did not return a TaskBinding")
        if binding.spec.task_id != task_id:
            # The registered id is the run's identity: resume and status look the
            # binding (and its tool/service state) up by the run's task_id.
            binding = replace(binding, spec=binding.spec.model_copy(update={"task_id": task_id}))
        return binding


def _live_endpoint(model_id: str, alias: str | None = None,
                   completion_kwargs: dict[str, Any] | None = None) -> ModelEndpoint:
    return ModelEndpoint(profile=registry_profile(model_id, alias),
                         adapter=LiteLLMAdapter(model_id, **(completion_kwargs or {})))


def load_live_router(path: Path) -> ModelRouter:
    """Build LiteLLM endpoints from ``config/models.yaml``.

    Each entry is either a verified profile, or ``profile: auto`` to take limits and
    prices from the model registry (rejected if the registry entry is incomplete).
    Any ``provider/model-id`` not listed resolves on demand the same way, so a run can
    switch to a newly released model version without code changes. Placeholder
    profiles are rejected at session start, never silently used.
    """
    try:
        return _load_live_router(path)
    except (yaml.YAMLError, KeyError, TypeError, AttributeError, ValueError) as exc:
        # pydantic.ValidationError is a ValueError
        raise SpecValidationError(f"{path} is invalid: {type(exc).__name__}: "
                                  f"{str(exc).splitlines()[0][:200]}") from exc


def _load_live_router(path: Path) -> ModelRouter:
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}
    if not isinstance(data, dict):
        raise TypeError("models.yaml must be a mapping with a 'models' key")
    endpoints: dict[str, ModelEndpoint] = {}
    for alias, cfg in (data.get("models") or {}).items():
        cfg = dict(cfg)
        kwargs = cfg.pop("completion_kwargs", {}) or {}
        if cfg.get("profile") == "auto":
            try:
                endpoints[alias] = _live_endpoint(cfg["model_id"], alias, kwargs)
            except UnsupportedCapabilityError:
                # keep the alias visible; it is rejected (with the reason) at session start
                endpoints[alias] = ModelEndpoint(
                    profile=CapabilityProfile(alias=alias, model_id=cfg["model_id"],
                                              context_window_tokens=0, max_output_tokens=0,
                                              supports_native_tools=False,
                                              profile_source="registry_incomplete"),
                    adapter=LiteLLMAdapter(cfg["model_id"], **kwargs))
            continue
        profile = CapabilityProfile.model_validate({"alias": alias, **cfg})
        endpoints[alias] = ModelEndpoint(profile=profile,
                                         adapter=LiteLLMAdapter(profile.model_id, **kwargs))
    return ModelRouter(endpoints, resolver=_live_endpoint)
