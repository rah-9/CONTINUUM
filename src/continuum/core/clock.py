# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Injectable clocks. Production uses wall/monotonic time; tests use FakeClock."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC wall time used for event ``occurred_at``."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds used for active-time budget increments."""
        ...

    def sleep(self, seconds: float) -> None: ...

    def lease_time(self) -> float | None:
        """Unix seconds for lease checks, or ``None`` to use database time."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def lease_time(self) -> float | None:
        return None


class FakeClock:
    """Deterministic clock. ``sleep`` advances time instead of blocking."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    def lease_time(self) -> float | None:
        return self._now.timestamp()
