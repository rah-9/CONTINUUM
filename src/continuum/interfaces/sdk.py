"""Python SDK: ``start``, ``resume``, ``status``, ``resolve_action`` (R10)."""

from __future__ import annotations

from typing import Any

from continuum.core.clock import SystemClock
from continuum.core.ids import RandomIds
from continuum.core.runtime import Decision, RunResult, Runtime, RuntimeConfig
from continuum.core.store import Store
from continuum.interfaces.registry import TaskBinding, TaskRegistry, load_live_router
from continuum.interfaces.settings import Settings


class Continuum:
    """Local SDK over a PostgreSQL store and pre-registered tasks."""

    def __init__(self, settings: Settings | None = None, *, store: Store | None = None,
                 registry: TaskRegistry | None = None) -> None:
        self.settings = settings or Settings()
        self.clock = SystemClock()
        self.ids = RandomIds()
        self.store = store or Store(self.settings.database_url, schema=self.settings.db_schema,
                                    clock=self.clock, ids=self.ids)
        self.registry = registry or TaskRegistry(self.settings.config_dir, self.settings.home)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> Continuum:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def init_db(self) -> None:
        self.store.migrate()

    def tasks(self) -> list[dict[str, Any]]:
        return self.registry.describe()

    def _runtime(self, binding: TaskBinding) -> Runtime:
        models = binding.models or load_live_router(self.settings.config_dir / "models.yaml")
        config = RuntimeConfig(workspace_root=self.settings.home / "workspaces",
                               input_root=binding.input_root, owner_id=self.settings.owner_id,
                               lease_ttl_seconds=self.settings.lease_ttl_seconds,
                               heartbeat_interval_seconds=max(
                                   self.settings.lease_ttl_seconds / 3, 1.0))
        return Runtime(store=self.store, tools=binding.tools, verifiers=binding.verifiers,
                       models=models, config=config, clock=self.clock, ids=self.ids)

    def _runtime_for_run(self, run_id: str) -> Runtime:
        with self.store.read() as q:
            task_id = q.run(run_id).task_id
        return self._runtime(self.registry.binding(task_id))

    def start(self, task_id: str, *, model_alias: str | None = None) -> RunResult:
        binding = self.registry.binding(task_id)
        return self._runtime(binding).start(binding.spec, model_alias=model_alias)

    def resume(self, run_id: str, *, model_alias: str | None = None) -> RunResult:
        return self._runtime_for_run(run_id).resume(run_id, model_alias=model_alias)

    def status(self, run_id: str) -> dict[str, Any]:
        return self._runtime_for_run(run_id).status(run_id)

    def resolve_action(self, run_id: str, action_id: str, decision: Decision, *, actor: str,
                       reason: str, evidence_ref: str | None = None) -> RunResult:
        return self._runtime_for_run(run_id).resolve_action(
            run_id, action_id, decision, actor=actor, reason=reason, evidence_ref=evidence_ref)

    def cancel(self, run_id: str, *, actor: str, reason: str) -> RunResult:
        return self._runtime_for_run(run_id).cancel(run_id, actor=actor, reason=reason)

    def inspect(self, run_id: str) -> dict[str, Any]:
        return self._runtime_for_run(run_id).inspect(run_id)

    def pending_decisions(self, run_id: str) -> list[dict[str, Any]]:
        """Read-only: decisions only a human may make, with the command to make them."""
        return self._runtime_for_run(run_id).pending_decisions(run_id)

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
        return self._runtime_for_run(run_id).trace_export(run_id)

    def runs(self) -> list[str]:
        with self.store.read() as q:
            return q.run_ids()
