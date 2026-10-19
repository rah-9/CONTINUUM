# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Conversation consolidation: what a long chat must hand to the next model, in a budget.

Keeping "the first and the newest few messages" loses everything said in the middle of a
long chat: measured on 40-message conversations, the v0.8.0 brief kept 18% of the user's
explicit requirements (``continuum_lab.memory_bench``). This module instead reads *every*
captured message and keeps what is still binding, deterministically and without a model:

1. **Requirements ledger.** Each user message is split into sentences; prose is separated
   from pasted material (code blocks, stack traces, log lines, diffs), which is never
   mistaken for a requirement. A sentence is kept when it carries a requirement cue
   (``must``, ``never``, ``make sure``, ``default``, ``only``, ``at most`` ...), revises an
   earlier decision (``actually``, ``instead of``, ``change of plan`` ...), or reads like a
   spec line (``Page size: 25 items``).
2. **Answers.** A short reply ("yes", "go ahead", "no") is meaningless alone. When the
   agent's message right before it asked a question, the pair is kept: *Asked "…?" →
   "yes"*.
3. **Agent decisions.** Sentences in the agent's captured reports that state a choice or a
   caveat (``I chose``, ``because``, ``instead of``, ``not yet`` ...) are kept as claims, so a
   model that never wrote a note still hands over its reasoning.
4. **Budget.** The newest message is always whole (it is the current instruction), the
   first message gives the goal, and the ledger fills the rest, newest and strongest
   first, then shown in conversation order with message numbers, so "newer wins" can be
   applied. Every omission is counted. ``coding_recall`` finds the rest.

The rules are lexical, so a requirement phrased with no cue at all ("blue please") is not
in the ledger; it is still in the journal and in recall, and the newest messages are shown
whole. ``continuum_lab.memory_bench`` measures each kind separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# --- budgets (characters; ~4 per token) --------------------------------------------------
LATEST_CHARS = 2000      # the current instruction, whole
FIRST_CHARS = 700        # the goal
LEDGER_CHARS = 4400      # requirements, revisions, answers and other statements ...
LEDGER_PER_MESSAGE = 50  # ... plus this much per user message beyond 40 ...
LEDGER_MAX_CHARS = 10000  # ... up to this cap (~2.5k tokens) however long the chat
ITEM_CHARS = 320         # one ledger sentence
RECENT_CHARS = 500       # one recent message shown whole if budget remains
RECENT_MESSAGES = 2
DECISION_ITEMS = 8       # agent decisions (claims), plus one per 40 reports ...
DECISION_MAX_ITEMS = 14  # ... up to this many
DECISION_CHARS = 260
PER_MESSAGE_ITEMS = 6    # a long message cannot crowd out the rest of the chat

_STRONG = re.compile(
    r"\b(must|mustn'?t|never|always|do not|don'?t|make sure|ensure|required?|requirements?|"
    r"important|critical|from now on|no longer|only|at most|at least|cannot|can'?t|shall|"
    r"exactly|forbidden|not allowed|remember)\b", re.I)
_MEDIUM = re.compile(
    r"\b(should(?:n'?t)?|need(?:s)? to|have to|has to|avoid|prefer|keep|instead of|"
    r"rather than|please|limit(?:ed)?|max(?:imum)?|min(?:imum)?|default|by default|"
    r"capped|up to|no more than|use only|stick to)\b", re.I)
_REVISION = re.compile(
    r"\b(actually|change of plan|instead of|instead|no longer|scratch that|forget (?:that|"
    r"what)|ignore (?:that|what|my)|not anymore|switch(?:ed)? to|correction|on second "
    r"thought|changed my mind|rather than|replace)\b", re.I)
_SPEC = re.compile(r"^[A-Z`][\w `/().-]{1,40}:\s+\S.{0,120}$")
# Wishes and conventions ("I'd like ...", "let's stick with ...", "X, not Y", "No emojis").
_PREFERENCE = re.compile(
    r"\b(I'?d like|I would like|I want|we want|we need|I need|let'?s (?:use|keep|stick|go|"
    r"make|do)|stick with|go with|we'?re (?:targeting|using)|targeting|across the board|"
    r"nothing (?:newer|older|else)|as a rule|convention)\b|,\s*not\s+\w|^no\s+\w+", re.I)
