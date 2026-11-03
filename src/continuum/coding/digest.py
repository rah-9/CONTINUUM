# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Session digest: a compressed memory of the whole session built without any model.

The digest is extractive: every line in it is text from the journal (the user's words,
sentences of the agent's reports, commit subjects, pull requests, failures, the user's
answers to questions), with its event number. Nothing is paraphrased, so nothing can be
invented, and it costs nothing to build.

Algorithm (deterministic, linear in the journal size):

1. **Turns.** The history is split at each user message: one turn is a request and all
   the work and reports that followed it.
2. **Units.** Each turn yields candidate units: the user's message (whole when short,
   else by sentence); the agent's messages by sentence, bullet or table row; from tool
   calls only what states an outcome: commit subjects, pull requests opened or merged,
   the user's answers to questions, and failures with the end of their error.
3. **Salience, from structure and statistics only** (no keyword lists):
   * *specificity*: numbers, versions, ``#N`` references, paths, identifiers, code spans;
   * *distinctiveness*: the mean inverse document frequency of the unit's words over the
     whole session, so a unit naming something rare outranks generic narration;
   * *source*: the user's words first, then the agent's last report of a turn (its
     summary of what happened), then other agent text and tool outcomes.
4. **Redundancy.** A unit whose word shingles overlap an already chosen unit by more
   than half is skipped, keeping the earliest specific statement of a fact.
5. **Packing across time.** Every turn gets a share of the budget that grows with the
   square root of how much happened in it, with extra room for the newest turns, so a
   long session is covered from its first request to its last instead of only its end.
   Within a turn the highest-scoring units are taken until its share is used, then
   printed in journal order.

