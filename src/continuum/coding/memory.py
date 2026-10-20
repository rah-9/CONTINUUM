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
SHORT_REPLY_CHARS = 60   # longer messages are statements, even when they start with "ok"
PER_MESSAGE_ITEMS = 6    # a long message cannot crowd out the rest of the chat
PASTED_SHOWN = 5         # typed summaries of what the user pasted (errors first)
NEAR_DUPLICATE = 0.8     # word overlap above which a plain statement adds nothing new
INDEX_MIN_MESSAGES = 12  # shorter chats are shown whole enough without an index
INDEX_SPAN = 10          # messages per stretch of the chat index ...
INDEX_MAX_LINES = 12     # ... merged to at most this many lines
INDEX_TERMS = 4

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
    """The person's prose lines: code blocks, stack traces, logs, diffs, JSON and tables
    removed (they are typed segments, ``segments.py``)."""
    from continuum.coding.segments import segment

    lines = []
    for seg in segment(text):
        if seg.kind == "prose":
            lines += _prose_lines(seg.text)
    return lines


def _prose_lines(text: str) -> list[str]:
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


def consolidate(turns: list[Turn], context_terms: frozenset[str] = frozenset()
                ) -> dict[str, Any]:
    """The user-side memory of the conversation, within fixed budgets (see module doc).

    ``context_terms``: words from what the agent actually did (tool inputs, changed
    files). A requirement that names something the work touched is ranked above one
    that does not (grounding); ties go to the newer message."""
    users = [t for t in turns if t.role == "user"]
    agents = [t for t in turns if t.role == "agent"]
    out: dict[str, Any] = {"messages": len(users), "chats": _chats(turns), "first": None,
                           "ledger": [], "recent": [], "latest": None, "pasted": [],
                           "pasted_total": 0, "index": [],
                           "omitted_messages": 0, "omitted_items": 0,
                           "agent_decisions": _decisions(agents)}
    if not users:
        return out
    number = {t.seq: i for i, t in enumerate(users, start=1)}
    latest = users[-1]
    # the current instruction whole; a long paste in it is summarised by type so the
    # words typed after it are not cut off
    out["latest"] = _shown(latest, number, LATEST_CHARS,
                           verbatim=len(latest.text) <= LATEST_CHARS)
    out["latest"]["ends_chat"] = ends_chat(latest.text)
    asked = _preceding_question(latest, turns)
    if asked and _is_reply(latest.text):
        out["latest"]["in_reply_to"] = _clip(asked, ITEM_CHARS)
    if len(users) > 1:
        out["first"] = _shown(users[0], number, FIRST_CHARS)

    # ledger candidates from every message except the latest (shown whole)
    candidates: list[dict[str, Any]] = []
    pasted: list[dict[str, Any]] = []
    for u in users[:-1]:
        shown_first = u is users[0] and len(" ".join(u.text.split())) <= FIRST_CHARS
        items = []
        if _is_reply(u.text):
            q = _preceding_question(u, turns)
            if q:
                items.append(("answer", 3, f'asked "{_clip(q, 200)}" → "{u.text.strip()}"'))
        if not shown_first:
            for sentence in sentences(u.text):
                found = classify(sentence)
                if found:
                    items.append((found[0], found[1], _clip(sentence, ITEM_CHARS)))
        for kind, strength, text in pasted_items(u.text):
            pasted.append({"n": number[u.seq], "kind": kind, "strength": strength,
                           "text": text, "at": u.at, "source": u.source})
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
    # the most recent message (shown whole) can also replace an earlier requirement
    latest_items = [{"n": number[latest.seq], "pos": i, "kind": found[0], "text": s}
                    for i, s in enumerate(sentences(latest.text))
                    if (found := classify(s)) is not None]
    first_item = ([{"n": 1, "pos": -1, "kind": "first", "text": users[0].text}]
                  if len(users) > 1 else [])
    supersede(first_item + candidates + latest_items)
    if first_item and out["first"]:
        for key in ("replaced_by", "maybe_replaced_by"):
            if first_item[0].get(key):
                out["first"][key] = first_item[0][key]
    # fill the budget: revisions/answers and strong cues first; within a tier, items
    # grounded in what the agent did, then newer before older; requirements a later
    # message replaced go last (they are shown only as history)
    for c in candidates:
        c["grounded"] = bool(context_terms) and bool(_terms_of(c["text"]) & context_terms)
    ranked = sorted(candidates, key=lambda c: (c.get("replaced_by") is None, c["strength"],
                                               c["grounded"], c["n"]), reverse=True)
    budget = ledger_budget(len(users))
    chosen, used = [], 0
    chosen_words: list[set[str]] = []
    for c in ranked:
        cost = len(c["text"]) + 12
        words = set(_norm(c["text"]).split())
        # near-duplicates of something already kept add nothing (diversity, MMR-style)
        if c["kind"] == "statement" and any(_jaccard(words, w) > NEAR_DUPLICATE
                                            for w in chosen_words):
            out["omitted_items"] += 1
            continue
        if used + cost > budget:
            out["omitted_items"] += 1
            continue
        chosen.append(c)
        chosen_words.append(words)
        used += cost
    out["ledger"] = sorted(chosen, key=lambda c: (c["n"], c["pos"]))
    # what the user pasted, newest first by importance (errors first), as typed
    # summaries outside the requirements: a fact to look at, not an instruction
    shown = sorted(pasted, key=lambda p: (p["strength"], p["n"]), reverse=True)
    out["pasted"] = sorted(shown[:PASTED_SHOWN], key=lambda p: p["n"])
    out["pasted_total"] = len(pasted)

    # recent messages whole, if they carry content the ledger did not already keep
    in_ledger = {c["n"] for c in out["ledger"]}
    for u in reversed(users[1:-1]):
        if len(out["recent"]) >= RECENT_MESSAGES:
            break
        if low_information(u.text) or number[u.seq] in in_ledger or \
                not any(classify(s) for s in sentences(u.text)):
            continue   # questions, chatter and pasted output are not requirements
        out["recent"].insert(0, _shown(u, number, RECENT_CHARS))
    later_items = candidates + latest_items
    for r in out["recent"]:                  # a whole message can be replaced later too
        probe = {"n": r["n"], "pos": -1, "kind": "recent", "text": r["text"]}
        supersede([probe] + [c for c in later_items if c["n"] > r["n"]])
        for key in ("replaced_by", "maybe_replaced_by"):
            if probe.get(key):
                r[key] = probe[key]
    represented = in_ledger | {r["n"] for r in out["recent"]} | {1, len(users)}
    out["omitted_messages"] = len(users) - len(represented)
    out["index"] = chat_index(users, number) if len(users) >= INDEX_MIN_MESSAGES else []
    return out


