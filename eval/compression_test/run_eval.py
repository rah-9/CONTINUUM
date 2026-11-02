# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Context-compression test on a real session (D-048; results in RESULTS.md).

Builds several compressed versions of the same history (same size budget), then has a
fresh model answer questions with known answers from each version alone.

  python run_eval.py export TRANSCRIPT  (writes history.txt from a Claude Code transcript)
  python run_eval.py build  METHOD   (tail | summary | state | anchored | hybrid | combined)
  python run_eval.py answer METHOD   (any method with a ctx_<METHOD>.md file)
  python run_eval.py score
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(os.environ.get("COMPRESSION_TEST_DIR", Path(__file__).resolve().parent))
# a command that runs `claude` with the right environment (e.g. `env` or a wrapper)
WRAP = Path(os.environ.get("CLAUDE_WRAPPER", "/usr/bin/env"))
RUN = HERE / "run"
BUDGET_CHARS = 24_000           # ~6k tokens: the same budget for every method
CHUNK_CHARS = 110_000           # ~27k tokens per step for the model-based methods
HISTORY = (HERE / "history.txt").read_text() if (HERE / "history.txt").exists() else ""


def export(transcript: str) -> None:
    """The faithful linear history the methods compress: every message whole, every tool
    call as one line (the end of its result; the user's answers to questions whole)."""
    from continuum.coding.activity import command_of
    from continuum.coding.transcript import read_all

    msgs, tools = read_all(transcript, app="claude-code", chat="s")
    out = [(m.at or "", f"{'USER' if m.role == 'user' else 'ASSISTANT'}: {m.text}")
           for m in msgs if not m.abandoned]
    starts: dict[str, dict] = {}
    for t in tools:
        if t["phase"] == "start":
            starts[t["call_key"]] = t
        elif t["phase"] == "finish":
            s = starts.get(t["call_key"], t)
            raw = s.get("input")
            what = command_of(raw) if s.get("kind") == "shell" else json.dumps(raw)
            res = t.get("error") or t.get("output") or ""
            res = res if t["tool"] == "AskUserQuestion" else res[-300:]
            out.append((t.get("at") or "", f"TOOL {t['tool']} [{t['status']}]: "
                                           f"{str(what)[:300]} => {' '.join(res.split())}"))
    out.sort(key=lambda x: x[0])
    (HERE / "history.txt").write_text("\n".join(x[1] for x in out))


def llm(instruction: str, data: str, model: str = "haiku",
        timeout: int = 900) -> tuple[str, float]:
    RUN.mkdir(exist_ok=True)
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(WRAP), "claude", "-p", instruction, "--model", model, "--output-format", "json",
         "--disallowedTools", "Bash Read Write Edit Grep Glob WebFetch WebSearch Task "
                              "NotebookEdit TodoWrite"],
        input=data, capture_output=True, text=True, cwd=RUN, timeout=timeout)
    try:
        r = json.loads(out.stdout)
    except ValueError as err:
        raise SystemExit(f"model call failed: {out.stdout[-500:]} {out.stderr[-500:]}") from err
    return str(r.get("result", "")), float(r.get("total_cost_usd", 0) or 0)


def chunks(text: str, size: int) -> list[str]:
    lines, out, cur = text.splitlines(), [], []
    n = 0
    for ln in lines:
        if n + len(ln) > size and cur:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln[:6000])           # one giant line must not fill a chunk alone
        n += len(cur[-1]) + 1
    if cur:
        out.append("\n".join(cur))
    return out


SUMMARY_STEP = """You are compacting a long coding-agent session so another model can continue it.
Below is one part of the session history (USER and ASSISTANT messages and TOOL calls with
results). Write a dense summary of THIS PART that keeps what a successor must know: the
user's requests, decisions and preferences (with their exact values), what was built or
changed, bugs found and how they were fixed, results and numbers, PR numbers, open items.
Prefer concrete facts over narrative. At most 3,000 characters. Output only the summary."""

SUMMARY_MERGE = f"""Below are summaries of consecutive parts of one long coding-agent session,
oldest first. Merge them into ONE summary for a model that will continue the work. Keep every
concrete fact a successor might need (user decisions with exact values, numbers, PR
numbers, names, bugs and fixes, results, open items); drop repetition. At most
{BUDGET_CHARS} characters. Output only the merged summary."""

