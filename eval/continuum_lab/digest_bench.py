# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Compression benchmark: does a fixed-size memory of a long coding session keep the
facts a successor needs, of every kind, not only what was said in the chat?

Generates coding sessions of N turns as the journal lines every memory method reads
(``working_memory.history_lines``: user and agent messages, tool calls with the end of
their result, checks, unknown outcomes) and plants facts of thirteen kinds at random
turns, among realistic noise: narration, announced next steps, read-only tool calls,
long logs, pasted diffs and JSON, chit-chat, repeated statements.

A fact counts as kept when its key value appears in the memory. For a changed value
("use port 8081 instead of 8080"), only the new value counts. No model, no database:
``python -m continuum_lab.memory_bench`` covers the user's words through the real
service; this one covers the whole session.

Run: ``python -m continuum_lab.digest_bench [--seeds 100-119] [--budget 16000]``.
Seeds 1-19 were used while developing the digest; report on 100 and up (held out).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

KINDS = ("requirement", "decision", "measurement", "file_changed", "commit", "pull_request",
         "error_fixed", "test_summary", "root_cause", "pasted_error", "pasted_config",
         "changed_value", "unknown_outcome")
# the current state of things that changed many times: only the latest counts
STATE_KINDS = ("latest_result", "pr_merged")

WORDS = (
    "invoice", "ledger", "tenant", "shard", "export", "importer", "quota", "webhook",
    "billing", "cursor", "batch", "session", "token", "cache", "report", "schema", "migration",
    "region", "locale", "backoff", "retry", "queue", "worker", "scheduler", "parser", "mapper",
    "adapter", "gateway",
)

NARRATION = [
    "I'll take a look at how this is wired up before changing anything.",
    "Let me check the surrounding code first so I understand the flow.",
    "That makes sense, I'll continue with the next part of the task now.",
    "Good, that part looks fine, so I'll move on to the remaining pieces.",
    "I looked at several files and thought about the overall approach for a while.",
    "This should be straightforward, I'll make the change and then verify it.",
    "Okay, I understand the structure better now and can proceed with the work.",
]
INTENTIONS = ["Now the tests:", "Next, the docs:", "Let me run the suite again:",
              "Now I'll update the config:", "Checking the CI status:"]
CHATTER = ["ok continue", "sounds good, go ahead", "yes please continue with that",
           "thanks, keep going", "alright, carry on"]


@dataclass
class Fact:
    kind: str
    keys: list[str]                 # every key must appear in the memory
    turn: int


@dataclass
class Session:
    lines: list[tuple[int, str]] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    seq: int = 1

    def add(self, text: str) -> None:
        self.lines.append((self.seq, f"#{self.seq} {text}"))
        self.seq += 1


def _w(r: random.Random) -> str:
    return r.choice(WORDS)


def _ident(r: random.Random) -> str:
    return f"{_w(r)}_{_w(r)}_{r.randint(10, 99)}"


def _path(r: random.Random) -> str:
    return f"src/{_w(r)}/{_w(r)}_{r.randint(10, 99)}.py"


def _noise_turn(s: Session, r: random.Random, turn_text: str) -> None:
    s.add(f"USER: {turn_text}")
    for _ in range(r.randint(2, 6)):
        k = r.random()
        if k < 0.35:
            s.add(f"ASSISTANT: {r.choice(NARRATION)}")
        elif k < 0.5:
            s.add(f"ASSISTANT: {r.choice(INTENTIONS)}")
        elif k < 0.75:
            p = _path(r)
            s.add(f'TOOL Read [ok]: {{"file_path": "{p}"}} => '
                  + " ".join(f"def {_w(r)}_{i}(self): return None" for i in range(8))[-300:])
        elif k < 0.9:
            log = " ".join(f"INFO {_w(r)} step {i} ok" for i in range(60))
            s.add(f"TOOL Bash [ok]: python -m {_w(r)}.cli --verbose => {log[-300:]}")
        else:
            s.add('TOOL Grep [ok]: {"pattern": "' + _w(r) + '"} => ' + " ".join(
                _path(r) for _ in range(12))[-300:])
    s.add(f"ASSISTANT: {r.choice(NARRATION)} {r.choice(NARRATION)}")


