"""Fenced event appends shared by the controller's components.

Every component that changes durable state goes through one ``RunJournal``,
so each append is fenced by the caller's lease (docs/03 s4) and status
changes never override a terminal state.
"""

from __future__ import annotations

from collections.abc import Sequence

from continuum.core.events import NewEvent, Payload, RunStatusChanged, make_event
from continuum.core.state import TERMINAL_RUN_STATUSES, RunStatus
from continuum.core.store import Store
from continuum.tools.gateway import RunContext


class RunStopped(Exception):
    """The step loop must stop (terminal status or budget exhaustion already recorded)."""


class RunJournal:
    def __init__(self, store: Store) -> None:
        self.store = store

    def append(self, ctx: RunContext, payload: Payload, *, actor: str = "runtime") -> None:
        self.append_events(ctx, [make_event(payload, actor=actor)])

    def append_events(self, ctx: RunContext, events: Sequence[NewEvent]) -> None:
        self.store.append(ctx.run_id, ctx.lease.fence, events)

    def set_status(self, ctx: RunContext, status: RunStatus, reason: str) -> None:
        with self.store.run_tx(ctx.run_id, ctx.lease.fence) as tx:
            current = tx.run(ctx.run_id).status
            if current != status and current not in TERMINAL_RUN_STATUSES:
                tx.append(make_event(RunStatusChanged(from_status=current.value,
                                                      to_status=status.value, reason=reason),
                                     actor="runtime"))

    def status(self, run_id: str) -> RunStatus:
        with self.store.read() as q:
            return q.run(run_id).status
