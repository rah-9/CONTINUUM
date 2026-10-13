"""Verifier contract and registry (docs/02 section 7, R7).

A verifier receives a read-only view of the task, workspace artifacts and the
ledger, and may perform service observations only through the gateway (as a
``verifier`` caller). It returns a status plus the inputs it examined, from
which the runtime computes the milestone ``input_fingerprint``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from continuum.core.errors import SpecValidationError
from continuum.core.evidence import Workspace
from continuum.core.state import TaskSpec, VerifierResult
from continuum.core.store import ActionRow


class ObservationError(Exception):
    """A verifier/reconciler read through the gateway did not succeed."""


@dataclass
class VerifierOutcome:
    status: VerifierResult
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    input_artifacts: dict[str, str] = field(default_factory=dict)   # path -> sha256
    external_versions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerifierContext:
    run_id: str
    milestone_id: str
    spec: TaskSpec
    workspace: Workspace
    confirmed_actions: list[ActionRow]
    dependency_evidence: dict[str, Any]
    observe: Callable[[str, dict[str, Any]], dict[str, Any]]

    def artifact(self, path: str) -> tuple[bytes, str] | None:
        """Bytes and sha256 of a workspace file, or None if absent."""
        sha = self.workspace.hash(path)
        if sha is None:
            return None
        return self.workspace.read_bytes(path), sha


class Verifier(ABC):
    verifier_id: str
    version: str

    @abstractmethod
    def verify(self, ctx: VerifierContext) -> VerifierOutcome: ...


class VerifierRegistry:
    def __init__(self, verifiers: list[Verifier] | None = None) -> None:
        self._by_id: dict[str, Verifier] = {}
        for v in verifiers or []:
            self.register(v)

    def register(self, verifier: Verifier) -> None:
        if verifier.verifier_id in self._by_id:
            raise SpecValidationError(f"verifier {verifier.verifier_id} already registered")
        self._by_id[verifier.verifier_id] = verifier

    def get(self, verifier_id: str) -> Verifier | None:
        return self._by_id.get(verifier_id)

    def validate_spec(self, spec: TaskSpec) -> None:
        for m in spec.milestones:
            v = self._by_id.get(m.verifier_id)
            if v is None:
                raise SpecValidationError(f"milestone {m.id}: unknown verifier {m.verifier_id}")
            if v.version != m.verifier_version:
                raise SpecValidationError(
                    f"milestone {m.id}: verifier {m.verifier_id} is version {v.version}, "
                    f"spec requires {m.verifier_version}")
