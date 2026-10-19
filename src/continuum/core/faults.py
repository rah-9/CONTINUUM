# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Named crash boundaries used by deterministic fault injection.

Production code calls :meth:`FaultInjector.checkpoint` at each boundary listed
in ``docs/02-ARCHITECTURE-AND-FLOWS.md`` section 9. The default injector does
nothing; Continuum Lab supplies one that raises :class:`SimulatedCrash`.
"""

from __future__ import annotations

from typing import Any, Protocol

BOUNDARIES: frozenset[str] = frozenset(
    {
        "before_event_commit",
        "after_event_commit",
        "after_lease_acquired",
        "after_action_created",
        "after_attempt_start_commit",
        "before_dispatch",
        "after_response_receipt",
        "after_action_outcome_commit",
        "after_artifact_write",
        "after_milestone_verified_commit",
        "after_model_switch_commit",
    }
)


class SimulatedCrash(BaseException):
    """Abrupt process death. BaseException so ordinary handlers cannot swallow it."""


class FaultInjector(Protocol):
    def checkpoint(self, boundary: str, **context: Any) -> None: ...


class NoFaults:
    def checkpoint(self, boundary: str, **context: Any) -> None:
        return None
