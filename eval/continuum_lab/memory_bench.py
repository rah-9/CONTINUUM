"""Memory benchmark: does what the user said in a long chat reach the next model?

Generates realistic coding conversations of N user messages, each followed by the agent's
final report, and plants facts at random turns:

* explicit requirements ("must", "never", "make sure", ...), sometimes buried in chatter
* implicit requirements (no cue word: "Page size: 25 items.")
* revisions ("Actually, use port B instead of A")
* answers to the agent's questions (agent: "Should I drop table X?" user: "yes")
* decisions the agent mentions in its report ("I chose X because ...")
* noise: pasted logs containing cue words, small talk, "continue", plain task requests

Each conversation is captured exactly as the hooks capture it (one user message plus the
turn's final agent report per turn), the brief is built by the real service, and the
compact Markdown (what the next model reads) is scored. No model is involved.

Run: ``python -m continuum_lab.memory_bench`` (needs PostgreSQL; prints a table).
"""

from __future__ import annotations

import random
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXPLICIT = [
    "Uploads must be limited to {n} MB.",
    "Never log the `{w}` field; it holds personal data.",
    "Always use the `{w}` logger instead of print.",
    "Make sure every endpoint returns JSON with a `{w}` key on errors.",
    "Don't touch the `{w}` module, another team owns it.",
    "Retries should back off exponentially, capped at {n} seconds.",
    "The cache needs to expire after {n} minutes.",
    "Keep the public function names in `{w}` unchanged.",
    "From now on, run `{w}` before every commit.",
    "Important: store timestamps in UTC and name the column `{w}`.",
    "Avoid adding any dependency except `{w}`.",
    "`{w}` is the only database role the app may use.",
    "Requests to the billing API must time out after {n} seconds.",
    "Please make the CLI flag `--{w}` required.",
]
IMPLICIT = [
    "Page size: {n} items.",
    "Dates in the UI as {w}.",
    "Default region {w}.",
]
# Held out: phrasings never used while developing the classifier, to measure how it
# generalizes. Report these numbers as they are; do not tune the rules on them.
HELD_OUT = [
    "We're targeting Python {n} for this, nothing newer.",
    "Max upload size is {n} MB.",
    "No emojis anywhere in the `{w}` logs.",
    "I'd like the config output in `{w}` format.",
    "Let's stick with `{w}` for the HTTP layer.",
    "Use tabs in `{w}`, not spaces.",
    "The tests have to pass on Windows too, see `{w}`.",
    "Timeouts are {n} ms across the board.",
    "Errors go to stderr via `{w}`.",
    "Please don't rename the `{w}` table.",
    "Prefer composition over inheritance in `{w}`.",
    "Only admins can call `{w}`.",
]
# Second held-out set, written after the classifier was last changed and measured once.
# HELD_OUT above informed the rules (cue families), so it no longer measures
# generalization on its own; FRESH does.
FRESH = [
    "Store money as integer cents in `{w}`.",
    "Sessions expire {n} minutes after the last request.",
    "Don't bother with Windows support for `{w}`.",
    "Every migration gets a down step, `{w}` included.",
    "Log lines are JSON, one object per line, field `{w}` first.",
    "Make the `{w}` endpoint idempotent.",
    "I want `{w}` behind a feature flag.",
    "Leave `{w}` alone for now, it's being rewritten.",
    "Ports below {n} are off limits.",
    "The CLI exit code for bad input is {n}.",
    "Cache keys get the `{w}` prefix.",
    "Write the changelog entry under `{w}`.",
]
# Third set, written after the structural change (statements kept at the lowest
# priority), measured once and reported as is.
FRESH2 = [
    "Health checks hit `/{w}` every {n} seconds.",
    "Admins see the audit trail in `{w}`, regular users never do.",
    "The importer skips rows with an empty `{w}` column.",
    "Feature work goes on branches named `feat/{w}`.",
    "Our SLA is {n} ms at p99 for `{w}`.",
    "Monetary amounts round half-even to {n} decimals.",
    "Background jobs retry up to {n} times, then land in `{w}`.",
    "We ship on Thursdays; `{w}` freezes the day before.",
]
# Discussion that is neither a requirement nor small talk: it competes for the budget.
DISCUSSION = [
    "I think the current structure is fine for now. The team talked about `{w}` yesterday. "
    "We might revisit the module layout later this quarter.",
    "The {w} dashboard was slow this morning. It recovered after a few minutes. "
    "Probably unrelated to our change.",
    "Context: this service replaces the old `{w}` cron job. That job ran for years. "
    "Some customers still remember its quirks.",
]
CHATTER_BEFORE = ["Looks good so far.", "Nice work.", "OK, I tried it.", "Thanks for that."]
CHATTER_AFTER = ["Also, can you check the tests?", "Then carry on.", "What do you think?", ""]
TASKS = ["Add a `{w}` command.", "Refactor the `{w}` helper.", "Write tests for `{w}`.",
         "Rename `{w}` to something clearer.", "Speed up the `{w}` query."]
SMALL = ["continue", "ok", "thanks", "go on", "nice", "keep going", "looks good"]
QUESTIONS = ["What does `{w}` do?", "How are we handling `{w}` right now?",
             "Why is `{w}` slow?"]
