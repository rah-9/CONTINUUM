"""Aggregate hard budgets and active-time accounting (R9, docs/03 time budget).

Budgets are run-wide: a model handoff never resets them. Active time counts
only while this process is driving the run (monotonic, capped increments), so
manual pauses and process downtime are excluded.
"""

from __future__ import annotations

from decimal import Decimal

from continuum.core.clock import Clock
from continuum.core.events import BudgetExceeded, RunActiveTimeRecorded, RunFailed, make_event
from continuum.core.journal import RunJournal
from continuum.core.state import TERMINAL_RUN_STATUSES
from continuum.core.store import Store
from continuum.tools.gateway import RunContext


class BudgetGuard:
    def __init__(self, *, store: Store, journal: RunJournal, clock: Clock,
                 increment_cap_seconds: float) -> None:
        self.store = store
        self.journal = journal
        self.clock = clock
        self.increment_cap_seconds = increment_cap_seconds
        self._tick: float | None = None

    def start_clock(self) -> None:
        """Start counting for a new operation (start/resume): recovery work counts too."""
        self._tick = self.clock.monotonic()

    def ensure_clock(self) -> None:
        if self._tick is None:
            self.start_clock()

    def record_active_time(self, ctx: RunContext) -> None:
        now = self.clock.monotonic()
        last = self._tick if self._tick is not None else now
        elapsed = max(0.0, min(now - last, self.increment_cap_seconds))
        self._tick = now
        ms = int(elapsed * 1000)
        if ms > 0:
            self.journal.append(ctx, RunActiveTimeRecorded(elapsed_ms=ms))

    def exhausted(self, ctx: RunContext, *, include_recoveries: bool = False) -> bool:
        """Fail the run if a hard budget is exhausted. True if the run must stop."""
        with self.store.read() as q:
            usage = q.usage(ctx.run_id)
            status = q.run(ctx.run_id).status
        if status in TERMINAL_RUN_STATUSES:
            return True
        b = ctx.spec.budgets
        checks: list[tuple[str, Decimal | int, Decimal | int, bool]] = [
            ("max_model_calls", b.max_model_calls, usage.model_calls,
             usage.model_calls >= b.max_model_calls),
            ("max_tokens", b.max_tokens, usage.tokens_reported,
             usage.tokens_reported >= b.max_tokens),
            ("max_cost_usd", b.max_cost_usd, usage.cost_known_usd,
             usage.cost_known_usd >= b.max_cost_usd),
            ("max_active_seconds", b.max_active_seconds, usage.active_elapsed_ms // 1000,
             usage.active_elapsed_ms >= b.max_active_seconds * 1000),
        ]
        if include_recoveries:
            checks.append(("max_recoveries", b.max_recoveries, usage.recoveries,
                           usage.recoveries > b.max_recoveries))
        for name, limit, observed, exceeded in checks:
            if exceeded:
                self.journal.append_events(ctx, [
                    make_event(BudgetExceeded(budget=name, limit=str(limit),
                                              observed=str(observed)), actor="runtime"),
                    make_event(RunFailed(failure_code="budget_exceeded",
                                         reason=f"{name} exhausted"), actor="runtime")])
                return True
        return False
