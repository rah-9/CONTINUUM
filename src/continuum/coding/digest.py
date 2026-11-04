# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Session digest: a compressed memory of the whole session built without any model.

Extractive and deterministic: every line is text from the journal with its event number
(``[#N]``), so nothing is paraphrased or invented, and it costs nothing to build. It
covers the work as well as the chat. The algorithm is documented, with its measurements,
in docs/MEMORY-ALGORITHM.md §3.4c:

1. **Units.** The history (``working_memory.history_lines``) is split into turns at each
   user message. Each turn yields units: the user's sentences (run-on speech by clause)
   and pastes (summarised by type), answers to questions, the agent's prose by sentence
   (announcements ending in ":" dropped), and from tool calls only outcomes: changed
   files (one line per turn), commits, pull requests, test and build summaries, failures
   (linked to a later success of the same command), unknown outcomes.
2. **Salience from structure and statistics, never keyword lists:** the kind of unit,
   its specificity (numbers, paths, identifiers, code spans) and its rarity (mean IDF of
   its rarest words over the session).
3. **Always kept:** the best sentence of each user message (chatter excepted), pastes,
   answers and unknown outcomes, up to MUST_SHARE of the room; beyond that the least
   informative go first, never the first request (the goal) or the latest.
4. **The rest, globally** by value per character with diminishing returns per turn and
   a boost for the newest turns, skipping a unit that repeats a chosen one's wording
   without naming anything new.
5. **Printed** turn by turn in journal order; every line and heading is counted, so the
   budget is never exceeded and no line is cut.

``build(..., user=False)`` leaves the user's own words out, for the brief, where
``memory.consolidate`` already shows them.
"""

from __future__ import annotations

import heapq
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from continuum.coding import segments

DIGEST_CHARS = 24_000      # as measured in eval/compression_test
BRIEF_CHARS = 16_000       # in the brief: the work, beside the user's words (~4k tokens)
CLAUSE_CHARS = 200          # a run-on sentence is split into clauses of at most this
UNIT_CHARS = 320            # one unit at most (a long sentence keeps its head)
RECENT_TURNS = 0.2          # the newest fifth of the turns gets extra room
RECENT_BOOST = 1.6
DENSITY_PAD = 60            # per-unit overhead in the value-per-character ranking
TURN_DECAY = 0.9            # each further pick from the same turn is worth this much less

_SPECIFIC = re.compile(
    r"#\d+|\bv?\d+(?:[.,:/x-]\d+)*%?|\$\d|`[^`]{2,60}`|\b[\w.-]+/[\w./-]+|"
    r"\b[a-z]+_[a-z0-9_]+\b|\b[a-z]+[A-Z]\w+\b|\b[A-Z]{2,}[\w-]*\b|\b\w+\.(?:py|md|ts|js|json|"
    r"sql|toml|yaml|yml|sh|go|rs|java|txt)\b")
# a sentence ends at . ! ? (or the CJK marks) before anything but a lowercase Latin letter
# ("e.g. this" stays whole), or at a line break
_SENT = re.compile(r"(?<=[.!?\u0964\u061f\u06d4\u3002\uff01\uff1f])\s+(?=[^a-z\s])"
                   r"|(?<=[\u3002\uff01\uff1f])|\n+")
_COMMIT = re.compile(r"git commit[^\n]*?-m\s+(?:\"([^\"]+)\"|'([^']+)'|\"\$\(cat <<'?EOF'?\s*(.+))",
                     re.S)
_CLAUSE = re.compile(r"(?<=[,;])\s+|\s+(?=(?:and|but|so|then|because|also)\s)")
_PREFIX = re.compile(r"^#(\d+) (USER|ASSISTANT|TOOL|OUTCOME UNKNOWN|AGENT NOTE|CHECK)[: ]\s?")

MUST = ("request", "answer", "paste", "unknown")   # the user's words, decisions, pastes,
                                                   # and calls whose outcome is unknown
MUST_SHARE = 0.6            # the most of the room the always-kept units may take

PRIOR = {"request": 3.0, "user": 2.5, "answer": 3.0, "paste": 3.0, "report": 2.2,
         "outcome": 1.6, "edit": 1.6, "result": 1.4, "failure": 1.3, "note": 1.6, "unknown": 3.0,
         "agent": 1.0}


@dataclass
class Unit:
    seq: int
    kind: str
    text: str
    turn: int
    score: float = 0.0


_LATIN_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")


def _words(text: str) -> list[str]:
    """Words for rarity and overlap: Latin-script words (letters, digits, underscores),
    and whole whitespace tokens of any other script (a regex word class would split
    Devanagari at its vowel signs, and CJK has no spaces to split on)."""
    low = text.lower()
    out = _LATIN_WORD.findall(low)
    for tok in low.split():
        if not tok.isascii():
            tok = tok.strip(".,;:!?()[]{}<>\"'`*|=+-\u0964\u061f\u3002\uff0c\uff01\uff1f")
            if len(tok) >= 2 and any(c.isalpha() and not c.isascii() for c in tok):
                out.append(tok)
    return out


def _clip(text: str, limit: int = UNIT_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _sentences(text: str) -> list[str]:
    """Sentences, bullets and table rows; a run-on sentence (voice typing, no full stops)
    is split further at clause boundaries into pieces of at most CLAUSE_CHARS."""
    out = []
    for part in _SENT.split(text):
        part = part.strip(" -*|>\t")
        if len(part) < 12 or set(part) <= set("-|: "):
            continue
        if len(part) <= CLAUSE_CHARS:
            out.append(part)
            continue
        piece = ""
        for clause in _CLAUSE.split(part):
            if piece and len(piece) + len(clause) + 1 > CLAUSE_CHARS:
                out.append(piece)
                piece = ""
            piece = f"{piece} {clause}".strip() if piece else clause
        if len(piece) >= 12:
            out.append(piece)
    return out


_EDIT_TOOL = re.compile(r"(?i)edit|write|patch|replace|create_file|str_replace")
_PATH_KEY = re.compile(r'"(?:file_path|path|filePath|notebook_path|target_file)":\s*"([^"]+)"')
# the closing summary of a test, build or lint run: counts with nouns at the very end
# ("214 passed, 2 failed in 41.0s", "Found 3 errors.", "12 files changed")
_SUMMARY = re.compile(r"((?:\b\d+(?:\.\d+)?\s+[a-z]+(?:[,;]\s*|\s+and\s+|\s+)?){1,6}"
                      r"(?:\bin\s+[\d.]+\s*m?s)?)[\W_]*$")
_ERROR_LINE = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Exception)|FAILED|panic|error)\b[:\s]")


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
            number = f" #{url.group(1)}" if url else ""
            return "outcome", f"PR{number} opened: {title.group(1)}"
    if "merge_pull_request" in tool and status == "ok":
        num = re.search(r'"pullNumber":\s*(\d+)', what)
        return "outcome", f"PR #{num.group(1) if num else '?'} merged"
    commit = _COMMIT.search(what)
    if commit and status == "ok":
        msg = next(g for g in commit.groups() if g)
        return "outcome", "Commit: " + msg.strip().splitlines()[0]
    path = _PATH_KEY.search(what)
    if path and status == "ok" and _EDIT_TOOL.search(tool):
        return "edit", path.group(1)
    if status in ("error", "timeout", "interrupted"):
        # the line that names the error, else the end of the output
        err = [ln for ln in re.split(r"(?<=\S)\s{2,}|\n| (?=FAILED )", result)
               if _ERROR_LINE.search(ln)]
        closing = _SUMMARY.search(result[-200:])
        tail = (err[-1] if err else closing.group(1) if closing and len(
            closing.group(1).split()) >= 2 else result)[-200:].strip()
        return "failure", f"Failed ({status}): {_clip(what, 120)}" + (f" => {tail}" if tail else "")
    summary = _SUMMARY.search(result[-200:]) if status == "ok" else None
    if summary and len(summary.group(1).split()) >= 2:
        return "result", f"{_clip(what, 100)} => {summary.group(1).strip()}"
    return None


def _command(body: str) -> str:
    """The call's identity for linking a failure to a later success: tool and input."""
    m = re.match(r"(\S+) \[\w+\]: (.*?)(?: => |$)", body, re.S)
    return f"{m.group(1)} {m.group(2).strip()}" if m else ""


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
    edits: dict[int, list[tuple[int, str]]] = {}
    failed: dict[str, Unit] = {}              # a command that failed and may pass later
    turn = 0
    for seq, line in rows:
        m = _PREFIX.match(line)
        if not m:
            continue
        who, body = m.group(2), line[m.end():]
        if who == "USER":
            turn += 1
            out.extend(_user_units(seq, body, turn))
        elif who == "ASSISTANT":
            kind = "report" if last_agent.get(turn) == seq else "agent"
            for seg in segments.segment(body):
                if seg.kind != "prose":
                    continue                  # the agent's own pasted code and logs
                for sent in _sentences(seg.text):
                    # a sentence that ends in ":" announces the next step (an intention,
                    # not a result); a question to the user is answered by the user
                    end = sent.rstrip("*` ")
                    if end.endswith(":") or (kind == "agent" and end.endswith("?")):
                        continue
                    out.append(Unit(seq, kind, _clip(sent), turn))
        elif who == "TOOL":
            found = _tool_unit(body)
            if found is None:
                continue
            kind, text = found
            if kind == "answer":
                # each answer is one of the user's decisions: one unit per question
                out.extend(Unit(seq, "answer", _clip("User decided: " + part), turn)
                           for part in text.split(" | "))
            elif kind == "edit":
                edits.setdefault(turn, []).append((seq, text))
            elif kind == "failure":
                unit = Unit(seq, "failure", _clip(text), turn)
                failed[_command(body)] = unit
                out.append(unit)
            else:
                if kind == "result" and _command(body) in failed:
                    before = failed.pop(_command(body))
                    before.text = _clip(f"{before.text} (passed at #{seq})")
                out.append(Unit(seq, kind, _clip(text), turn))
        elif who == "CHECK":
            out.append(Unit(seq, "result", _clip("Check " + body), turn))
        elif who == "OUTCOME UNKNOWN":
            # a call that may have acted outside: the next model must not repeat it
            out.append(Unit(seq, "unknown", _clip("Outcome unknown: " + body), turn))
        elif who == "AGENT NOTE":
            out.append(Unit(seq, "note", _clip(body), turn))
    for t, files in edits.items():
        # one line per turn: every file the turn changed, in order, each named once
        names = list(dict.fromkeys(f for _, f in files))
        shown = ", ".join(names[:12]) + (f" (+{len(names) - 12} more)" if len(names) > 12
                                         else "")
        out.append(Unit(files[0][0], "edit", _clip("Changed: " + shown, UNIT_CHARS * 2), t))
    return out


def _user_units(seq: int, body: str, turn: int) -> list[Unit]:
    """The user's message by sentence (clauses for run-on speech), and anything pasted (a
    stack trace, a diff, JSON, a log, code) as one unit: whole when short, else summarised
    by type (an error signature, keys, line counts). The most informative sentence of
    each message becomes its ``request`` after scoring (``_mark_requests``)."""
    parts = segments.segment(body)
    prose = " ".join(" ".join(p.text.split()) for p in parts if p.kind == "prose")
    pieces = _sentences(prose) or ([prose] if prose.strip() else [])
    out = [Unit(seq, "user", _clip(x), turn) for x in pieces]
    for p in parts:
        if p.kind == "prose":
            continue
        flat = " ".join(p.text.split())
        text = flat if len(flat) <= UNIT_CHARS else (p.summary or flat)
        # pasted by the user: their data, kept like their words
        out.append(Unit(seq, "paste", _clip(f"Pasted {p.kind}: {text}"), turn))
    return out


def _chatter(text: str) -> bool:
    """A reply with nothing to remember: a few words, no number, path or identifier."""
    return len(text.split()) < 6 and not _SPECIFIC.search(text)


def _mark_requests(all_units: list[Unit]) -> None:
    """The best-scoring sentence of each user message is its request: always kept,
    unless the whole message is chatter ("ok continue")."""
    best: dict[int, Unit] = {}
    for u in all_units:
        if u.kind == "user" and (u.seq not in best or u.score > best[u.seq].score):
            best[u.seq] = u
    for u in best.values():
        if not _chatter(u.text):
            u.kind = "request"


def _score(all_units: list[Unit]) -> None:
    docs = [set(_words(u.text)) for u in all_units]
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
        # a fragment ranks low, unless it is short because it is dense ("Changed: a.py")
        short = 1.0 if specific else min(1.0, len(u.text) / 60)
        u.score = PRIOR.get(u.kind, 1.0) * (0.4 + rarity) * (0.5 + specific) * short


def _shingles(text: str) -> set[tuple[str, ...]]:
    w = _words(text)
    return {tuple(w[i:i + 3]) for i in range(max(1, len(w) - 2))}


def _specifics(text: str) -> set[str]:
    return {m.lower() for m in _SPECIFIC.findall(text)}


@dataclass
class _Seen:
    shingles: set[tuple[str, ...]]
    specifics: set[str]


def _redundant(u: Unit, seen: list[_Seen]) -> bool:
    """Says the same as something already chosen: most of its wording overlaps AND it
    names no number, path or identifier the overlapping lines do not. "p95 fell from 840
    to 120 ms" and a later "p95 fell from 610 to 95 ms" are two facts, not one."""
    sh, sp = _shingles(u.text), _specifics(u.text)
    covered: set[str] = set()
    overlapping = False
    for s in seen:
        if len(sh & s.shingles) > 0.5 * max(1, min(len(sh), len(s.shingles))):
            overlapping = True
            covered |= s.specifics
    return overlapping and sp <= covered


def _cost(units: list[Unit], heading: int) -> int:
    """Characters these units take when printed: each line, each turn's heading once."""
    return sum(_line_cost(u) for u in units) + heading * len({u.turn for u in units})


def _fit_must(must: list[Unit], room: int, heading: int) -> tuple[list[Unit], int]:
    """The user's words and decisions are kept, but they may not crowd out everything
    else: above MUST_SHARE of the room, the least informative of them are left out (and
    counted), the newer ones last, never the first (it states the goal) or the latest."""
    limit = int(room * MUST_SHARE)
    if _cost(must, heading) <= limit:
        return must, 0
    n = len(must)
    # value per character, with a mild preference for newer lines
    # requests go first, then pastes, answers to questions last (they are explicit
    # decisions); within a tier by value per character, with newer lines kept longer
    tier = {"request": 0, "paste": 1, "answer": 2, "unknown": 3}
    rank = sorted(range(n), key=lambda i: (
        tier.get(must[i].kind, 0), must[i].score * (1 + 0.5 * i / max(1, n - 1))
        / (len(must[i].text) + DENSITY_PAD)))
    out = set(range(n))
    turns = Counter(u.turn for u in must)
    used = _cost(must, heading)
    dropped = 0
    for i in rank:
        if used <= limit:
            break
        if i in (0, n - 1):
            continue                      # the first (the goal) and the latest stay
        out.discard(i)
        turns[must[i].turn] -= 1
        used -= _line_cost(must[i]) + (heading if turns[must[i].turn] == 0 else 0)
        dropped += 1
    return [must[i] for i in sorted(out)], dropped


def build(lines: Iterable[tuple[int, str]], budget: int = DIGEST_CHARS, *,
          user: bool = True) -> str:
    """The digest of ``lines`` in at most ``budget`` characters (Markdown). ``user=False``
    leaves out the user's own words and pastes (shown elsewhere in the brief); their
    answers to questions and the unknown outcomes stay."""
    cands = units(lines)
    if not cands or budget < 80:
        return ""
    _score(cands)
    _mark_requests(cands)
    if not user:
        cands = [u for u in cands if u.kind not in ("request", "user", "paste")]
        if not cands:
            return ""
    # the longest heading this session can print: "### Turn T (#first to #last)\n"
    top_seq = max(u.seq for u in cands)
    heading = len(f"### Turn {max(u.turn for u in cands)} (#{top_seq} to #{top_seq})") + 1
    turns: dict[int, list[Unit]] = {}
    for u in cands:
        turns.setdefault(u.turn, []).append(u)
    order = sorted(turns)
    recent_from = order[int(len(order) * (1 - RECENT_TURNS))] if order else 0
    room = budget - 100                                # the "left out" note
    chosen, dropped = _fit_must([u for u in cands if u.kind in MUST], room, heading)
    seen = [_Seen(_shingles(u.text), _specifics(u.text)) for u in chosen]
    picks: Counter[int] = Counter(u.turn for u in chosen)
    # exact cost: each line plus its "- [#N] " prefix, each turn's heading once
    used = _cost(chosen, heading)
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
        cost = _line_cost(u) + (heading if picks[u.turn] == 0 else 0)
        if used + cost > room or _redundant(u, seen):
            continue
        seen.append(_Seen(_shingles(u.text), _specifics(u.text)))
        chosen.append(u)
        used += cost
        picks[u.turn] += 1
    blocks: list[str] = []
    if dropped:
        blocks.append(f"- ({dropped} less specific line(s) of the user's left out for "
                      "space: `coding_recall` finds them)")
    position = {id(u): i for i, u in enumerate(cands)}
    by_turn: dict[int, list[Unit]] = {}
    for u in chosen:
        by_turn.setdefault(u.turn, []).append(u)
    for t in order:
        mine = sorted(by_turn.get(t, []), key=lambda x: position[id(x)])
        if not mine:
            continue
        blocks.append(f"### Turn {t} (#{mine[0].seq} to #{mine[-1].seq})")
        blocks.extend(f"- [#{u.seq}] {_label(u)}{u.text}" for u in mine)
    out = "\n".join(blocks)
    if len(out) > budget:                              # never cut a line in half
        out = out[:budget].rsplit("\n", 1)[0]
    return out


def _line_cost(u: Unit) -> int:
    return len(u.text) + len(_label(u)) + len(str(u.seq)) + 7


def _label(u: Unit) -> str:
    return {"request": "USER: ", "user": "USER: ", "paste": "USER ", "note": "NOTE: ",
            "unknown": "⚠ "}.get(
        u.kind, "")