STATE_STEP = f"""You maintain the WORKING MEMORY of a long coding-agent session, so another model
can continue it. You get the current memory and the next part of the history (USER and
ASSISTANT messages, TOOL calls with results). Return the UPDATED memory, in exactly these
sections:

## Goal and current task
## User decisions, preferences and constraints   (exact values; mark superseded ones)
## What was built / changed                      (with PR numbers, versions)
## Problems found and how they were fixed
## Results and measurements                      (numbers)
## Research and external facts
## Open items and the user's latest requests

Rules: one fact per bullet, each ending with its source in brackets: the user message
date/time or PR number if known. Keep older facts unless a newer part changes them (then
update and say what replaced what). Never invent facts. Keep the whole memory under
{BUDGET_CHARS} characters by merging and tightening older bullets, not by dropping user
decisions. Output only the memory."""


ANCHORED_STEP = """You maintain the WORKING MEMORY of a long coding-agent session as numbered items,
so another model can continue the work. You get the current items and the next part of the
history; every history line starts with its id like [L1234].

Return ONLY a JSON list of edit operations (never rewrite the memory):
  {"op": "add", "section": S, "text": "...", "src": ["L1234", ...]}
  {"op": "update", "id": N, "text": "...", "src": [...]}      (same fact, new detail/value)
  {"op": "supersede", "id": N, "text": "...", "src": [...]}   (a later part replaced it)
  {"op": "drop", "id": N}                                     (only for pure noise)
S is one of: goal, decision (the user's decisions, preferences, constraints, with exact
values), built (what was built/changed, PR numbers, versions), problem (bugs found and the
fix), result (measurements, numbers), research (external facts), open (open items, the
user's latest requests).
Rules: one fact per item, concrete (names, numbers, PR numbers, exact values). Each item
cites the [L..] lines that state it; numbers and names must appear in those lines. Prefer
facts a successor needs; skip routine tool chatter. At most 25 operations."""


def _apply(items: dict[int, dict], ops: list, lines: dict[str, str], stats: dict) -> None:
    nid = max(items, default=0) + 1
    for op in ops if isinstance(ops, list) else []:
        if not isinstance(op, dict):
            continue
        kind, text = op.get("op"), str(op.get("text", "")).strip()
        src = [s for s in op.get("src", []) if isinstance(s, str)]
        if kind in ("add", "update", "supersede"):
            cited = " ".join(lines.get(s, "") for s in src)
            # deterministic gate: cited lines exist, and every number in the item is in them
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if not cited or any(n not in cited for n in numbers):
                stats["rejected"] += 1
                continue
            stats["applied"] += 1
        if kind == "add":
            items[nid] = {"section": op.get("section", "open"), "text": text, "src": src}
            nid += 1
        elif kind in ("update", "supersede") and op.get("id") in items:
            old = items[op["id"]]
            if kind == "supersede":
                old["text"] = f"{text} (replaced earlier: {old['text'][:120]})"
            else:
                old["text"] = text
            old["src"] = (old["src"] + src)[-4:]
        elif kind == "drop" and op.get("id") in items:
            items.pop(op["id"])


SECTIONS = [("goal", "Goal"), ("decision", "User decisions and constraints"),
            ("built", "What was built"), ("problem", "Problems and fixes"),
            ("result", "Results"), ("research", "Research"), ("open", "Open items")]


def _render(items: dict[int, dict], limit: int) -> str:
    """Every section gets a share of the budget (by its number of items, at least 6%);
    within a section the newest items come first. One source tag per item."""
    by: dict[str, list[tuple[int, dict]]] = {k: [] for k, _ in SECTIONS}
    for i, it in items.items():
        by.setdefault(it["section"] if it["section"] in by else "open", []).append((i, it))
    total = sum(len(v) for v in by.values()) or 1
    out = []
    for key, title in SECTIONS:
        rows = sorted(by[key], key=lambda x: -x[0])
        if not rows:
            continue
        share = max(0.06, len(rows) / total) * (limit - 400)
        block, used = [f"## {title}"], len(title) + 4
        for _i, it in rows:
            line = f"- {it['text']} ({it['src'][0] if it['src'] else ''})"
            if used + len(line) + 1 > share:
                block.append(f"- ... {len(rows) - len(block) + 1} older item(s) not shown")
                break
            block.append(line)
            used += len(line) + 1
        out += block
    return "\n".join(out)[:limit]


