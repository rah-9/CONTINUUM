# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Context-compression test on a real session (D-048; results in RESULTS.md).

Builds several compressed versions of the same history (same size budget), then has a
fresh model answer questions with known answers from each version alone.

  python run_eval.py export TRANSCRIPT   writes history.txt from a coding-app transcript
  python run_eval.py build METHOD [SID]  tail | summary | state | anchored | hybrid |
                                         combined (model-built); digest | product (from
                                         the Continuum journal of session SID)
  python run_eval.py coverage            offline score, no model: is each answer stated
                                         near the question's own topic words?
  python run_eval.py answer METHOD       a reader model answers from ctx_<METHOD>.md
  python run_eval.py recall METHOD SID   the same, plus coding_recall/coding_show tools
  python run_eval.py score

Models: every call goes to an OpenAI-compatible endpoint, by default OpenRouter's free
models (key from OPENROUTER_API_KEY). EVAL_MODEL builds the model-built methods and
EVAL_READER answers; both default to a free model, and a paid OpenRouter model is
refused unless CONTINUUM_MEMORY_ALLOW_PAID=1. CONTINUUM_MEMORY_API_BASE points the calls
at another server (a local llama.cpp, vLLM, LM Studio or Ollama).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from continuum.coding import working_memory as wm

HERE = Path(os.environ.get("COMPRESSION_TEST_DIR", Path(__file__).resolve().parent))
EVAL_MODEL = os.environ.get("EVAL_MODEL", wm.DEFAULT_OPENROUTER_MODEL)
EVAL_READER = os.environ.get("EVAL_READER", EVAL_MODEL)
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


def _client(model: str) -> wm.OpenAICompatibleSummarizer:
    base = os.environ.get("CONTINUUM_MEMORY_API_BASE")
    if base:
        return wm.OpenAICompatibleSummarizer(model, base_url=base,
                                             key_env="CONTINUUM_MEMORY_API_KEY",
                                             label="openai-compatible")
    return wm.OpenAICompatibleSummarizer(model)


def llm(instruction: str, data: str, model: str = "") -> tuple[str, float]:
    """One model call; a rate limit waits and retries twice, then stops the run."""
    client = _client(model or EVAL_MODEL)
    for attempt in range(3):
        try:
            text, cost = client.complete(instruction, data)
            return text, cost or 0.0
        except wm.MemoryModelError as err:
            if "limit" not in str(err) or attempt == 2:
                raise SystemExit(f"model call failed: {err}") from err
            time.sleep(30 * (attempt + 1))
    raise SystemExit("unreachable")


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


def _service():
    from continuum.coding.service import CodingContinuity
    from continuum.core.clock import SystemClock
    from continuum.core.ids import RandomIds
    from continuum.core.store import Store

    store = Store(os.environ.get("CONTINUUM_DATABASE_URL",
                                 "postgresql://continuum:continuum@localhost:5432/continuum"),
                  schema=os.environ.get("CONTINUUM_DB_SCHEMA", "public"),
                  clock=SystemClock(), ids=RandomIds())
    return CodingContinuity(store, clock=SystemClock(), ids=RandomIds())


def _journal_lines(sid: str) -> list[tuple[int, str]]:
    _, events = _service()._events(sid)
    return wm.history_lines(events)


def build(method: str, sid: str = "") -> None:
    cost = 0.0
    if method == "digest":
        # no model: the extractive session digest, from the journal (D-049)
        from continuum.coding import digest

        ctx = digest.build(_journal_lines(sid), BUDGET_CHARS)
    elif method == "product":
        # the brief the product gives a new model, whatever its memory mode
        ctx = _service().brief(sid)["markdown"]
    elif method == "tail":
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


def answer(method: str, model: str = "") -> None:
    ctx = (HERE / f"ctx_{method}.md").read_text()[:BUDGET_CHARS]   # same budget for all
    qa = json.loads((HERE / "qa.json").read_text())
    questions = "\n".join(f"{q['id']}. {q['q']}" for q in qa)
    text, cost = llm(ANSWER, f"# CONTEXT\n{ctx}\n\n# QUESTIONS\n{questions}",
                     model=model or EVAL_READER)
    m = re.search(r"\{.*\}", text, re.S)
    answers = json.loads(m.group(0)) if m else {}
    (HERE / f"answers_{method}.json").write_text(json.dumps(
        {"answers": answers, "answer_usd": cost}, indent=1))
    print(f"{method}: answered {len(answers)} questions, ${cost:.3f}")