# Words that say nothing about a topic: standard English stopwords, plus the filler of
# spoken and voice-typed chat ("like, you know, actually"), plus generic coding-chat verbs.
_STOP = frozenset((  # noqa: SIM905 (a readable word list)
    "a about above actually after again check checked against ago all almost alone "
    "along already also "
    "although always am among an and another any anybody anyone anything anyway anywhere are "
    "around as ask asked asking at away back basically be became because become been before "
    "began behind being below best better between big both but by call came can cannot could "
    "currently did do does doing done down during each either else enough even ever every "
    "everyone everything fine first for from full further gave get gets getting give given go "
    "goes going gone gonna good got great had has have having he hello help her here hey him "
    "his how however i if in indeed instead into is it its itself just keep kind know last "
    "least let lets like likely little look looking lot lots made make makes making many may "
    "maybe me mean means might mine more most much must my myself need needs never new next "
    "no nobody none nor not nothing now of off often oh ok okay on once one only or other "
    "others otherwise our ours out over own part per perhaps please point pretty probably put "
    "quite rather really right said same say saying see seem seems shall she should show side "
    "since so some somebody someone something sometimes somewhere soon still stuff such sure "
    "take tell than thank thanks that the their them then there therefore these they thing "
    "things think this those though through thus till to today together too took toward try "
    "trying two under unless until up upon us use used using very via want wanted wants was "
    "way we well went were what whatever when where whether which while who whole whom whose "
    "why will wish with within without won would yeah yes yet you your yours yourself "
    "definitely especially exactly literally obviously simply totally truly usually add added "
    "code coding fix fixed run running work working works "
).split())
_TERM = re.compile(r"`([^`]+)`|([A-Za-z_][\w'./-]{2,})")