# A short statement of fact with a concrete value ("Timeouts are 500 ms.", "Errors go to
# stderr via `log`."): a spec, unless it is a question or a task step.
_VALUE = re.compile(r"\d|`[^`]+`|\"[^\"]+\"|'[^']+'|\b[\w.-]+/[\w./-]+")
_STATEMENT = re.compile(r"\b(is|are|goes|go|stays?|lives?|runs?|uses?|means?)\b", re.I)
# Not a requirement: pleasantries and reactions.
_CHATTER = re.compile(
    r"^(looks good|looks great|nice|great|thanks|thank you|thx|ok|okay|cool|perfect|"
    r"awesome|good job|well done|lgtm|sounds good|i tried it|ok, i tried it|"
    r"that works|works now|got it|sure)\b[^.!?]{0,30}[.!]?$", re.I)
_TASK_START = re.compile(
    r"^(?:(?:can|could|would) you|please )?(add|write|refactor|rename|speed|fix|implement|"
    r"create|build|make|update|remove|delete|run|check|look|try|show|explain|tell|why|"
    r"what|how|where|when|who|which|start|continue|carry|go|thanks|thank)\b", re.I)
_DECISION = re.compile(
    r"\b(I chose|I picked|I went with|I decided|decided to|chose|opted|switched to|"
    r"because|instead of|trade-?off|caveat|not yet|left out|skipped|TODO|known issue|"
    r"limitation|assum(?:e|ed|ption)|deliberately|on purpose|not (?:\w+ )?(?:handled|"
    r"supported|implemented|covered|tested|done)|unsupported|doesn'?t (?:handle|support)|"
    r"won'?t|warning|beware)\b|^note\b", re.I)
# Pasted material: never a requirement, even when it says "must".
_PASTED_LINE = re.compile(
    r"^\s*(Traceback|File \"|at [\w.$]+\(|[\w.]*(Error|Exception|Warning)\b[:(]|"
    r"(WARN(ING)?|ERROR|INFO|DEBUG|FATAL|CRITICAL)\b|\[\d|\d{4}-\d\d-\d\d[ T]\d\d:|"
    r"[+-]{3} |@@ |\$ |>>> |\t|\s{4,}\S)")
_FENCE = re.compile(r"```.*?(```|$)", re.S)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\"'(*\-])")
_AFFIRM = re.compile(
    r"^(y|yes|yep|yeah|sure|ok|okay|go ahead|do it|please do|yes please|ok,? do it|"
    r"sounds good|go for it|no|nope|don'?t|do not|not now|skip it|leave it)\b[\s.!]*",
    re.I)
# The user ending a chat with the previous agent ("stop here", "out of time", "the next
# model will do it"): addressed to that agent, not to the one that continues.
_HANDOFF = re.compile(
    r"\b(stop here|stop now|out of time|that'?s (?:all|it) for (?:now|today)|pause here|"
    r"next (?:model|agent|chat|session) (?:will|should|can)|leave (?:it|the rest) for|"
    r"switch(?:ing)? (?:to )?(?:another|a different|the next) (?:model|app|agent)|"
    r"hand(?:ing)? (?:it )?over|wrap (?:it )?up)\b", re.I)
_LOW_INFO = frozenset({
    "continue", "go on", "ok", "okay", "k", "yes", "y", "yep", "no", "n", "thanks",
    "thank you", "proceed", "go", "next", "do it", "keep going", "carry on", "go ahead",
    "sure", "continue where we left off", "continue where you left off", "resume", "done",
    "great", "nice", "cool", "good", "lgtm", "looks good", "thanks, looks good",
    "yes please", "ok, do it", "fix that", "fix that please", "go for it"})


@dataclass(frozen=True)
class Turn:
    seq: int            # journal order
    role: str           # user | agent
    text: str
    at: str
    source: str
    chat: str | None


def ends_chat(text: str) -> bool:
    """The message closes the chat for the agent it was sent to."""
    return bool(_HANDOFF.search(text))


def low_information(text: str) -> bool:
    return " ".join(text.lower().replace("!", " ").replace(".", " ").split()) in _LOW_INFO


