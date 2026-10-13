"""Typed runtime errors. Each carries a stable machine-readable ``code``."""

from __future__ import annotations


class ContinuumError(Exception):
    code = "continuum_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class SpecValidationError(ContinuumError):
    code = "invalid_spec"


class ManifestError(ContinuumError):
    code = "invalid_manifest"


class NotFoundError(ContinuumError):
    code = "not_found"


class AlreadyOwnedError(ContinuumError):
    """Another live controller holds the run lease."""

    code = "already_owned"


class StaleOwnerError(ContinuumError):
    """The caller's (owner, epoch) is no longer the active unexpired lease."""

    code = "stale_owner"


class InvalidTransitionError(ContinuumError):
    code = "invalid_transition"


class IntentConflictError(ContinuumError):
    """Same operation identity proposed with different immutable arguments."""

    code = "intent_conflict"


class PathContainmentError(ContinuumError):
    code = "path_outside_workspace"


class PacketTooLargeError(ContinuumError):
    code = "unsupported_capability"


class UnsupportedCapabilityError(ContinuumError):
    code = "unsupported_capability"


class BudgetExceededError(ContinuumError):
    code = "budget_exceeded"
