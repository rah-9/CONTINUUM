"""Metric definitions and uncertainty (docs/07 section 7).

Proportions use Wilson score intervals; matched C1-B0 differences use a
seeded paired bootstrap. ``0/0`` is reported as not applicable, never 100%.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

Row = dict[str, Any]


@dataclass(frozen=True)
class Proportion:
    successes: int
    total: int

    @property
    def rate(self) -> float | None:
        return None if self.total == 0 else self.successes / self.total

    def wilson(self, z: float = 1.96) -> tuple[float, float] | None:
        if self.total == 0:
            return None
        n, p = self.total, self.successes / self.total
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
        return max(0.0, centre - half), min(1.0, centre + half)

    def fmt(self) -> str:
        if self.total == 0:
            return "n/a (0/0)"
        lo, hi = self.wilson() or (0.0, 0.0)
        return f"{self.successes}/{self.total} = {self.rate:.2f} [{lo:.2f}, {hi:.2f}]"


def proportion(rows: Iterable[Row], pred: Callable[[Row], bool],
               where: Callable[[Row], bool] = lambda r: True) -> Proportion:
    sel = [r for r in rows if where(r)]
    return Proportion(sum(1 for r in sel if pred(r)), len(sel))


def paired_bootstrap(rows: list[Row], value: Callable[[Row], float], *, a: str = "C1",
                     b: str = "B0", iterations: int = 2000, seed: int = 7
                     ) -> tuple[float, float, float, int] | None:
    """Mean of (a - b) over matched cells with a 95% bootstrap interval."""
    by_cell: dict[tuple[Any, ...], dict[str, Row]] = defaultdict(dict)
    for r in rows:
        by_cell[(r["task"], r["fault"], r["mode"], r["seed"])][r["config"]] = r
    diffs = [value(c[a]) - value(c[b]) for c in by_cell.values() if a in c and b in c]
    if not diffs:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs)
                   for _ in range(iterations))
    return (sum(diffs) / len(diffs), means[int(0.025 * iterations)],
            means[int(0.975 * iterations) - 1], len(diffs))


def summarize(rows: list[Row]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for config in sorted({r["config"] for r in rows}):
        rs = [r for r in rows if r["config"] == config]
        writes = [r for r in rs if r["intended_writes"] > 0]
        ambiguous = [r for r in rs if r["ambiguous_actions"] > 0]
        out[config] = {
            "runs": len(rs),
            "verified_completion": proportion(rs, lambda r: r["verified_completion"]),
            "clean_completion": proportion(rs, lambda r: r["clean_completion"]),
            "runs_with_duplicate": proportion(writes, lambda r: r["committed_duplicates"] > 0),
            "committed_duplicates": sum(r["committed_duplicates"] for r in rs),
            "intended_writes": sum(r["intended_writes"] for r in rs),
            "extra_write_requests": sum(r["extra_write_requests"] for r in rs),
            "blocked_repeat_proposals": sum(r["blocked_repeat_proposals"] for r in rs),
            "human_escalations": sum(r["human_escalations"] for r in rs),
            "unresolved_runs": sum(1 for r in rs if r["unresolved"]),
            "unknown_resolution": proportion(ambiguous, lambda r: r["ambiguous_resolved"]),
            "false_completion": proportion(rs, lambda r: r["false_completion"],
                                           where=lambda r: r["reported_complete"]),
            "policy_bypass": sum(r["policy_bypass"] for r in rs),
            "preserved_progress": Proportion(sum(r["preserved_num"] for r in rs),
                                             sum(r["preserved_den"] for r in rs)),
            "model_calls": sum(r["model_calls"] for r in rs),
            "tokens": sum(r["tokens"] for r in rs),
            "cost_usd": round(sum(r["cost_usd"] for r in rs), 6),
            "unpriced_runs": sum(1 for r in rs if r["unpriced"]),
        }
    out["paired_completion_diff"] = paired_bootstrap(
        rows, lambda r: float(r["verified_completion"]))
    out["paired_clean_completion_diff"] = paired_bootstrap(
        rows, lambda r: float(r["clean_completion"]))
    out["paired_duplicate_diff"] = paired_bootstrap(
        rows, lambda r: float(r["committed_duplicates"]))
    return out