def prose(text: str) -> list[str]:
    """The person's prose lines: code blocks, stack traces, logs and diffs removed."""
    lines = []
    in_paste = False
    for line in _FENCE.sub("\n", text).splitlines():
        stripped = line.strip()
        if not stripped:
            in_paste = False   # a blank line ends a pasted block
            continue
        if in_paste or _PASTED_LINE.match(line):
            in_paste = True    # output continues until a blank line
            continue
        letters = sum(c.isalpha() or c.isspace() for c in stripped)
        if len(stripped) > 12 and letters / len(stripped) < 0.6:  # mostly symbols
            continue
        lines.append(stripped)
    return lines


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in prose(text):
        line = re.sub(r"^([-*•]|\d+[.)])\s+", "", line)  # list markers
        out += [s.strip() for s in _SENTENCE.split(line) if s.strip()]
    return out


def classify(sentence: str) -> tuple[str, int] | None:
    """(kind, strength) for a sentence worth keeping, else None."""
    if len(sentence) < 8 or low_information(sentence) or _CHATTER.match(sentence):
        return None
    if _REVISION.search(sentence) and (_STRONG.search(sentence) or _MEDIUM.search(sentence)
                                       or re.search(r"\d|`", sentence)
                                       or re.search(r"\b(use|make|set)\b", sentence, re.I)):
        return "revision", 3
    if _STRONG.search(sentence):
        return "requirement", 2
    if _MEDIUM.search(sentence) or _SPEC.match(sentence) or _PREFERENCE.search(sentence):
        return "requirement", 1
    if (len(sentence) <= 100 and not sentence.rstrip().endswith("?")
            and not _TASK_START.match(sentence) and _VALUE.search(sentence)
            and _STATEMENT.search(sentence)):
        return "requirement", 1
    # Everything else the user stated (not a question, not chatter) is kept at the lowest
    # priority: recognizing what is NOT a requirement is more reliable than recognizing
    # every way of phrasing one. The budget drops this tier first, oldest first.
    if sentence.rstrip().endswith("?"):
        return None
    return "statement", 0


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w`]+", " ", text.lower()).split())


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + " …"


def _question(agent_text: str) -> str | None:
    """The last question the agent asked in a message."""
    asked = [s for s in sentences(agent_text) if s.rstrip().endswith("?")]
    return asked[-1] if asked else None


def consolidate(turns: list[Turn]) -> dict[str, Any]:
    """The user-side memory of the conversation, within fixed budgets (see module doc)."""
    users = [t for t in turns if t.role == "user"]
    agents = [t for t in turns if t.role == "agent"]
    out: dict[str, Any] = {"messages": len(users), "chats": _chats(turns), "first": None,
                           "ledger": [], "recent": [], "latest": None,
                           "omitted_messages": 0, "omitted_items": 0,
                           "agent_decisions": _decisions(agents)}
    if not users:
        return out
    number = {t.seq: i for i, t in enumerate(users, start=1)}
    latest = users[-1]
    out["latest"] = _shown(latest, number, LATEST_CHARS, verbatim=True)
    out["latest"]["ends_chat"] = ends_chat(latest.text)
    asked = _preceding_question(latest, turns)
    if asked and (low_information(latest.text) or _AFFIRM.match(latest.text)):
        out["latest"]["in_reply_to"] = _clip(asked, ITEM_CHARS)
    if len(users) > 1:
        out["first"] = _shown(users[0], number, FIRST_CHARS)

    # ledger candidates from every message except the latest (shown whole)
    candidates: list[dict[str, Any]] = []
    for u in users[:-1]:
        shown_first = u is users[0] and len(" ".join(u.text.split())) <= FIRST_CHARS
        items = []
        if _AFFIRM.match(u.text.strip()) and len(u.text) < 60:
            q = _preceding_question(u, turns)
            if q:
                items.append(("answer", 3, f'asked "{_clip(q, 200)}" → "{u.text.strip()}"'))
        if not shown_first:
            for sentence in sentences(u.text):
                found = classify(sentence)
                if found:
                    items.append((found[0], found[1], _clip(sentence, ITEM_CHARS)))
        for pos, (kind, strength, text) in enumerate(items[:PER_MESSAGE_ITEMS]):
            candidates.append({"n": number[u.seq], "pos": pos, "kind": kind,
                               "strength": strength, "text": text, "at": u.at,
                               "source": u.source})
        out["omitted_items"] += max(0, len(items) - PER_MESSAGE_ITEMS)

    # identical requirements repeated later: keep the newest occurrence
    seen: dict[str, dict[str, Any]] = {}
    for c in candidates:
        seen[_norm(c["text"])] = c
    candidates = sorted(seen.values(), key=lambda c: (c["n"], c["pos"]))
    # fill the budget: revisions/answers and strong cues first, newer before older
    ranked = sorted(candidates, key=lambda c: (c["strength"], c["n"]), reverse=True)
    budget = ledger_budget(len(users))
    chosen, used = [], 0
    for c in ranked:
        cost = len(c["text"]) + 12
        if used + cost > budget:
            out["omitted_items"] += 1
            continue
        chosen.append(c)
        used += cost
    out["ledger"] = sorted(chosen, key=lambda c: (c["n"], c["pos"]))

    # recent messages whole, if they carry content the ledger did not already keep
    in_ledger = {c["n"] for c in out["ledger"]}
    for u in reversed(users[1:-1]):
        if len(out["recent"]) >= RECENT_MESSAGES:
            break
        if low_information(u.text) or number[u.seq] in in_ledger or \
                not any(classify(s) for s in sentences(u.text)):
            continue   # questions, chatter and pasted output are not requirements
        out["recent"].insert(0, _shown(u, number, RECENT_CHARS))
    represented = in_ledger | {r["n"] for r in out["recent"]} | {1, len(users)}
    out["omitted_messages"] = len(users) - len(represented)
    return out


def readable(text: str) -> str:
    """A message as prose: pasted output (logs, traces, code) replaced by a short marker."""
    kept = prose(text)
    total = len([line for line in _FENCE.sub("\n<pasted>\n", text).splitlines()
                 if line.strip()])
    dropped = total - len(kept)
    body = " ".join(kept)
    return body + (f" [{dropped} pasted line(s) omitted]" if dropped > 0 else "")


def ledger_budget(messages: int) -> int:
    """Characters for the ledger: fixed for ordinary chats, growing slowly for very long
    ones, capped so that no chat makes the brief expensive."""
    return min(LEDGER_MAX_CHARS, LEDGER_CHARS + LEDGER_PER_MESSAGE * max(0, messages - 40))


def _shown(turn: Turn, number: dict[int, int], limit: int, *,
           verbatim: bool = False) -> dict[str, Any]:
    text = " ".join((turn.text if verbatim else readable(turn.text)).split())
    return {"n": number[turn.seq], "text": _clip(text, limit), "at": turn.at,
            "source": turn.source, "clipped": len(text) > limit}


def _preceding_question(user: Turn, turns: list[Turn]) -> str | None:
    """The question in the agent message directly before this user message, if any.

    "Before" is conversation order (``turns`` is sorted by when messages were written),
    not journal order: a transcript imported later (another app, a missed hook) has
    higher journal numbers for messages written earlier."""
    index = next((i for i, t in enumerate(turns) if t is user), None)
    before = turns[:index] if index is not None else [t for t in turns if t.seq < user.seq]
    for t in reversed(before):
        if t.role == "user":
            return None
        if t.role == "agent":
            return _question(t.text)
    return None


def _decisions(agents: list[Turn]) -> list[dict[str, Any]]:
    """Choices and caveats stated in the agent's reports, newest first, deduplicated."""
    limit = min(DECISION_MAX_ITEMS, DECISION_ITEMS + len(agents) // 40)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in reversed(agents):
        for s in reversed(sentences(t.text)):
            if len(s) < 15 or not _DECISION.search(s) or s.rstrip().endswith("?"):
                continue
            key = _norm(s)
            if key in seen:
                continue
            seen.add(key)
            out.append({"text": _clip(s, DECISION_CHARS), "at": t.at, "source": t.source})
            if len(out) >= limit:
                return list(reversed(out))
    return list(reversed(out))


def _chats(turns: list[Turn]) -> list[dict[str, Any]]:
    """Chats in order of first appearance: which app, how many user messages."""
    chats: dict[tuple[str, str | None], dict[str, Any]] = {}
    for t in turns:
        c = chats.setdefault((t.source, t.chat), {"source": t.source, "messages": 0})
        if t.role == "user":
            c["messages"] += 1
    return list(chats.values())