def build(method: str) -> None:
    cost = 0.0
    if method == "tail":
        ctx = HISTORY[-BUDGET_CHARS:]
    elif method == "summary":
        parts = []
        for i, part in enumerate(chunks(HISTORY, CHUNK_CHARS)):
            text, c = llm(SUMMARY_STEP, part)
            cost += c
            parts.append(f"### Part {i + 1}\n{text}")
            print(f"summary part {i + 1}: {len(text)} chars, ${c:.3f}", flush=True)
        ctx, c = llm(SUMMARY_MERGE, "\n\n".join(parts))
        cost += c
    elif method == "anchored":
        numbered = "\n".join(f"[L{i}] {ln[:6000]}" for i, ln in
                              enumerate(HISTORY.splitlines(), 1))
        lines = {f"L{i}": ln for i, ln in enumerate(HISTORY.splitlines(), 1)}
        items: dict[int, dict] = {}
        stats = {"applied": 0, "rejected": 0}
        for i, part in enumerate(chunks(numbered, CHUNK_CHARS)):
            current = "\n".join(f"[{k}] ({v['section']}) {v['text']}" for k, v in items.items())
            text, c = llm(ANCHORED_STEP, f"# CURRENT ITEMS\n{current or '(none)'}\n\n# NEXT "
                                         f"PART OF THE HISTORY ({i + 1})\n{part}")
            cost += c
            m = re.search(r"\[.*\]", text, re.S)
            try:
                ops = json.loads(m.group(0)) if m else []
            except ValueError:
                ops = []
            _apply(items, ops, lines, stats)
            print(f"anchored step {i + 1}: {len(items)} items, {stats}, ${c:.3f}", flush=True)
        (HERE / "anchored_items.json").write_text(json.dumps(items))
        ctx = _render(items, BUDGET_CHARS)
        (HERE / "anchored_stats.json").write_text(json.dumps(stats))
    elif method == "hybrid":
        # research-recommended mix: model-written memory of the whole history, plus the
        # most recent part word for word; same total budget
        raw = json.loads((HERE / "anchored_items.json").read_text())
        items = {int(k): v for k, v in raw.items()}
        half = BUDGET_CHARS // 2
        ctx = ("# Working memory of the whole session (model-written, cited)\n"
               + _render(items, half - 80) + "\n\n# The most recent part, word for word\n"
               + HISTORY[-(half - 80):])
    elif method == "combined":
        # the user's exact words (rule-based ledger) + the model's structured memory
        brief = (HERE / "ctx_brief.md").read_text()
        asked = brief.split("## What the user asked", 1)[1].split("\n## ", 1)[0]
        asked = "## What the user asked (exact words)" + asked[:9000]
        state = (HERE / "ctx_state.md").read_text()
        room = BUDGET_CHARS - len(asked) - 40
        ctx = asked + "\n\n# Working memory (model-written)\n" + state[:room]
    elif method == "state":
        memory = "(empty: nothing recorded yet)"
        for i, part in enumerate(chunks(HISTORY, CHUNK_CHARS)):
            memory, c = llm(STATE_STEP, f"# CURRENT MEMORY\n{memory}\n\n# NEXT PART OF THE "
                                        f"HISTORY ({i + 1})\n{part}")
            cost += c
            print(f"state step {i + 1}: {len(memory)} chars, ${c:.3f}", flush=True)
        ctx = memory
    else:
        raise SystemExit(f"unknown method {method}")
    (HERE / f"ctx_{method}.md").write_text(ctx)
    (HERE / f"cost_{method}.json").write_text(json.dumps({"build_usd": cost,
                                                           "chars": len(ctx)}))
    print(f"{method}: {len(ctx)} chars (~{len(ctx) // 4} tokens), build cost ${cost:.3f}")


ANSWER = """You are continuing a coding project. The ONLY thing you know about the earlier
work is the context below. Answer each question briefly from it. If the context does not
say, answer exactly "unknown" (never guess). Return only JSON: {"1": "...", "2": "...", ...}."""


