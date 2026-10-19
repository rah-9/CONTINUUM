# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Concise Markdown report from raw Continuum Lab results."""

from __future__ import annotations

from collections import Counter
from typing import Any

from continuum_lab.metrics import Proportion, Row, proportion, summarize


def _diff(d: tuple[float, float, float, int] | None) -> str:
    if d is None:
        return "n/a (no matched pairs)"
    mean, lo, hi, n = d
    return f"{mean:+.3f} [{lo:+.3f}, {hi:+.3f}] over {n} matched cells"


def render(rows: list[Row], meta: dict[str, Any]) -> str:
    s = summarize(rows)
    configs = [c for c in ("B0", "C1") if c in s]
    lines = [
        "# Continuum Lab report", "",
        f"- Suite: `{meta.get('suite')}`; adapter: **{meta.get('adapter')}**; "
        f"seeds: {meta.get('seeds')}; generated: {meta.get('generated_at')}",
        f"- Cells run: {len(rows)} ({', '.join(f'{c}={s[c]['runs']}' for c in configs)})",
        "- Deterministic fake-model results test modeled safety properties only; they are",
        "  not evidence about live provider behaviour. Two repeats are exploratory.", "",
        "## Coverage", "",
        "| fault | modes | tasks | cells |", "|---|---|---|---|",
    ]
    by_fault: Counter[str] = Counter(r["fault"] for r in rows)
    for fault in sorted(by_fault):
        fr = [r for r in rows if r["fault"] == fault]
        lines.append(f"| {fault} | {', '.join(sorted({r['mode'] for r in fr}))} | "
                     f"{len({r['task'] for r in fr})} | {by_fault[fault]} |")
    lines += ["", "## B0 vs C1", "", "| metric | " + " | ".join(configs) + " |",
              "|---|" + "---|" * len(configs)]

    def row(label: str, key: str) -> None:
        vals = []
        for c in configs:
            v = s[c][key]
            vals.append(v.fmt() if isinstance(v, Proportion) else str(v))
        lines.append(f"| {label} | " + " | ".join(vals) + " |")

    for label, key in [
        ("verified completion", "verified_completion"),
        ("clean completion (no duplicate/unintended effect)", "clean_completion"),
        ("runs with a committed duplicate", "runs_with_duplicate"),
        ("committed duplicate effects", "committed_duplicates"),
        ("intended write effects", "intended_writes"),
        ("extra physical write requests", "extra_write_requests"),
        ("blocked repeated proposals", "blocked_repeat_proposals"),
        ("human escalations", "human_escalations"),
        ("runs left unresolved (paused)", "unresolved_runs"),
        ("unknown-resolution coverage", "unknown_resolution"),
        ("false completion", "false_completion"),
        ("policy bypass (unintended targets)", "policy_bypass"),
        ("preserved progress", "preserved_progress"),
        ("model calls", "model_calls"), ("tokens", "tokens"),
        ("estimated cost (USD)", "cost_usd"), ("runs with unpriced usage", "unpriced_runs"),
    ]:
        row(label, key)
    lines += ["", "Matched differences (C1 - B0, paired bootstrap 95%):", "",
              f"- verified completion: {_diff(s['paired_completion_diff'])}",
              f"- clean completion: {_diff(s['paired_clean_completion_diff'])}",
              f"- committed duplicates per cell: {_diff(s['paired_duplicate_diff'])}", "",
              "## Per fault", "",
              "| fault | config | completion | clean completion | duplicates | paused |",
              "|---|---|---|---|---|---|"]
    for fault in sorted(by_fault):
        for c in configs:
            fr = [r for r in rows if r["fault"] == fault and r["config"] == c]
            comp = proportion(fr, lambda r: r["verified_completion"])
            clean = proportion(fr, lambda r: r["clean_completion"])
            lines.append(f"| {fault} | {c} | {comp.fmt()} | {clean.fmt()} | "
                         f"{sum(r['committed_duplicates'] for r in fr)} | "
                         f"{sum(1 for r in fr if r['unresolved'])} |")
    failed = [r for r in rows if not r["verified_completion"]]
    lines += ["", f"## Non-completed cells ({len(failed)})", ""]
    for r in failed[:40]:
        lines.append(f"- {r['config']} {r['task']} fault={r['fault']} mode={r['mode']} "
                     f"seed={r['seed']}: {r['status']}")
    lines += ["", "## Limitations", "",
              "- B0 and C1 use different context formats and recovery algorithms; task",
              "  instructions, tools, permissions, budgets, services and scorer are held equal.",
              "- B0 sends no idempotency keys (no stable action identity) and may check before",
              "  repeating writes; C1 gets no privileged API the baseline lacks.",
              "- A C1 pause for human review is reported as non-completion, not success.",
              "- Results cover only the modeled connector contracts in the mock services.",
              "", "Reproduce: `" + str(meta.get("command")) + "`", ""]
    return "\n".join(lines)
