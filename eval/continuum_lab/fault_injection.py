# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Deterministic fault injection at named boundaries (docs/07 section 4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from continuum.core.faults import BOUNDARIES, SimulatedCrash

Predicate = Callable[[dict[str, Any]], bool]


@dataclass
class CrashRule:
    boundary: str
    when: Predicate = field(default=lambda ctx: True)
    remaining: int = 1

    def __post_init__(self) -> None:
        if self.boundary not in BOUNDARIES:
            raise ValueError(f"unknown boundary {self.boundary}")


class CrashInjector:
    """Raises :class:`SimulatedCrash` the first N times a matching boundary is hit."""

    def __init__(self, *rules: CrashRule) -> None:
        self.rules = list(rules)
        self.fired: list[tuple[str, dict[str, Any]]] = []

    def checkpoint(self, boundary: str, **context: Any) -> None:
        for rule in self.rules:
            if rule.boundary == boundary and rule.remaining > 0 and rule.when(context):
                rule.remaining -= 1
                self.fired.append((boundary, context))
                raise SimulatedCrash(f"injected crash at {boundary}")


def for_tool(tool: str) -> Predicate:
    return lambda ctx: ctx.get("tool") == tool


def for_milestone(milestone_id: str) -> Predicate:
    return lambda ctx: ctx.get("milestone_id") == milestone_id