Measured in ``eval/compression_test`` against the same budget as every other method.
"""

from __future__ import annotations

import heapq
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

DIGEST_CHARS = 24_000      # as measured in eval/compression_test
BRIEF_CHARS = 16_000       # in the brief, beside the user's words and the facts (~4k tokens)
UNIT_CHARS = 320            # one unit at most (a long sentence keeps its head)
USER_HEAD = 160             # the opening of each user message, always kept
RECENT_TURNS = 0.2          # the newest fifth of the turns gets extra room
RECENT_BOOST = 1.6
DENSITY_PAD = 60            # per-unit overhead in the value-per-character ranking
TURN_DECAY = 0.9            # each further pick from the same turn is worth this much less

_SPECIFIC = re.compile(
    r"#\d+|\bv?\d+(?:[.,:/x-]\d+)*%?|\$\d|`[^`]{2,60}`|\b[\w.-]+/[\w./-]+|"
    r"\b[a-z]+_[a-z0-9_]+\b|\b[a-z]+[A-Z]\w+\b|\b[A-Z]{2,}[\w-]*\b|\b\w+\.(?:py|md|ts|js|json|"
    r"sql|toml|yaml|yml|sh|go|rs|java|txt)\b")
_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`*(\[])|\n+")
_COMMIT = re.compile(r"git commit[^\n]*?-m\s+(?:\"([^\"]+)\"|'([^']+)'|\"\$\(cat <<'?EOF'?\s*(.+))",
                     re.S)
_PREFIX = re.compile(r"^#(\d+) (USER|ASSISTANT|TOOL|OUTCOME UNKNOWN|AGENT NOTE|CHECK)[: ]\s?")

MUST = ("request", "answer")   # never dropped for space: the user's words and decisions

PRIOR = {"request": 3.0, "user": 3.0, "answer": 3.0, "report": 2.2, "outcome": 1.6, "agent": 1.0,
         "failure": 1.3, "note": 1.2}


@dataclass
class Unit:
    seq: int
    kind: str
    text: str
    turn: int
    score: float = 0.0


def _clip(text: str, limit: int = UNIT_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _sentences(text: str) -> list[str]:
    out = []
    for part in _SENT.split(text):
        part = part.strip(" -*|>\t")
        if len(part) >= 12 and not set(part) <= set("-|: "):
            out.append(part)
    return out


def _tool_unit(body: str) -> tuple[str, str] | None:
    """What a tool call says about the outcome of the work, if anything."""
    m = re.match(r"(\S+) \[(\w+)\]: (.*?)(?: => (.*))?$", body, re.S)
    if not m:
        return None
    tool, status, what, result = m.group(1), m.group(2), m.group(3), m.group(4) or ""
    if tool == "AskUserQuestion" and result:
        pairs = re.findall(r'"([^"]{8,}?\?)"="([^"]+)"', result)
        if pairs:
            return "answer", " | ".join(f"{q} -> {a}" for q, a in pairs)
        return "answer", "User answered: " + result[-UNIT_CHARS:]
    if "create_pull_request" in tool:
        title = re.search(r'"title":\s*"([^"]+)"', what)
        url = re.search(r"/pull/(\d+)", result)
        if title:
            return "outcome", f"PR {('#' + url.group(1)) if url else ''} opened: {title.group(1)}"
    if "merge_pull_request" in tool and status == "ok":
        num = re.search(r'"pullNumber":\s*(\d+)', what)
        return "outcome", f"PR #{num.group(1) if num else '?'} merged"
    commit = _COMMIT.search(what)
    if commit and status == "ok":
        msg = next(g for g in commit.groups() if g)
        return "outcome", "Commit: " + msg.strip().splitlines()[0]
    if status in ("error", "timeout", "interrupted") and result:
        return "failure", f"Failed: {_clip(what, 120)} => {result[-200:]}"
    return None


def units(lines: Iterable[tuple[int, str]]) -> list[Unit]:
    """Candidate units from ``working_memory.history_lines`` output, with turn numbers."""
    rows = list(lines)
    # the agent's last message before the next user message is its report for the turn
    last_agent: dict[int, int] = {}
    turn = 0
    for seq, line in rows:
        m = _PREFIX.match(line)
        if not m:
            continue
        if m.group(2) == "USER":
            turn += 1
        elif m.group(2) == "ASSISTANT":
            last_agent[turn] = seq
    out: list[Unit] = []
    turn = 0
    for seq, line in rows:
        m = _PREFIX.match(line)
        if not m:
            continue
        who, body = m.group(2), line[m.end():]
        if who == "USER":
            turn += 1
            # the opening of every user message is always kept (it states the request);
            # the rest competes by salience like everything else
            text = " ".join(body.split())
            cut = len(text) if len(text) <= USER_HEAD else (
                text.rfind(" ", 0, USER_HEAD) if " " in text[:USER_HEAD] else USER_HEAD)
            out.append(Unit(seq, "request", text[:cut] + ("…" if cut < len(text) else ""),
                            turn))
            out.extend(Unit(seq, "user", _clip(s), turn) for s in _sentences(text[cut:]))
        elif who == "ASSISTANT":
            kind = "report" if last_agent.get(turn) == seq else "agent"
            for sent in _sentences(body):
                # a sentence that ends in ":" announces the next step (an intention, not a
                # result), and a question to the user is answered by the user's next words
                if kind == "agent" and sent.rstrip("*` ").endswith((":", "?")):
                    continue
                out.append(Unit(seq, kind, _clip(sent), turn))
        elif who == "TOOL":
            found = _tool_unit(body)
            if found and found[0] == "answer":
                # each answer is one of the user's decisions: one unit per question
                out.extend(Unit(seq, "answer", _clip("User decided: " + part), turn)
                           for part in found[1].split(" | "))
            elif found:
                out.append(Unit(seq, found[0], _clip(found[1]), turn))
        elif who in ("AGENT NOTE", "OUTCOME UNKNOWN"):
            out.append(Unit(seq, "note", _clip(body), turn))
    return out


def _score(all_units: list[Unit]) -> None:
    docs = [set(_WORD.findall(u.text.lower())) for u in all_units]
    df: Counter[str] = Counter(w for d in docs for w in d)
    n = max(1, len(docs))
    idf = {w: math.log((n + 1) / (c + 0.5)) for w, c in df.items()}
    top = max(idf.values(), default=1.0)
    for u, words in zip(all_units, docs, strict=True):
        if not words:
            u.score = 0.0
            continue
        distinct = sorted((idf[w] for w in words), reverse=True)[:12]
        rarity = sum(distinct) / len(distinct) / top                      # 0..1
        specific = min(len(_SPECIFIC.findall(u.text)), 6) / 6             # 0..1
        short = min(1.0, len(u.text) / 60)                                # fragments rank low
        u.score = PRIOR.get(u.kind, 1.0) * (0.4 + rarity) * (0.5 + specific) * short


def _shingles(text: str) -> set[tuple[str, ...]]:
    w = _WORD.findall(text.lower())
    return {tuple(w[i:i + 3]) for i in range(max(1, len(w) - 2))}


def build(lines: Iterable[tuple[int, str]], budget: int = DIGEST_CHARS) -> str:
    """The digest of ``lines`` in at most ``budget`` characters (Markdown)."""
    cands = units(lines)
    if not cands:
        return ""
    _score(cands)
    turns: dict[int, list[Unit]] = {}
    for u in cands:
        turns.setdefault(u.turn, []).append(u)
    order = sorted(turns)
    recent_from = order[int(len(order) * (1 - RECENT_TURNS))] if order else 0
    room = budget - 40 * len(order)                    # turn headings
    chosen: list[Unit] = [u for u in cands if u.kind in MUST]
    seen = [_shingles(u.text) for u in chosen]
    used = sum(len(u.text) + 12 for u in chosen)
    picks: Counter[int] = Counter(u.turn for u in chosen)
    # global greedy by value per character, with diminishing returns inside a turn
    # (submodular coverage): a turn full of facts can take more room, yet every turn is
    # reached before any one turn is exhausted
    pool = [u for u in cands if u.kind not in MUST]
    base = {id(u): u.score * (RECENT_BOOST if u.turn >= recent_from else 1.0)
            / (len(u.text) + DENSITY_PAD) for u in pool}
    heap = [(-base[id(u)], i, 0, u) for i, u in enumerate(pool)]
    heapq.heapify(heap)
    while heap and used < room:
        _, i, stamp, u = heapq.heappop(heap)
        if stamp != picks[u.turn]:                     # stale: re-rank with today's decay
            heapq.heappush(heap, (-base[id(u)] * TURN_DECAY ** picks[u.turn], i,
                                  picks[u.turn], u))
            continue
        cost = len(u.text) + 12
        if used + cost > room:
            continue
        sh = _shingles(u.text)
        if any(len(sh & s) > 0.5 * min(len(sh), len(s)) for s in seen):
            continue
        seen.append(sh)
        chosen.append(u)
        used += cost
        picks[u.turn] += 1
    blocks: list[str] = []
    position = {id(u): i for i, u in enumerate(cands)}
    for t in order:
        mine = sorted((u for u in chosen if u.turn == t), key=lambda x: position[id(x)])
        if not mine:
            continue
        blocks.append(f"### Turn {t} (#{mine[0].seq} to #{mine[-1].seq})")
        blocks.extend(f"- [#{u.seq}] {_label(u)}{u.text}" for u in mine)
    return "\n".join(blocks)[:budget]


def _label(u: Unit) -> str:
    return {"request": "USER: ", "user": "USER (cont.): ", "note": "NOTE: "}.get(u.kind, "")