def answer(method: str, model: str = "sonnet") -> None:
    ctx = (HERE / f"ctx_{method}.md").read_text()[:BUDGET_CHARS]   # same budget for all
    qa = json.loads((HERE / "qa.json").read_text())
    questions = "\n".join(f"{q['id']}. {q['q']}" for q in qa)
    text, cost = llm(ANSWER, f"# CONTEXT\n{ctx}\n\n# QUESTIONS\n{questions}", model=model)
    m = re.search(r"\{.*\}", text, re.S)
    answers = json.loads(m.group(0)) if m else {}
    (HERE / f"answers_{method}.json").write_text(json.dumps(
        {"answers": answers, "answer_usd": cost}, indent=1))
    print(f"{method}: answered {len(answers)} questions, ${cost:.3f}")


def answer_recall(method: str, sid: str, model: str = "sonnet") -> None:
    """The real product setup: the brief in context PLUS search over the stored history
    (coding_recall / coding_show over MCP). Bounded to 12 tool turns."""
    ctx = (HERE / f"ctx_{method}.md").read_text()[:BUDGET_CHARS]
    qa = json.loads((HERE / "qa.json").read_text())
    questions = "\n".join(f"{q['id']}. {q['q']}" for q in qa)
    instruction = (ANSWER.replace("is the context below", "is the context below and what "
                   "you find with the coding_recall and coding_show tools")
                   + f" The Continuum session id is {sid}. Search for anything the context "
                   "does not answer; keep searches short and specific.")
    RUN.mkdir(exist_ok=True)
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(WRAP), "claude", "-p", instruction, "--model", model, "--output-format", "json",
         "--mcp-config", str(HERE / "mcp.json"), "--strict-mcp-config", "--max-turns", "40",
         "--allowedTools", "mcp__continuum__coding_recall mcp__continuum__coding_show",
         "--disallowedTools", "Bash Read Write Edit Grep Glob WebFetch WebSearch Task"],
        input=f"# CONTEXT\n{ctx}\n\n# QUESTIONS\n{questions}", capture_output=True,
        text=True, cwd=RUN, timeout=1800)
    r = json.loads(out.stdout)
    text = str(r.get("result", ""))
    m = re.search(r"\{.*\}", text, re.S)
    answers = json.loads(m.group(0)) if m else {}
    name = f"{method}+recall"
    (HERE / f"ctx_{name}.md").write_text(ctx)
    (HERE / f"answers_{name}.json").write_text(json.dumps(
        {"answers": answers, "answer_usd": r.get("total_cost_usd"),
         "turns": r.get("num_turns")}, indent=1))
    print(f"{name}: {len(answers)} answers, ${r.get('total_cost_usd', 0):.3f}, "
          f"{r.get('num_turns')} turns")


def score() -> None:
    qa = json.loads((HERE / "qa.json").read_text())
    rows = []
    for f in sorted(HERE.glob("answers_*.json")):
        method = f.stem.removeprefix("answers_")
        answers = json.loads(f.read_text())["answers"]
        ok, by_part = 0, {}
        for q in qa:
            a = str(answers.get(str(q["id"]), "")).lower()
            hit = a != "unknown" and all(any(k in a for k in group) for group in q["keys"])
            ok += hit
            t = by_part.setdefault(q["part"], [0, 0])
            t[0] += hit
            t[1] += 1
        size = min(BUDGET_CHARS, len((HERE / f"ctx_{method}.md").read_text()))
        rows.append((method, ok, len(qa), size, by_part))
    for method, ok, n, size, parts in sorted(rows, key=lambda r: -r[1]):
        detail = "  ".join(f"{p} {a}/{b}" for p, (a, b) in parts.items())
        print(f"{method:<14} {ok:>2}/{n}  ~{size // 4:>5} tokens   {detail}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "export":
        export(sys.argv[2])
    elif cmd == "build":
        build(sys.argv[2])
    elif cmd == "recall":
        answer_recall(sys.argv[2], sys.argv[3])
    elif cmd == "answer":
        answer(sys.argv[2], *(sys.argv[3:4]))
    else:
        score()