def _terms_of(text: str) -> set[str]:
    """Content words and identifiers of a text, lower case (for grounding and the index)."""
    out: set[str] = set()
    for quoted, word in _TERM.findall(text):
        term = (quoted or word).lower().strip("./-'")
        if quoted:
            if len(term) >= 2:
                out.add(term)                       # `quoted` is always meant literally
            continue
        identifier = any(c in term for c in "_./") or any(c.isupper() for c in word[1:])
        if "'" in term or term in _STOP or len(term) < (3 if identifier else 4):
            continue
        out.add(term)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def chat_index(users: list[Turn], number: dict[int, int]) -> list[dict[str, Any]]:
    """An index of the whole conversation, so nothing is out of reach (D-046): the chat
    cut into stretches (a new app or chat, an hour's pause, or every
    ``INDEX_SPAN`` messages), each named by its most distinctive words (tf-idf against the
    rest of the chat), not by a keyword list. The next model asks ``coding_recall`` for a
    stretch it needs."""
    import math

    docs = [_terms_of(" ".join(prose(u.text))) for u in users]   # the person's words
    df: dict[str, int] = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    spans: list[list[int]] = []
    for i, u in enumerate(users):
        start_new = not spans or len(spans[-1]) >= INDEX_SPAN or (
            users[spans[-1][-1]].chat, users[spans[-1][-1]].source) != (u.chat, u.source)
        if not start_new and spans:
            gap = _gap_seconds(users[spans[-1][-1]].at, u.at)
            start_new = gap is not None and gap > 3600
        if start_new:
            spans.append([i])
        else:
            spans[-1].append(i)
    while len(spans) > INDEX_MAX_LINES:           # merge the smallest neighbouring pair
        k = min(range(len(spans) - 1), key=lambda j: len(spans[j]) + len(spans[j + 1]))
        spans[k:k + 2] = [spans[k] + spans[k + 1]]
    total = len(users)
    out = []
    for span in spans:
        tf: dict[str, int] = {}
        for i in span:
            for t in docs[i]:
                tf[t] = tf.get(t, 0) + 1
        score = {t: n * math.log(1 + total / df[t]) for t, n in tf.items()}
        top = sorted(score, key=lambda t: (-score[t], t))[:INDEX_TERMS]
        first, last = users[span[0]], users[span[-1]]
        out.append({"from": number[first.seq], "to": number[last.seq],
                    "source": first.source, "terms": top})
    return out


def _gap_seconds(a: str, b: str) -> float | None:
    from datetime import datetime

    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except (TypeError, ValueError):
        return None


# What a change of mind replaces: "use B instead of A", "B rather than A", "change it
# from A to B", "A is no longer needed", "not A anymore".
_REPLACED = [re.compile(p, re.I) for p in (
    r"\binstead of\s+(?:the\s+|using\s+|a\s+)?([^,.;!?]+)",
    r"\brather than\s+(?:the\s+|using\s+|a\s+)?([^,.;!?]+)",
    r"\b(?:change|switch|move|go|update)\w*\s+(?:it\s+|this\s+|that\s+)?from\s+"
    r"(.+?)\s+to\b",
    r"\b(?:replace|swap)\s+(?:the\s+)?(.+?)\s+with\b",
    r"\bno longer\s+(?:use|need|want)\s+(?:the\s+)?([^,.;!?]+)",
    r"\bnot\s+(.+?)\s+any\s?more\b",
)]
_WORD = re.compile(r"`[^`]+`|\d[\w.:/-]*|[A-Za-z][\w./-]{3,}")
_COMMON = frozenset({"that", "this", "with", "from", "into", "what", "which", "there",
                     "their", "they", "them", "then", "than", "have", "will", "would",
                     "should", "could", "using", "make", "made", "just", "also", "please",
                     "instead", "rather", "anymore", "longer", "need", "want", "thing",
                     "things", "some", "more", "less", "use", "used", "keep", "change",
                     "switch", "move", "replace", "swap", "update", "build", "run", "set",
                     "let's", "lets", "it's", "now", "only", "every", "each", "all", "must",
                     "always", "never", "don't", "does", "done", "the", "and", "for"})


def _terms(phrase: str) -> list[str]:
    terms = [w.strip("`").lower().rstrip(".:") for w in _WORD.findall(phrase)]
    return [w for w in terms if w and w not in _COMMON]


