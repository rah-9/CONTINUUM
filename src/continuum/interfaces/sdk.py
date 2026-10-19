# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Python SDK: ``start``, ``resume``, ``status``, ``resolve_action`` (R10)."""

from __future__ import annotations

from typing import Any

from continuum.coding.service import CodingContinuity
from continuum.core.clock import SystemClock
from continuum.core.ids import RandomIds
from continuum.core.runtime import Decision, RunResult, Runtime, RuntimeConfig
from continuum.core.store import Store
from continuum.interfaces.registry import TaskBinding, TaskRegistry, load_live_router
from continuum.interfaces.settings import Settings
from continuum.models.base import ModelRouter


class Continuum:
    """Local SDK over a PostgreSQL store and pre-registered tasks."""

    def __init__(self, settings: Settings | None = None, *, store: Store | None = None,
                 registry: TaskRegistry | None = None) -> None:
        self.settings = settings or Settings()
        self.clock = SystemClock()
        self.ids = RandomIds()
        self.store = store or Store(self.settings.database_url, schema=self.settings.db_schema,
                                    clock=self.clock, ids=self.ids,
                                    pool_timeout_seconds=self.settings.db_timeout_seconds)
        self.registry = registry or TaskRegistry(self.settings.config_dir, self.settings.home)
        self.coding = CodingContinuity(self.store, clock=self.clock, ids=self.ids)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> Continuum:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def init_db(self) -> None:
        self.store.migrate()

    def ensure_schema(self) -> bool:
        """Create or upgrade the schema only when needed (cheap version check)."""
        return self.store.ensure_schema()

    def tasks(self) -> list[dict[str, Any]]:
        return self.registry.describe()

    def _runtime(self, binding: TaskBinding, *, read_only: bool = False) -> Runtime:
        # Read-only inspection never needs live models, so a broken models.yaml cannot
        # hide a run's state from the person trying to repair it.
        models = binding.models or (ModelRouter({}) if read_only else load_live_router(
            self.settings.config_dir / "models.yaml"))
        config = RuntimeConfig(workspace_root=self.settings.home / "workspaces",
                               input_root=binding.input_root, owner_id=self.settings.owner_id,
                               lease_ttl_seconds=self.settings.lease_ttl_seconds,
                               heartbeat_interval_seconds=max(
                                   self.settings.lease_ttl_seconds / 3, 1.0))
        return Runtime(store=self.store, tools=binding.tools, verifiers=binding.verifiers,
                       models=models, config=config, clock=self.clock, ids=self.ids)

    def _runtime_for_run(self, run_id: str, *, read_only: bool = False) -> Runtime:
        with self.store.read() as q:
            task_id = q.run(run_id).task_id
        return self._runtime(self.registry.binding(task_id), read_only=read_only)

    def start(self, task_id: str, *, model_alias: str | None = None) -> RunResult:
        binding = self.registry.binding(task_id)
        return self._runtime(binding).start(binding.spec, model_alias=model_alias)

    def resume(self, run_id: str, *, model_alias: str | None = None) -> RunResult:
        return self._runtime_for_run(run_id).resume(run_id, model_alias=model_alias)

    def status(self, run_id: str) -> dict[str, Any]:
        return self._runtime_for_run(run_id, read_only=True).status(run_id)

    def resolve_action(self, run_id: str, action_id: str, decision: Decision, *, actor: str,
                       reason: str, evidence_ref: str | None = None) -> RunResult:
        return self._runtime_for_run(run_id).resolve_action(
            run_id, action_id, decision, actor=actor, reason=reason, evidence_ref=evidence_ref)

    def cancel(self, run_id: str, *, actor: str, reason: str) -> RunResult:
        return self._runtime_for_run(run_id).cancel(run_id, actor=actor, reason=reason)

    def inspect(self, run_id: str) -> dict[str, Any]:
        return self._runtime_for_run(run_id, read_only=True).inspect(run_id)

    def pending_decisions(self, run_id: str) -> list[dict[str, Any]]:
        """Read-only: decisions only a human may make, with the command to make them."""
        return self._runtime_for_run(run_id, read_only=True).pending_decisions(run_id)

    def doctor(self) -> dict[str, Any]:
        """Check the installation: database, schema, registered tasks and model profiles."""
        checks: dict[str, Any] = {}
        try:
            with self.store.connection() as conn:
                version = conn.execute(b"SHOW server_version").fetchone()
                conn.rollback()
            checks["database"] = {"ok": True, "server_version": version["server_version"]
                                  if version else None}
        except Exception as exc:  # report, never raise: doctor must always answer
            checks["database"] = {"ok": False, "error": type(exc).__name__, "detail": str(exc)}
        try:
            with self.store.read() as q:
                q.run_ids()
            checks["schema"] = {"ok": True, "schema": self.settings.db_schema}
        except Exception as exc:
            checks["schema"] = {"ok": False, "hint": "run `continuum db-init`",
                                "error": type(exc).__name__}
        tasks = self.registry.task_ids()
        checks["tasks"] = {"ok": bool(tasks), "count": len(tasks),
                           "invalid_files": {str(path): why for path, why
                                             in self.registry.errors.items()},
                           "config_dir": str(self.settings.config_dir.resolve())}
        models: dict[str, Any] = {}
        try:
            router = load_live_router(self.settings.config_dir / "models.yaml")
            for alias in router.aliases():
                try:
                    router.get(alias).profile.check_deployable()
                    models[alias] = {"deployable": True}
                except Exception as exc:
                    models[alias] = {"deployable": False, "reason": str(exc)}
        except Exception as exc:
            models["error"] = str(exc)
        checks["live_models"] = models
        checks["ok"] = all(checks[k]["ok"] for k in ("database", "schema", "tasks"))
        return checks

    def trace(self, run_id: str) -> list[dict[str, Any]]:
        """Read-only JSON trace export (events with redacted blobs inlined)."""
        return self._runtime_for_run(run_id, read_only=True).trace_export(run_id)

    def runs(self) -> list[str]:
        with self.store.read() as q:
            return q.run_ids()