def clean_copy(sid: str, schema: str) -> None:
    """Copy the session into ``schema``, cut before the first event that mentions this test.

    When the session under test is the one that built the test, its journal holds
    ``qa.json`` with the answers, and search finds them: a first run scored 32/32 that way.
    Search must only see the history from before the test was written."""
    import psycopg

    from continuum.core.clock import SystemClock
    from continuum.core.ids import RandomIds
    from continuum.core.store import Store

    dsn, src = os.environ["CONTINUUM_DATABASE_URL"], os.environ["CONTINUUM_DB_SCHEMA"]
    Store(dsn, schema=schema, clock=SystemClock(), ids=RandomIds()).migrate()
    marks = ("%qa.json%", "%compression_test%", "%known-answer question%")
    with psycopg.connect(dsn) as c:
        row = c.execute(
            f"SELECT min(seq) FROM {src}.coding_events WHERE session_id = %s AND "
            "(payload_json::text LIKE %s OR payload_json::text LIKE %s "
            "OR payload_json::text LIKE %s)", (sid, *marks)).fetchone()
        cut = row[0] if row and row[0] else 1 << 62
        c.execute(f"INSERT INTO {schema}.coding_sessions SELECT session_id, repo_root, goal, "
                  f"status, {cut - 1}, created_at, updated_at, repo_key, owner_role "
                  f"FROM {src}.coding_sessions WHERE session_id = %s", (sid,))
        c.execute(f"INSERT INTO {schema}.coding_events (session_id, seq, type, actor, "
                  f"payload_json, occurred_at) SELECT session_id, seq, type, actor, "
                  f"payload_json, occurred_at FROM {src}.coding_events "
                  f"WHERE session_id = %s AND seq < %s", (sid, cut))
        c.execute(f"INSERT INTO {schema}.coding_blobs (session_id, digest, media_type, chars, "
                  f"content, truncated, created_at) SELECT session_id, digest, media_type, "
                  f"chars, content, truncated, created_at FROM {src}.coding_blobs "
                  f"WHERE session_id = %s AND content NOT LIKE %s AND content NOT LIKE %s "
                  f"AND content NOT LIKE %s", (sid, *marks))
    print(f"{schema}: events before #{cut}; point mcp.json at CONTINUUM_DB_SCHEMA={schema}")


RECALL_TOOLS = [
    {"type": "function", "function": {
        "name": "coding_recall", "description": "Search the session's whole journal.",
        "parameters": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "coding_show", "description": "Open one journal event by its number.",
        "parameters": {"type": "object", "required": ["seq"], "properties": {
            "seq": {"type": "integer"}}}}}]


def answer_recall(method: str, sid: str, model: str = "", max_turns: int = 12) -> None:
    """The real product setup: the brief in context PLUS search over the stored history
    (coding_recall / coding_show, run in-process against CONTINUUM_DB_SCHEMA). The
    searched journal must not contain this test: see ``clean_copy``."""
    import urllib.request

    coding = _service()
    ctx = (HERE / f"ctx_{method}.md").read_text()[:BUDGET_CHARS]
    qa = json.loads((HERE / "qa.json").read_text())
    questions = "\n".join(f"{q['id']}. {q['q']}" for q in qa)
    instruction = (ANSWER.replace("is the context below", "is the context below and what "
                   "you find with the coding_recall and coding_show tools")
                   + " Search for anything the context does not answer; keep searches "
                   "short and specific.")
    client = _client(model or EVAL_READER)
    if (client.label == "openrouter" and not client.model.endswith(":free")
            and os.environ.get("CONTINUUM_MEMORY_ALLOW_PAID") != "1"):
        raise SystemExit(f"{client.model} is not a free model")
    messages: list[dict] = [{"role": "system", "content": instruction},
                            {"role": "user", "content": f"# CONTEXT\n{ctx}\n\n# QUESTIONS\n"
                                                        f"{questions}"}]
    key = os.environ.get(client.key_env, "")
    turns, text = 0, ""
    while turns < max_turns:
        turns += 1
        body = json.dumps({"model": client.model, "temperature": 0, "messages": messages,
                           "tools": RECALL_TOOLS}).encode()
        req = urllib.request.Request(  # noqa: S310 - https/http base from the client
            f"{client.base_url.rstrip('/')}/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {key}"} if key else {})})
        with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310
            msg = json.loads(resp.read())["choices"][0]["message"]
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            text = msg.get("content") or ""
            break
        for call in calls:
            args = json.loads(call["function"].get("arguments") or "{}")
            if call["function"]["name"] == "coding_recall":
                found = coding.recall(str(args.get("query", "")), session_id=sid)
            else:
                found = coding.show(sid, seq=int(args.get("seq", 0)))
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(found, default=str)[:6000]})
    m = re.search(r"\{.*\}", text, re.S)
    answers = json.loads(m.group(0)) if m else {}
    name = f"{method}+recall"
    (HERE / f"ctx_{name}.md").write_text(ctx)
    (HERE / f"answers_{name}.json").write_text(json.dumps(
        {"answers": answers, "turns": turns}, indent=1))
    print(f"{name}: {len(answers)} answers, {turns} turns")