def _plant(kind: str, s: Session, r: random.Random, turn: int) -> None:
    """Add one turn that carries one fact of ``kind``, wrapped in realistic noise."""
    pad = " ".join(r.choice(CHATTER) for _ in range(r.randint(0, 3)))
    if kind == "requirement":
        n = r.randint(11, 97)
        key = f"{_ident(r)}"
        s.add(f"USER: okay so like {pad} one thing, the `{key}` limit has to stay at {n} "
              f"per minute, don't change that, and {pad} continue with the rest")
        s.facts.append(Fact(kind, [key, str(n)], turn))
        s.add(f"ASSISTANT: {r.choice(NARRATION)}")
    elif kind == "decision":
        q = f"Which storage should the {_w(r)} {_w(r)} use?"
        choice = r.choice(["Object storage (Recommended)", "Local disk", "PostgreSQL table"])
        s.add(f"USER: {r.choice(CHATTER)}")
        s.add('TOOL AskUserQuestion [ok]: {"questions": [{"question": "' + q + '"}]} => '
              f'User has answered your questions: "{q}"="{choice}". Read the answers carefully.')
        s.facts.append(Fact(kind, [choice.split(" (")[0]], turn))
    elif kind == "measurement":
        a, b = r.randint(400, 990), r.randint(20, 300)
        what = f"{_w(r)} {_w(r)}"
        s.add(f"USER: {r.choice(CHATTER)}, how fast is it now?")
        s.add(f"ASSISTANT: {r.choice(NARRATION)}")
        s.add(f"TOOL Bash [ok]: python bench.py => run 1 {a} ms run 2 {b} ms")
        s.add(f"ASSISTANT: {r.choice(NARRATION)} The {what} p95 dropped from {a} ms to {b} ms "
              f"after the change. {r.choice(NARRATION)}")
        s.facts.append(Fact(kind, [f"{a} ms", f"{b} ms"], turn))
    elif kind == "file_changed":
        p = _path(r)
        s.add(f"USER: please fix the {_w(r)} handling")
        s.add(f"ASSISTANT: {r.choice(INTENTIONS)}")
        s.add(f'TOOL Edit [ok]: {{"file_path": "{p}", "old_string": "x = 1", '
              f'"new_string": "x = 2"}} => The file {p} has been updated.')
        s.add(f"ASSISTANT: {r.choice(NARRATION)}")
        s.facts.append(Fact(kind, [p], turn))
    elif kind == "commit":
        subj = f"Fix {_w(r)} {_w(r)} rounding in {_ident(r)}"
        s.add(f"USER: {r.choice(CHATTER)} and commit it")
        s.add(f'TOOL Bash [ok]: git add -A && git commit -q -m "{subj}" => '
              f"[main {r.randint(1000000, 9999999):x}] {subj}")
        s.add(f"ASSISTANT: {r.choice(NARRATION)}")
        s.facts.append(Fact(kind, [subj], turn))
    elif kind == "pull_request":
        n = r.randint(30, 400)
        title = f"Add {_w(r)} {_w(r)} support"
        s.add(f"USER: open a PR for this {pad}")
        s.add('TOOL mcp__github__create_pull_request [ok]: {"title": "' + title + '", '
              f'"head": "feature"}} => {{"url": "https://github.com/o/r/pull/{n}"}}')
        s.add(f"ASSISTANT: {r.choice(NARRATION)}")
        s.facts.append(Fact(kind, [f"#{n}"], turn))
    elif kind == "error_fixed":
        err = f"KeyError: '{_ident(r)}'"
        s.add(f"USER: run the tests {pad}")
        s.add(f"TOOL Bash [error]: pytest -q tests => FAILED tests/test_{_w(r)}.py::test_x - "
              f"{err}")
        s.add(f"ASSISTANT: {r.choice(INTENTIONS)}")
        s.add("TOOL Bash [ok]: pytest -q tests => 120 passed in 3.2s")
        s.facts.append(Fact(kind, [err.split(": ")[1]], turn))
    elif kind == "test_summary":
        n, f_ = r.randint(150, 900), r.randint(1, 9)
        s.add(f"USER: {r.choice(CHATTER)}")
        log = " ".join(f"tests/test_{_w(r)}.py ...." for _ in range(30))
        out = f"{log} {n} passed, {f_} failed in 41.0s"
        s.add(f"TOOL Bash [error]: pytest -q => {out[-300:]}")     # the end, as journaled
        s.add(f"ASSISTANT: {r.choice(NARRATION)}")
        s.facts.append(Fact(kind, [f"{n} passed", f"{f_} failed"], turn))
    elif kind == "root_cause":
        cause = f"the {_ident(r)} cache key ignored the tenant id"
        s.add(f"USER: why did it break? {pad}")
        s.add(f"ASSISTANT: {r.choice(NARRATION)} The root cause was that {cause}, so two "
              f"tenants shared entries. {r.choice(NARRATION)}")
        s.facts.append(Fact(kind, [cause.split(" cache")[0].split()[-1]], turn))
    elif kind == "pasted_error":
        fn = _ident(r)
        trace = (f"Traceback (most recent call last):\n  File \"app/{_w(r)}.py\", line "
                 f"{r.randint(10, 400)}, in {fn}\n    return rows[key]\n"
                 f"ValueError: invalid literal for int() with base 10: '{_w(r)}'")
        s.add(f"USER: getting this now {pad}\n{trace}")
        s.add(f"ASSISTANT: {r.choice(NARRATION)}")
        s.facts.append(Fact(kind, [fn], turn))
    elif kind == "pasted_config":
        key, val = _ident(r), r.randint(1000, 9999)
        cfg = json.dumps({"service": _w(r), key: val, "debug": False})
        s.add(f"USER: use this config {pad}\n{cfg}")
        s.add(f"ASSISTANT: {r.choice(NARRATION)}")
        s.facts.append(Fact(kind, [key, str(val)], turn))
    elif kind == "changed_value":
        old, new = r.randint(8000, 8099), r.randint(9000, 9099)
        svc = _ident(r)
        s.add(f"USER: run `{svc}` on port {old}")
        s.add(f"ASSISTANT: {r.choice(NARRATION)}")
        s.facts.append(Fact(kind, [str(new)], turn))
        s.add(f"USER: actually use port {new} instead of {old} for `{svc}`")
        s.add(f"ASSISTANT: {r.choice(NARRATION)}")
    elif kind == "unknown_outcome":
        branch = f"feature/{_w(r)}-{r.randint(10, 99)}"
        s.add(f"USER: push it {pad}")
        s.add(f"TOOL Bash [interrupted]: git push origin {branch} => ")
        s.add(f"OUTCOME UNKNOWN: Bash: git push origin {branch} was interrupted")
        s.facts.append(Fact(kind, [branch], turn))


