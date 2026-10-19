# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Trusted tool manifests and the trusted-tool contract (docs/04 section 5).

Tools are explicitly registered Python adapters. Their manifests are trusted
operator configuration; remote annotations are never accepted as manifests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from continuum.core.errors import ManifestError
from continuum.core.evidence import Workspace


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OperationIdentity(_M):
    milestone_scoped: bool = True
    fields: tuple[str, ...] = ()


class Idempotency(_M):
    supported: bool = False
    key_location: Literal["header", "argument", "none"] = "none"
    # None means no expiry (e.g. deterministic local writes). Only set values
    # that are tested/documented for the connector.
    retention_seconds: int | None = None


class Reconciliation(_M):
    method: Literal["none", "authoritative_query", "opaque"] = "opaque"
    check_tool: str | None = None
    check_args: tuple[str, ...] = ()        # action args passed to check tool, same names
    positive_proof: tuple[str, ...] = ()    # action args that must equal check observation
    negative_proof_is_safe: bool = False    # only with a tested race-safe absence contract


class Permissions(_M):
    requires_approval: bool = False
    allowed_path_prefix: str | None = None
    path_fields: tuple[str, ...] = ()


class ToolManifest(_M):
    name: str
    version: str
    description: str = ""
    effect: Literal["read", "write"]
    args_schema: dict[str, Any]
    operation_identity: OperationIdentity = Field(default_factory=OperationIdentity)
    idempotency: Idempotency = Field(default_factory=Idempotency)
    reconciliation: Reconciliation = Field(default_factory=Reconciliation)
    permissions: Permissions = Field(default_factory=Permissions)
    timeout_seconds: float = Field(default=20, gt=0)
    max_attempts: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> ToolManifest:
        try:
            jsonschema.Draft202012Validator.check_schema(self.args_schema)
        except jsonschema.SchemaError as exc:
            raise ValueError(f"invalid args_schema for {self.name}: {exc.message}") from exc
        if self.args_schema.get("additionalProperties", True) is not False:
            raise ValueError(f"{self.name}: args_schema must set additionalProperties: false")
        props = set(self.args_schema.get("properties", {}))
        if self.effect == "write":
            if not self.operation_identity.fields:
                raise ValueError(f"{self.name}: write tools need operation_identity.fields")
            if self.reconciliation.method == "authoritative_query" and \
                    not self.reconciliation.check_tool:
                raise ValueError(f"{self.name}: authoritative_query requires check_tool")
        if self.reconciliation.negative_proof_is_safe and \
                self.reconciliation.method != "authoritative_query":
            raise ValueError(f"{self.name}: negative proof requires an authoritative query")
        missing = set(self.operation_identity.fields) - props
        if missing:
            raise ValueError(f"{self.name}: identity fields not in schema: {sorted(missing)}")
        return self

    def validate_args(self, args: dict[str, Any]) -> None:
        validator = jsonschema.Draft202012Validator(self.args_schema)
        errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))
        if errors:
            raise jsonschema.ValidationError(errors[0].message)

    def provider_schema(self) -> dict[str, Any]:
        """OpenAI-style function schema handed to model adapters."""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.args_schema}}


def load_manifest(path: Path) -> ToolManifest:
    try:
        return ToolManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValueError as exc:
        raise ManifestError(f"{path}: {exc}") from exc


class ToolCleanFailure(Exception):
    """The target definitively rejected the call before any effect took place."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class ToolAmbiguousFailure(Exception):
    """The call may or may not have committed (timeout, dropped response, ...)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class BindContext:
    run_id: str
    milestone_id: str | None
    workspace: Workspace


@dataclass(frozen=True)
class ToolContext:
    run_id: str
    action_id: str
    attempt_no: int
    milestone_id: str | None
    idempotency_key: str | None
    workspace: Workspace
    timeout_seconds: float


@dataclass
class ToolResult:
    data: Any
    artifacts: list[str] = field(default_factory=list)  # workspace-relative paths written


class TrustedTool(ABC):
    """A registered, trusted adapter. Only the gateway may call :meth:`invoke`."""

    manifest: ToolManifest

    def bind(self, args: dict[str, Any], ctx: BindContext) -> dict[str, Any]:
        """Return the immutable execution arguments (e.g. add content hashes).

        Raise :class:`ToolCleanFailure` if the proposal cannot be bound.
        """
        return dict(args)

    def normalize_identity(self, field_name: str, value: Any) -> str:
        return str(value).strip()

    @abstractmethod
    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


class ToolRegistry:
    def __init__(self, tools: list[TrustedTool] | None = None) -> None:
        self._tools: dict[str, TrustedTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: TrustedTool) -> None:
        name = tool.manifest.name
        if name in self._tools:
            raise ManifestError(f"tool {name} already registered")
        self._tools[name] = tool
        check = tool.manifest.reconciliation.check_tool
        if check and check in self._tools and self._tools[check].manifest.effect != "read":
            raise ManifestError(f"check tool {check} must be read-only")

    def get(self, name: str) -> TrustedTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def validate_references(self) -> None:
        for tool in self._tools.values():
            check = tool.manifest.reconciliation.check_tool
            if check:
                target = self._tools.get(check)
                if target is None:
                    raise ManifestError(f"{tool.manifest.name}: check tool {check} missing")
                if target.manifest.effect != "read":
                    raise ManifestError(f"check tool {check} must be read-only")
