"""Logical action identity and state machine (docs/04 sections 2-3).

A logical action is one intended business operation, identified by a trusted
``operation_key`` derived from task/manifest fields — never from a provider's
call ID or a model-chosen index. Physical dispatches are attempts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from continuum.core.canonical import sha256_hex
from continuum.core.errors import InvalidTransitionError
from continuum.core.events import ActionAttemptAmbiguous, make_event
from continuum.core.state import ActionStatus as S
from continuum.core.store import RunTx

ACTION_TRANSITIONS: dict[S, frozenset[S]] = {
    S.PROPOSED: frozenset({S.REJECTED, S.AWAITING_APPROVAL, S.AUTHORIZED}),
    S.AWAITING_APPROVAL: frozenset({S.REJECTED, S.AUTHORIZED}),
    S.AUTHORIZED: frozenset({S.IN_FLIGHT}),
    S.IN_FLIGHT: frozenset({S.CONFIRMED, S.FAILED_CLEAN, S.UNKNOWN}),
    S.FAILED_CLEAN: frozenset({S.AUTHORIZED}),
    # unknown -> authorized ONLY with proven-safe absence evidence;
    # unknown -> in_flight ONLY for a documented idempotent same-key retry.
    S.UNKNOWN: frozenset({S.CONFIRMED, S.AUTHORIZED, S.IN_FLIGHT, S.NEEDS_HUMAN}),
    S.NEEDS_HUMAN: frozenset({S.CONFIRMED, S.AUTHORIZED, S.SKIPPED}),
    S.CONFIRMED: frozenset(),
    S.REJECTED: frozenset(),
    S.SKIPPED: frozenset(),
}


def assert_transition(current: S, target: S) -> None:
    if target not in ACTION_TRANSITIONS[current]:
        raise InvalidTransitionError(f"action transition {current} -> {target} is not allowed")


def operation_key(
    *,
    tool_name: str,
    milestone_id: str | None,
    milestone_scoped: bool,
    fields: list[str],
    args: Mapping[str, Any],
    normalize: Callable[[str, Any], str],
) -> str:
    """``<milestone>:<tool>:<normalized field values>`` from trusted manifest rules."""
    if milestone_scoped and milestone_id is None:
        raise ValueError(f"{tool_name} is milestone-scoped but no milestone is in focus")
    parts = [normalize(f, args[f]) for f in fields]
    scope = milestone_id if milestone_scoped else "run"
    return f"{scope}:{tool_name}:" + "|".join(parts)


def idempotency_key(run_id: str, action_id: str) -> str:
    """Stable across attempts and providers; persisted at authorization."""
    return sha256_hex(f"continuum:v1|{run_id}|{action_id}")


def mark_interrupted_attempts(tx: RunTx, *, actor: str) -> list[str]:
    """Classify every ``started`` attempt without an outcome as ambiguous (-> unknown).

    Applied at takeover even if the request possibly never left the process.
    """
    marked: list[str] = []
    for attempt in tx.started_attempts(tx.run_id):
        action = tx.action(attempt.action_id)
        assert_transition(action.status, S.UNKNOWN)
        tx.append(make_event(ActionAttemptAmbiguous(
            action_id=attempt.action_id, attempt_no=attempt.attempt_no,
            error_code="interrupted",
            message="attempt started but no outcome was recorded before takeover"),
            actor=actor))
        marked.append(f"{attempt.action_id}#{attempt.attempt_no}")
    return marked