LOG = ("I got this, can you look?\nTraceback (most recent call last):\n"
       "  File \"/srv/app/{w}.py\", line 41, in load\n"
       "ValueError: config must define {w}; this value should never be empty\n"
       "WARNING {w}: must retry")


@dataclass
class Fact:
    kind: str           # explicit | implicit | revision | answer | agent_decision | noise
    probe: str          # text that must (or, for noise, must not) reach the brief
    turn: int
    stale: str | None = None  # for revisions: the old value


@dataclass
class Conversation:
    turns: list[tuple[str, str]] = field(default_factory=list)  # (user, agent report)
    facts: list[Fact] = field(default_factory=list)


def _token(rng: random.Random, used: set[str]) -> str:
    while True:
        w = "".join(rng.choice("bcdfghjklmnpqrstvwxz") for _ in range(3)) + \
            str(rng.randint(10, 99))
        if w not in used:
            used.add(w)
            return w


def generate(seed: int, n: int, *, verbose: bool = False) -> Conversation:
    """A conversation of ``n`` user messages with planted facts at random turns."""
    rng = random.Random(seed)
    used: set[str] = set()
    conv = Conversation()
    plan: list[str] = []
    k = max(3, n // 4)
    plan += ["explicit"] * k + ["implicit"] * max(1, n // 20) + ["revision"] * max(1, n // 20)
    plan += ["held_out"] * max(2, n // 8) + ["fresh"] * max(2, n // 8)
    plan += ["fresh2"] * max(2, n // 8)
    plan += ["answer"] * max(1, n // 15) + ["agent_decision"] * max(1, n // 15)
    plan += ["noise"] * max(1, n // 15)
    plan += ["filler"] * max(0, n - 1 - len(plan))
    rng.shuffle(plan)
    plan = plan[: n - 1]
    goal = f"Let's build the `{_token(rng, used)}` service: an HTTP API with a CLI."
    conv.turns.append((goal, "Set up the project skeleton."))
    pending_answer: str | None = None
    revisions: list[tuple[str, str]] = []
    for i, what in enumerate(plan, start=2):
        if pending_answer is not None:      # the agent asked a question last turn
            conv.turns.append((rng.choice(["yes", "yes please", "ok, do it"]),
                               "Done as agreed."))
            conv.facts.append(Fact("answer", pending_answer, i))
            pending_answer = None
            continue
        report = "Done."
        n_val = str(rng.randint(101, 997))
        w = _token(rng, used)
        match what:
            case "explicit":
                text = rng.choice(EXPLICIT).format(n=n_val, w=w)
                probe = n_val if "{n}" in text or n_val in text else w
                if rng.random() < 0.5:
                    text = f"{rng.choice(CHATTER_BEFORE)} {text} {rng.choice(CHATTER_AFTER)}"
                conv.facts.append(Fact("explicit", probe, i))
            case "fresh2":
                text = rng.choice(FRESH2).format(n=n_val, w=w)
                if rng.random() < 0.5:
                    text = f"{rng.choice(CHATTER_BEFORE)} {text} {rng.choice(CHATTER_AFTER)}"
                conv.facts.append(Fact("fresh2", n_val if n_val in text else w, i))
            case "fresh":
                text = rng.choice(FRESH).format(n=n_val, w=w)
                if rng.random() < 0.5:
                    text = f"{rng.choice(CHATTER_BEFORE)} {text} {rng.choice(CHATTER_AFTER)}"
                conv.facts.append(Fact("fresh", n_val if n_val in text else w, i))
            case "held_out":
                text = rng.choice(HELD_OUT).format(n=n_val, w=w)
                if rng.random() < 0.5:
                    text = f"{rng.choice(CHATTER_BEFORE)} {text} {rng.choice(CHATTER_AFTER)}"
                conv.facts.append(Fact("held_out", n_val if n_val in text else w, i))
            case "implicit":
                text = rng.choice(IMPLICIT).format(n=n_val, w=w)
                conv.facts.append(Fact("implicit", n_val if n_val in text else w, i))
            case "revision":
                old, new = str(rng.randint(1000, 4999)), str(rng.randint(5000, 9999))
                text = f"Use port {old} for the dev server."
                revisions.append((old, new))
                conv.facts.append(Fact("revision_old", old, i))
            case "answer":
                text = rng.choice(TASKS).format(w=w)
                report = f"Done. Should I also delete the legacy `{w}_v1` table?"
                pending_answer = f"{w}_v1"
            case "agent_decision":
                text = rng.choice(TASKS).format(w=w)
                choice = _token(rng, used)       # never in the user's words
                report = f"Implemented it. I chose `{choice}` because it handles concurrent writes."
                conv.facts.append(Fact("agent_decision", choice, i))
            case "noise":
                text = LOG.format(w=w)
                conv.facts.append(Fact("noise", w, i))
            case _ if verbose and rng.random() < 0.6:
                text = rng.choice(DISCUSSION).format(w=w)
            case _:
                text = rng.choice(SMALL + QUESTIONS + TASKS).format(w=w)
        conv.turns.append((text, report))
    # revisions land late in the chat, after the value they change
    for old, new in revisions:
        at = rng.randint(max(2, len(conv.turns) // 2), len(conv.turns))
        while at < len(conv.turns) and conv.turns[at - 1][1].endswith("?"):
            at += 1                  # never between a question and its answer
        conv.turns.insert(at, (f"Actually, change of plan: use port {new} instead of {old}.",
                               "Switched."))
        conv.facts.append(Fact("revision", new, at + 1, stale=old))
    if pending_answer is not None:
        conv.turns.append(("yes", "Done as agreed."))
        conv.facts.append(Fact("answer", pending_answer, len(conv.turns)))
    return conv


def score(markdown: str, conv: Conversation) -> dict[str, Any]:
    """Recall per kind (what reached the brief), noise leaks, and size."""
    out: dict[str, Any] = {}
    last_two = " ".join(u for u, _ in conv.turns[-2:])
    for kind in ("explicit", "implicit", "held_out", "fresh", "fresh2", "revision", "answer",
                 "agent_decision"):
        facts = [f for f in conv.facts if f.kind == kind]
        out[kind] = (sum(1 for f in facts if f.probe in markdown), len(facts))
    stale = [f for f in conv.facts if f.kind == "revision"]
    out["revision_order_ok"] = sum(
        1 for f in stale if f.probe in markdown and (
            f.stale is None or f.stale not in markdown
            or markdown.rfind(f.stale) < markdown.rfind(f.probe)))
    noise = [f for f in conv.facts if f.kind == "noise" and f.probe not in last_two]
    out["noise_leaks"] = (sum(1 for f in noise if f.probe in markdown), len(noise))
    out["tokens"] = len(markdown) // 4
    return out


def legacy_v080(users: list[str]) -> str:
    """The v0.8.0 selection, for comparison: first message + newest 6 substantive ones,
    1,200 characters each."""
    low = {"continue", "go on", "ok", "okay", "yes", "thanks", "nice", "keep going",
           "looks good", "yes please", "ok, do it"}
    keep = [u for i, u in enumerate(users) if i == len(users) - 1 or u.lower() not in low]
    chosen = keep if len(keep) <= 7 else keep[:1] + keep[-6:]
    return "\n".join(u[:1200] for u in chosen)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,  # noqa: S603,S607
                   capture_output=True)


def run_one(coding: Any, seed: int, n: int, workdir: Path, *,
            verbose: bool = False) -> dict[str, Any]:
    """Capture one conversation turn by turn, as the hooks do, and score the brief."""
    from continuum.coding.brief import compact
    from continuum.coding.transcript import Message

    conv = generate(seed, n, verbose=verbose)
    repo = workdir / f"repo_{seed}_{n}_{int(verbose)}"
    repo.mkdir()
    _git(repo, "init", "-q")
    for user, report in conv.turns:
        coding.capture(str(repo), [Message("user", user), Message("agent", report)],
                       source="bench", chat_id=f"chat-{seed}")
    md = compact(coding.brief(coding.latest_active(str(repo))))["markdown"]
    new = score(md, conv)
    old = score(legacy_v080([u for u, _ in conv.turns]), conv)
    return {"seed": seed, "n": n, "new": new, "legacy": old}


def summarize(results: list[dict[str, Any]], which: str) -> dict[str, Any]:
    total: dict[str, list[int]] = {}
    for r in results:
        for key, value in r[which].items():
            if isinstance(value, tuple):
                got, of = total.setdefault(key, [0, 0])
                total[key] = [got + value[0], of + value[1]]
    summary: dict[str, Any] = {k: (v[0] / v[1] if v[1] else 1.0) for k, v in total.items()}
    summary["tokens_max"] = max(r[which]["tokens"] for r in results)
    return summary


def main() -> None:  # pragma: no cover - manual benchmark
    import os

    from continuum.coding.service import CodingContinuity
    from continuum.core.clock import SystemClock
    from continuum.core.ids import RandomIds
    from continuum.core.store import Store

    dsn = os.environ.get("CONTINUUM_TEST_DATABASE_URL",
                         "postgresql://continuum:continuum@localhost:5432/continuum_test")
    schema = "bench_" + "".join(random.choice("abcdefgh") for _ in range(8))
    store = Store(dsn, schema=schema, clock=SystemClock(), ids=RandomIds())
    store.migrate()
    coding = CodingContinuity(store, clock=SystemClock(), ids=RandomIds())
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for n, verbose in ((10, False), (20, False), (40, False), (80, False),
                               (40, True), (80, True), (160, True)):
                results = [run_one(coding, seed, n, Path(tmp), verbose=verbose)
                           for seed in range(20)]
                for which in ("legacy", "new"):
                    s = summarize(results, which)
                    label = f"n={n:3d}{' verbose' if verbose else ''}"
                    print(f"{label:12s} {which:7s} " + "  ".join(
                        f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                        for k, v in s.items()))
    finally:
        with store.connection() as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE'.encode())
            conn.commit()
        store.close()


if __name__ == "__main__":  # pragma: no cover
    main()