def session(seed: int, turns: int, facts_per_kind: int = 2) -> Session:
    r = random.Random(seed)
    s = Session()
    plan = [k for k in KINDS for _ in range(facts_per_kind)]
    slots = r.sample(range(turns), min(len(plan), turns))
    at = dict(zip(slots, plan, strict=False))
    # two things whose state changes through the session
    check = f"make {_w(r)}-check"
    check_turns = set(r.sample(range(turns), min(turns, max(4, turns // 8))))
    pr_n, pr_title = r.randint(500, 900), f"Rework {_w(r)} {_w(r)} pipeline"
    pr_open, pr_merge = sorted(r.sample(range(turns), 2))
    last_count = ""
    for t in range(turns):
        if t in check_turns:
            last_count = f"{r.randint(1000, 4999)} passed"
            flaky = r.random() < 0.3
            s.add(f"TOOL Bash [{'error' if flaky else 'ok'}]: cd /repo && {check} 2>&1 | tail -3"
                  f" => {last_count}{', 2 failed' if flaky else ''} in {r.randint(20, 90)}.0s")
            if flaky:
                last_count = f"{r.randint(1000, 4999)} passed"
                s.add(f"TOOL Bash [ok]: cd /repo && {check} 2>&1 | tail -3 => {last_count} "
                      f"in {r.randint(20, 90)}.0s")
        if t == pr_open:
            s.add('TOOL mcp__github__create_pull_request [ok]: {"title": "' + pr_title + '"} => '
                  f'{{"url": "https://github.com/o/r/pull/{pr_n}"}}')
        if t == pr_merge:
            s.add(f'TOOL mcp__github__merge_pull_request [ok]: {{"pullNumber": {pr_n}}} => '
                  '{"merged": true}')
        if t in at:
            _plant(at[t], s, r, t)
        else:
            _noise_turn(s, r, r.choice([
                f"can you look at the {_w(r)} module", f"next, the {_w(r)} {_w(r)} please",
                *CHATTER]))
    s.facts.append(Fact("latest_result", [check, last_count], turns))
    s.facts.append(Fact("pr_merged", [f"#{pr_n}", "merged"], turns))
    return s


def kept(memory: str, fact: Fact) -> bool:
    if fact.kind in STATE_KINDS:       # the keys together, on one line of the memory
        return any(all(k in line for k in fact.keys) for line in memory.splitlines())
    return all(re.search(re.escape(k), memory) for k in fact.keys)


def run(build: Callable[[list[tuple[int, str]], int], str], seeds: list[int],
        lengths: tuple[int, ...] = (20, 60, 150), budget: int = 16_000,
        per_kind: int = 2) -> dict[str, Any]:
    by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_len: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    seconds = 0.0
    for n in lengths:
        for seed in seeds:
            s = session(seed * 1000 + n, n, per_kind)
            t = time.perf_counter()
            memory = build(s.lines, budget)
            seconds = max(seconds, time.perf_counter() - t)
            assert len(memory) <= budget
            for f in s.facts:
                ok = kept(memory, f)
                by_kind[f.kind][0] += ok
                by_kind[f.kind][1] += 1
                if f.kind in KINDS:               # the 13 kinds; state kinds apart
                    by_len[n][0] += ok
                    by_len[n][1] += 1
    return {"by_kind": dict(by_kind), "by_length": dict(by_len), "max_seconds": seconds}


def _report(name: str, res: dict[str, Any]) -> None:
    print(f"\n{name}")
    tot = [0, 0]
    for k in KINDS:
        a, b = res["by_kind"].get(k, [0, 0])
        tot[0] += a
        tot[1] += b
        print(f"  {k:<16} {a:>4}/{b:<4} {100 * a / max(b, 1):5.1f}%")
    for k in STATE_KINDS:
        a, b = res["by_kind"].get(k, [0, 0])
        print(f"  {k:<16} {a:>4}/{b:<4} {100 * a / max(b, 1):5.1f}%   (current state)")
    for n, (a, b) in sorted(res["by_length"].items()):
        print(f"  {n:>3} turns        {a:>4}/{b:<4} {100 * a / max(b, 1):5.1f}%")
    print(f"  all              {tot[0]:>4}/{tot[1]:<4} {100 * tot[0] / max(tot[1], 1):5.1f}%"
          f"   slowest build {res['max_seconds'] * 1000:.0f} ms")


def main() -> None:
    from continuum.coding import digest

    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="100-119")
    ap.add_argument("--budget", type=int, default=16_000)
    ap.add_argument("--lengths", default="20,60,150")
    ap.add_argument("--per-kind", type=int, default=2, help="facts of each kind per session")
    a = ap.parse_args()
    lo, _, hi = a.seeds.partition("-")
    seeds = list(range(int(lo), int(hi or lo) + 1))

    def tail(lines: list[tuple[int, str]], budget: int) -> str:
        return "\n".join(line for _, line in lines)[-budget:]

    lengths = tuple(int(x) for x in a.lengths.split(","))
    _report(f"last {a.budget} characters of the session (baseline)",
            run(tail, seeds, lengths, a.budget, a.per_kind))
    _report(f"session digest at {a.budget} characters",
            run(digest.build, seeds, lengths, a.budget, a.per_kind))


if __name__ == "__main__":
    main()
