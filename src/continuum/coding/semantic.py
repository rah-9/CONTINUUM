# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Semantic session memory: an embedding model organises the whole session (D-050).

No language model writes anything: every fact is quoted from the journal with its event
number. A sentence-embedding model gives each statement a vector (its meaning), and the
memory is built in layers, like a person's notes on a long project:

1. **State of the work**: what each check, pull request, commit and file ended up as,
   folded over the whole session (``digest._state_lines``).
2. **Topics.** Every statement gets a vector; statements are grouped by meaning into the
   threads of the session (the CSV export, the flaky CI job, the database upgrade ...),
   even when a thread is spread over hours with other work in between. Each topic is
   named by its most distinctive words. The overview lists them, most important first.
3. **Facts per topic**, in the order they happened.

What is kept is chosen across the whole session (``digest.select``: the user's
decisions, pastes and unknown outcomes always, then value per character with
diminishing returns per turn), with the meaning added to the word statistics: a
statement's score grows with how central it is to its topic (it represents the thread)
and with its topic's closeness to the ``focus`` (what the user is asking now); and a
statement that says what a chosen one already says, in any words and naming nothing new,
is not kept twice. Measured: choosing globally and organising afterwards keeps far more
facts than giving each topic a fixed share of the room.

Thresholds are calibrated per model and per session: models put unrelated sentences at
very different similarities (one at 0.0, another at 0.4), so "same topic" and "same
statement" are percentiles of the session's own similarity distribution, not constants.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from continuum.coding import digest as dg
from continuum.coding import embed

POOL = 1_500                  # statements embedded: the always-kept and the best by words
TOPIC_PERCENTILE = 95.0       # similarity to a topic above this share of all pairs: joins it
SAME_PERCENTILE = 99.7        # two statements this close say the same thing
MIN_BUDGET = 8_000            # smaller memories are the lexical digest (measured)
MAX_TOPICS = 16
TOPIC_CHARS = 1_200           # one topic per this much room, at least 3, at most MAX_TOPICS
HEADING_CHARS = 75            # room reserved per topic heading
MIN_TOPIC_UNITS = 2           # smaller groups join their nearest topic, or "Other"
CENTRALITY_WEIGHT = 0.6       # score x (1 + this x centrality in its topic)
FOCUS_WEIGHT = 1.0            # score x (1 + this x its topic's closeness to the focus)



@dataclass
class Topic:
    members: list[int] = field(default_factory=list)      # indexes into the unit list
    centroid: Any = None
    weight: float = 0.0
    name: str = ""
    focus: float = 0.0