def _private_keys(qa: list[dict]) -> None:
    """A withheld question's keys come from ``qa.private.json`` (not in git), if present."""
    private = HERE / "qa.private.json"
    if private.is_file():
        keys = {q["id"]: q["keys"] for q in json.loads(private.read_text())}
        for q in qa:
            q["keys"] = keys.get(q["id"], q["keys"])


STOP = {
    "what", "which", "when", "where", "why", "how", "who", "whom", "does", "did", "do", "the",
    "a", "an", "of", "to", "in", "on", "for", "from", "and", "or", "was", "were", "is", "are",
    "be", "been", "with", "that", "this", "these", "those", "than", "then", "into", "about",
    "after", "before", "by", "as", "at", "it", "its", "user", "users", "continuum", "ask",
    "asked", "want", "wanted", "number", "name", "named", "kind", "roughly", "besides",
    "according", "first", "second", "their", "there", "they", "them", "said", "say", "says",
    "make", "made", "used", "use", "using", "get", "got", "two", "one", "being", "under",
    "over", "also", "only", "most", "more", "much", "many", "some", "any", "each", "every",
    "other", "others",
}


def _covered(text: str, q: dict, window: int = 500) -> bool:
    """The answer keys stated within ``window`` characters of at least two of the
    question's own topic words (so a stray number elsewhere does not count)."""
    topic = [w for w in re.findall(r"[a-z][a-z0-9\-]{2,}", q["q"].lower()) if w not in STOP]
    groups = q["keys"]
    for k in groups[0]:
        for m in re.finditer(r"(?<![\w.])" + re.escape(k.lower()) + r"(?![\w])", text):
            near = text[max(0, m.start() - window): m.end() + window]
            rest = all(any(re.search(r"(?<![\w.])" + re.escape(x.lower()) + r"(?![\w])",
                                     near) for x in g) for g in groups[1:])
            if rest and sum(1 for w in topic if w[:6] in near) >= min(2, len(topic)):
                return True
    return False


def coverage() -> None:
    """Offline score, no model: a proxy for what a reader could answer. It agreed with
    the reader's ranking on the earlier runs (the rule-based brief lowest, the model
    memory highest) but is not a substitute for ``answer``."""
    qa = json.loads((HERE / "qa.json").read_text())
    _private_keys(qa)
    qa = [q for q in qa if q["keys"]]
    for f in sorted(HERE.glob("ctx_*.md")):
        text = f.read_text()[:BUDGET_CHARS].lower()
        hits = sum(_covered(text, q) for q in qa)
        print(f"{f.stem.removeprefix('ctx_'):<18} {hits:>2}/{len(qa)}")


def score() -> None:
    qa = json.loads((HERE / "qa.json").read_text())
    _private_keys(qa)
    qa = [q for q in qa if q["keys"]]          # withheld and no private copy: not scored
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
        build(sys.argv[2], *(sys.argv[3:4]))
    elif cmd == "coverage":
        coverage()
    elif cmd == "clean-copy":
        clean_copy(sys.argv[2], sys.argv[3])
    elif cmd == "recall":
        answer_recall(sys.argv[2], sys.argv[3])
    elif cmd == "answer":
        answer(sys.argv[2], *(sys.argv[3:4]))
    else:
        score()