def _key_terms(phrase: str) -> list[str]:
    """The distinctive terms of what is being replaced: a value or identifier when there
    is one ("port 4000" -> "4000"), else its content words."""
    terms = _terms(phrase)
    values = [w for w in terms if any(ch.isdigit() for ch in w) or ("`" in phrase
              and f"`{w}`" in phrase.lower())]
    return values or terms[:4]


def _has(text: str, term: str) -> bool:
    return re.search(r"(?<![\w.])" + re.escape(term) + r"(?![\w])", text) is not None


def supersede(candidates: list[dict[str, Any]]) -> None:
    """Mark earlier requirements that a later change of mind replaces.

    Deterministic: the later message must name what it replaces ("instead of A"), and an
    earlier item must contain all of A's distinctive terms. That alone is only a
    *possible* replacement (``maybe_replaced_by``: shown, never struck out): "port 5000
    instead of 4000" must not strike "the timeout must be 4000 ms". It is a replacement
    (``replaced_by``) when the earlier item also shares the context: a content word of the
    later sentence outside A ("port"), or A itself has content words besides a value.

    Only the user's own words count: an answer item quotes the agent's question, so it
    can be replaced but never replaces anything. An item is never deleted: it stays in
    the journal and in recall."""
    for later in candidates:
        if later["kind"] == "answer":
            continue
        if later["kind"] != "revision" and not any(r.search(later["text"])
                                                   for r in _REPLACED):
            continue
        for pattern in _REPLACED:
            for match in pattern.finditer(later["text"]):
                phrase = match.group(1)
                terms = _key_terms(phrase)
                if not terms:
                    continue
                values_only = all(any(ch.isdigit() for ch in t) for t in terms)
                own_words = [w for w in _terms(phrase) if w not in terms]
                rest = later["text"][:match.start()] + " " + later["text"][match.end():]
                context = [w for w in _terms(rest) if not any(ch.isdigit() for ch in w)]
                for earlier in candidates:
                    if earlier["n"] >= later["n"] or earlier.get("replaced_by"):
                        continue
                    low = earlier["text"].lower()
                    if not all(_has(low, term) for term in terms):
                        continue
                    shared = [w for w in (own_words or context) if _has(low, w)]
                    # a quoted identifier (`old_export`) or several words are
                    # specific enough; a bare number or one common word is not
                    sure = bool(shared) or (not values_only and (
                        len(terms) >= 2 or "`" in phrase))
                    if sure:
                        earlier["replaced_by"] = later["n"]
                        earlier.pop("maybe_replaced_by", None)
                    elif not earlier.get("maybe_replaced_by"):
                        earlier["maybe_replaced_by"] = later["n"]


def readable(text: str) -> str:
    """A message as prose, each pasted block (trace, diff, log, JSON, table, code)
    replaced in place by its typed summary; the full text stays in the journal."""
    from continuum.coding.segments import readable as typed

    return typed(text)


def pasted_items(text: str) -> list[tuple[str, int, str]]:
    """Ledger items for what the user pasted: an error, a diff or a table is a fact the
    next model needs, summarised by structure (D-046)."""
    from continuum.coding.segments import technical

    items = []
    for seg in technical(text):
        strength = 2 if seg.kind == "traceback" else 1
        if seg.kind in ("traceback", "diff", "table", "json", "log", "code", "shell"):
            items.append(("pasted", strength, f"pasted {seg.summary}"))
    return items


def ledger_budget(messages: int) -> int:
    """Characters for the ledger: fixed for ordinary chats, growing slowly for very long
    ones, capped so that no chat makes the brief expensive."""
    return min(LEDGER_MAX_CHARS, LEDGER_CHARS + LEDGER_PER_MESSAGE * max(0, messages - 40))


def _shown(turn: Turn, number: dict[int, int], limit: int, *,
           verbatim: bool = False) -> dict[str, Any]:
    text = " ".join((turn.text if verbatim else readable(turn.text)).split())
    return {"n": number[turn.seq], "text": _clip(text, limit), "at": turn.at,
            "source": turn.source, "clipped": len(text) > limit}


def _is_reply(text: str) -> bool:
    """A short answer ("yes", "ok, do it"), not a message that merely starts with "OK"
    (found in a real handoff: "OK, I'm out of time. Stop here, ..." was paired with the
    agent's open question)."""
    text = text.strip()
    return len(text) < SHORT_REPLY_CHARS and (low_information(text)
                                              or _AFFIRM.match(text) is not None)


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