def _calibrate(vectors: Any, rng_seed: int = 7) -> tuple[float, float]:
    """(topic threshold, same-statement threshold) from this session's own similarity
    distribution: random pairs, percentiles."""
    n = len(vectors)
    rng = np.random.default_rng(rng_seed)
    m = min(20_000, n * (n - 1) // 2) or 1
    a, b = rng.integers(0, n, m), rng.integers(0, n, m)
    keep = a != b
    sims = np.einsum("ij,ij->i", vectors[a[keep]], vectors[b[keep]])
    if not len(sims):
        return 0.6, 0.9
    topic = float(np.percentile(sims, TOPIC_PERCENTILE))
    same = float(np.percentile(sims, SAME_PERCENTILE))
    return topic, max(same, topic + 0.05)


def _cluster(order: list[int], vectors: Any, threshold: float) -> list[Topic]:
    """Online clustering in journal order: each statement joins the topic whose centroid
    it is closest to, if close enough, else starts a new one. Threads are found even
    when interleaved, and topics come out in order of their first appearance."""
    topics: list[Topic] = []
    sums: list[Any] = []
    for i in order:
        v = vectors[i]
        if topics:
            cents = np.stack([t.centroid for t in topics])
            sims = cents @ v
            best = int(np.argmax(sims))
            if sims[best] >= threshold:
                topics[best].members.append(i)
                sums[best] = sums[best] + v
                c = sums[best] / (np.linalg.norm(sums[best]) or 1.0)
                topics[best].centroid = c
                continue
        topics.append(Topic(members=[i], centroid=v.copy()))
        sums.append(v.copy())
    return topics


def _merge_small(topics: list[Topic], vectors: Any, threshold: float) -> list[Topic]:
    """Groups too small to be a thread join their nearest topic; leftovers form "Other";
    beyond MAX_TOPICS the least important topics are folded into their nearest."""
    big = [t for t in topics if len(t.members) >= MIN_TOPIC_UNITS]
    small = [t for t in topics if len(t.members) < MIN_TOPIC_UNITS]
    other = Topic()
    for t in small:
        if big:
            sims = np.stack([b.centroid for b in big]) @ t.centroid
            j = int(np.argmax(sims))
            if sims[j] >= threshold * 0.8:
                big[j].members.extend(t.members)
                continue
        other.members.extend(t.members)
    return big + ([other] if other.members else [])


def _recompute(topic: Topic, vectors: Any) -> None:
    s = vectors[topic.members].sum(axis=0)
    topic.centroid = s / (np.linalg.norm(s) or 1.0)


def _name(topics: list[Topic], units: list[dg.Unit]) -> None:
    """Each topic named by its most distinctive words (class-based TF-IDF): frequent in
    the topic, rare in the rest of the session."""
    words = [Counter(w for i in t.members for w in set(dg._words(units[i].text)))
             for t in topics]
    df: Counter[str] = Counter()
    for c in words:
        df.update(c.keys())
    n = len(topics)
    for t, c in zip(topics, words, strict=True):
        scored = sorted(((cnt * math.log(1 + n / df[w]), w) for w, cnt in c.items()
                         if cnt >= 2 and len(w) > 3 and not w.isdigit()), reverse=True)
        t.name = ", ".join(w for _, w in scored[:4]) or "other"


def _distinct(units: list[dg.Unit]) -> list[dg.Unit]:
    """One unit per statement: a sentence the agent repeats word for word counts once
    (the newest occurrence, which is the current one)."""
    seen: dict[str, dg.Unit] = {}
    for u in units:
        key = re.sub(r"\s+", " ", u.text.lower()).strip(" .")
        seen[key] = u
    return list(seen.values())


class _Space:
    """The session's statements in vector space: topics, thresholds, centrality."""

    def __init__(self, embedder: embed.Embedder, focus: str | None, max_topics: int) -> None:
        self.embedder, self.focus, self.max_topics = embedder, focus, max_topics
        self.index: dict[int, int] = {}            # id(unit) -> row
        self.units: list[dg.Unit] = []
        self.vectors: Any = None
        self.topics: list[Topic] = []
        self.topic_of: dict[int, Topic] = {}       # id(unit) -> topic
        self.same = 1.01
        self.failed = False

    def prepare(self, cands: list[dg.Unit]) -> None:
        """Embed the statements that can be kept, find the topics, and add meaning to
        the scores: centrality in the topic, closeness of the topic to the focus."""
        must = [u for u in cands if u.kind in dg.MUST]
        others = [u for u in cands if u.kind not in dg.MUST]
        # generic narration ("let me look at the code first") forms no topic: what is
        # kept names something specific or is rarer than the session's median statement
        median = sorted(u.score for u in others)[len(others) // 2] if others else 0.0
        content = [u for u in _distinct(others)
                   if dg._SPECIFIC.search(u.text) or u.score > median]
        content.sort(key=lambda u: -u.score)
        pool = sorted(must + content[:max(0, POOL - len(must))], key=lambda u: u.seq)
        if len(pool) < 3:
            return
        try:
            vectors = self.embedder.embed([u.text for u in pool])
            fvec = self.embedder.embed([self.focus])[0] if self.focus else None
        except embed.EmbedError:
            self.failed = True
            return
        self.units, self.vectors = pool, vectors
        self.index = {id(u): i for i, u in enumerate(pool)}
        topic_t, self.same = _calibrate(vectors)
        topics = _merge_small(_cluster(list(range(len(pool))), vectors, topic_t), vectors,
                              topic_t)
        for t in topics:
            _recompute(t, vectors)
            if fvec is not None:
                t.focus = float(t.centroid @ fvec)
        if len(topics) > self.max_topics:
            topics = _fold(topics, vectors, self.max_topics)
        _name(topics, pool)
        self.topics = topics
        for t in topics:
            sims = vectors[t.members] @ t.centroid
            for i, sim in zip(t.members, sims, strict=True):
                u = pool[i]
                self.topic_of[id(u)] = t
                u.score *= 1 + CENTRALITY_WEIGHT * max(0.0, float(sim))
                u.score *= 1 + FOCUS_WEIGHT * max(0.0, t.focus)
                t.weight += u.score

    def redundant(self, u: dg.Unit, chosen: list[dg.Unit]) -> bool:
        """Says what a chosen statement says, in any words, and names nothing new."""
        i = self.index.get(id(u))
        if i is None or self.vectors is None:
            return False
        rows = [self.index[id(c)] for c in chosen if id(c) in self.index]
        if not rows:
            return False
        sims = self.vectors[rows] @ self.vectors[i]
        mine = dg._specifics(u.text)
        for row, sim in zip(rows, sims, strict=True):
            if sim >= self.same and mine <= dg._specifics(self.units[row].text):
                return True
        return False


def _fold(topics: list[Topic], vectors: Any, limit: int) -> list[Topic]:
    """Beyond ``limit`` topics the smallest are folded into their nearest."""
    topics = sorted(topics, key=lambda t: -len(t.members))
    keep, fold = topics[:limit], topics[limit:]
    for t in fold:
        j = int(np.argmax(np.stack([k.centroid for k in keep]) @ t.centroid))
        keep[j].members.extend(t.members)
    for t in keep:
        _recompute(t, vectors)
    return keep


def build(lines: Iterable[tuple[int, str]], budget: int = dg.DIGEST_CHARS, *,
          embedder: embed.Embedder, user: bool = True, focus: str | None = None) -> str:
    """The session memory of ``lines`` in at most ``budget`` characters, organised by an
    embedding model. On any embedding failure, and below MIN_BUDGET (where the topic
    structure costs more room than it saves), it is the lexical digest."""
    if budget < MIN_BUDGET:
        return dg.build(lines, budget, user=user)
    # a smaller memory has fewer, broader topics: the structure must not eat the room
    max_topics = max(3, min(MAX_TOPICS, budget // TOPIC_CHARS))
    space = _Space(embedder, focus, max_topics)
    sel = dg.select(lines, budget, user=user, reserve=max_topics * HEADING_CHARS,
                    prepare=space.prepare, redundant=space.redundant)
    if sel is None:
        return ""
    if space.failed or not space.topics:
        return dg.build(lines, budget, user=user)
    by_topic: dict[int, list[dg.Unit]] = {}
    loose: list[dg.Unit] = []
    for u in sel.chosen:
        t = space.topic_of.get(id(u))
        if t is None:
            loose.append(u)
        else:
            by_topic.setdefault(id(t), []).append(u)
    present = [t for t in space.topics if id(t) in by_topic]
    ranked = sorted(present, key=lambda t: -t.weight)
    out: list[str] = [dg.STATE_HEADING, *sel.state] if sel.state else []
    if sel.dropped:
        out.append(dg._dropped_note(sel.dropped))
    rank = {id(t): n for n, t in enumerate(ranked, 1)}
    # the topics in the order the session took them up; the rank says which matter most
    for t in sorted(present, key=lambda t: min(u.seq for u in by_topic[id(t)])):
        mine = by_topic[id(t)]
        out.append(f"### Topic: {t.name} (importance {rank[id(t)]} of {len(ranked)}; "
                   f"#{min(u.seq for u in mine)} to #{max(u.seq for u in mine)})")
        out.extend(f"- [#{u.seq}] {dg._label(u)}{u.text}"
                   for u in sorted(by_topic[id(t)], key=lambda x: sel.position[id(x)]))
    if loose:
        out.append("### Other")
        out.extend(f"- [#{u.seq}] {dg._label(u)}{u.text}"
                   for u in sorted(loose, key=lambda x: sel.position[id(x)]))
    return dg._fit("\n".join(out), budget)
