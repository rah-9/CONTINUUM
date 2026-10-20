# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Technical content in a message, recognised by structure, never by a model (D-046).

People paste stack traces, diffs, logs, JSON, tables and code into coding chats. Those
are often the most important facts in the chat ("this is the error"), but they are not
prose: a sentence splitter mangles them and a requirement rule must not read "must" in
a log line. This module splits a message into typed segments and summarises each one
deterministically:

* **traceback** (Python, JavaScript/Node, Java/JVM, Go panic, Rust panic): the error
  signature (type, message, the innermost frame in the project's own code)
* **diff**: the files and +/- line counts
* **json**: the top-level keys or the array length
* **table**: rows x columns and the header
* **log**: lines, counts per level, the first error line
* **shell**: the commands typed (``$ cmd``)
* **code**: the language when known (fence tag or syntax) and the names it defines

Every segment keeps its full text: the summary is a pointer, not a replacement. The
identifiers in technical segments (``getUserById``, ``api/views.py``, ``user_id``) are
also extracted, split into words, so recall finds them however they are written.
Standard library only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SUMMARY_CHARS = 220

_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+#.-]*)\s*$")
_TECH_LINE = re.compile(
    r"^\s*(Traceback \(most recent call last\)|File \"|at [\w.$<>]+[ (]|"
    r"Exception in thread|Caused by:|goroutine \d+|panic:|thread '.*' panicked|"
    r"[\w.]*(Error|Exception|Warning)\b[:(]|"
    r"(WARN(ING)?|ERROR|INFO|DEBUG|FATAL|CRITICAL|TRACE)\b|\[\d|"
    r"\d{4}-\d\d-\d\d[ T]\d\d:|diff --git |[+-]{3} [ab/]|@@ |\$ |>>> |\t|\s{4,}\S|"
    r"[{\[]\s*$|[{\[].*[}\]]\s*$|\|.*\|.*\|)")
_PY_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+)(?:, in (\S+))?')
_JS_FRAME = re.compile(r"^\s*at (?:([\w.$<>\[\] ]+?) \()?((?:[A-Za-z]:)?[^():\s]+):(\d+)"
                       r"(?::\d+)?\)?\s*$", re.M)
_JAVA_FRAME = re.compile(r"^\s*at ([\w.$<>]+)\(([\w.$-]+):(\d+)\)", re.M)
_ERROR_LINE = re.compile(
    r"^\s*(?:Exception in thread \"[^\"]*\" )?([A-Za-z_][\w.$]*(?:Error|Exception|Warning|"
    r"Interrupt|Exit|Failure|Fault))(?::\s*(.*))?$")
_PANIC = re.compile(r"^\s*(panic: .*|thread '.*' panicked at .*)$")
_DEFINES = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|func|fn|struct|enum|interface|"
    r"trait|type|impl|module|record)\s+([A-Za-z_][\w]*)|"
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", re.M)
_LANG_HINTS = [
    ("python", re.compile(r"^\s*(def |class \w+[:(]|import \w|from [\w.]+ import |"
                          r"if __name__)", re.M)),
    ("typescript", re.compile(r"^\s*(interface \w+|type \w+ =|export (const|function|"
                              r"class)|import .* from ['\"])|: (string|number)\b", re.M)),
    ("javascript", re.compile(r"^\s*(function \w+|const \w+ = |let \w+ = |"
                              r"module\.exports|require\()", re.M)),
    ("go", re.compile(r"^\s*(package \w+|func (\(\w+ \*?\w+\) )?\w+\(|import \()", re.M)),
    ("rust", re.compile(r"^\s*(fn \w+|let mut |impl |use \w+::|pub (fn|struct))", re.M)),
    ("java", re.compile(r"^\s*(public |private |protected )?(static )?(class|void|"
                        r"interface) ", re.M)),
    ("sql", re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|WITH)\b", re.M | re.I)),
    ("html", re.compile(r"^\s*<(!DOCTYPE|html|div|span|body|head)\b", re.M | re.I)),
    ("css", re.compile(r"^\s*[.#]?[\w-]+\s*\{\s*$", re.M)),
    ("yaml", re.compile(r"^\s*[\w-]+:\s+\S.*$\n^\s*[\w-]+:", re.M)),
]
_IDENT = re.compile(
    r"(?<![\w/.-])(?:[A-Za-z_][\w-]*/)*[A-Za-z_][\w-]*\.[A-Za-z]{1,6}(?::\d+)?|"  # paths
    r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b|"  # camel/Pascal
    r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b|\b[A-Z0-9]+(?:_[A-Z0-9]+)+\b")              # snake/CONST
_LOG_LEVEL = re.compile(r"\b(FATAL|CRITICAL|ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE)\b")
_VENDOR = re.compile(r"(site-packages|dist-packages|node_modules|/lib/python\d|"
                     r"<frozen|internal/|/usr/lib/|\.cargo/registry|/go/pkg/mod/)")


@dataclass
class Segment:
    kind: str                    # prose | traceback | diff | json | table | log | shell | code
    text: str
    lang: str | None = None
    summary: str = ""
    identifiers: list[str] = field(default_factory=list)

    @property
    def lines(self) -> int:
        return len(self.text.splitlines())


def segment(text: str) -> list[Segment]:
    """The message as an ordered list of segments; joining their texts gives it back."""
    out: list[Segment] = []
    lines = text.splitlines()
    i = 0
    prose_buf: list[str] = []

    def flush_prose() -> None:
        if prose_buf and any(line.strip() for line in prose_buf):
            out.append(Segment("prose", "\n".join(prose_buf)))
        prose_buf.clear()

    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush_prose()
            marker, tag = fence.group(1), fence.group(2).lower() or None
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker[:3]):
                body.append(lines[i])
                i += 1
            i += 1                                    # the closing fence
            out.append(_typed("\n".join(body), tag))
            continue
        if line.strip() and _technical(line, lines[i + 1] if i + 1 < len(lines) else ""):
            flush_prose()
            start = i
            i += 1
            # a pasted block runs to the next blank line (or the end) ...
            while i < len(lines) and lines[i].strip():
                i += 1
            # ... except JSON and tables, which end exactly where they end: prose
            # typed right after them ("thanks") is still the person's words
            i = start + _exact_length(lines[start:i])
            out.append(_typed("\n".join(lines[start:i]), None))
            continue
        prose_buf.append(line)
        i += 1
    flush_prose()
    return out


def _exact_length(run: list[str]) -> int:
    """How many lines of ``run`` belong to the pasted block."""
    first = run[0].lstrip()
    if first[:1] in "{[":
        for k in range(1, len(run) + 1):
            if _json("\n".join(run[:k])) is not None:
                return k
    if first.startswith("|") or first.count("|") >= 2:
        k = 0
        while k < len(run) and run[k].count("|") >= 2:
            k += 1
        return max(1, k)
    return len(run)


def _technical(line: str, following: str) -> bool:
    if _TECH_LINE.match(line):
        return True
    stripped = line.strip()
    letters = sum(c.isalpha() or c.isspace() for c in stripped)
    if len(stripped) > 12 and letters / len(stripped) < 0.6:     # mostly symbols
        return True
    # a line of code ("x = foo(1);" / "return bar") followed by an indented line
    return bool(re.match(r"^\S.*[:{(]\s*$", line) and re.match(r"^\s{2,}\S", following))


def _typed(body: str, tag: str | None) -> Segment:
    seg = Segment("code", body, lang=tag)
    stripped = body.strip()
    kind = _kind(stripped, tag)
    seg.kind = kind
    if kind == "code" and not seg.lang:
        seg.lang = _language(body)
    seg.summary = _summary(seg)
    seg.identifiers = identifiers(body)
    return seg


def _kind(body: str, tag: str | None) -> str:
    if tag in ("diff", "patch"):
        return "diff"
    if tag == "json":
        return "json" if _json(body) is not None else "code"
    if tag in ("log", "logs", "console", "output", "text", "txt", "stderr", "stdout"):
        return "traceback" if _error_signature(body) else "log"
    if tag in ("sh", "bash", "shell", "zsh", "console$", "powershell", "ps1", "cmd"):
        return "shell" if re.search(r"^\s*[$>] ", body, re.M) or tag else "code"
    lines = body.splitlines()
    if re.search(r"^(diff --git |--- a/|\+\+\+ b/|@@ -\d)", body, re.M):
        return "diff"
    if _error_signature(body) and (re.search(r"^\s*(Traceback|File \"|at |Caused by|"
                                             r"goroutine|panic|thread ')", body, re.M)):
        return "traceback"
    if body[:1] in "{[" and _json(body) is not None:
        return "json"
    if len(lines) >= 2 and sum("|" in ln for ln in lines) >= max(2, len(lines) - 1):
        return "table"
    if len(lines) >= 2 and sum(bool(_LOG_LEVEL.search(ln) or re.match(
            r"^\s*\[?\d{4}-\d\d-\d\d", ln)) for ln in lines) >= max(2, len(lines) // 2):
        return "log"
    if re.match(r"^\s*\$ ", body) and not tag:
        return "shell"
    if _error_signature(body):
        return "traceback"
    return "code"


def _json(body: str) -> object | None:
    try:
        value: object = json.loads(body)
    except ValueError:
        return None
    return value


def _language(body: str) -> str | None:
    for name, pattern in _LANG_HINTS:
        if pattern.search(body):
            return name
    return None


def _error_signature(body: str) -> str | None:
    """``Type: message (at file:line in function)`` for the innermost project frame."""
    lines = body.splitlines()
    error = None
    for line in reversed(lines):
        m = _ERROR_LINE.match(line)
        if m:
            message = (m.group(2) or "").strip()
            error = f"{m.group(1)}: {message}" if message else m.group(1)
            break
        p = _PANIC.match(line)
        if p:
            error = p.group(1)
            break
    if error is None:
        return None
    frames: list[str] = []
    for line in lines:
        for pattern, parts in ((_PY_FRAME, (1, 2, 3)), (_JAVA_FRAME, (2, 3, 1)),
                               (_JS_FRAME, (2, 3, 1))):
            m = pattern.match(line)
            if m:
                where, number, func = (m.group(k) for k in parts)
                frames.append(f"{where}:{number}" + (f" in {func.strip()}" if func else ""))
                break
    own = [f for f in frames if not _VENDOR.search(f)]
    # Python lists the innermost frame last; the JVM and V8 list it first
    python = any(_PY_FRAME.match(line) for line in lines)
    pick = (own or frames)
    frame = (pick[-1] if python else pick[0]) if pick else None
    return error[:160] + (f" (at {frame})" if frame else "")


def _summary(seg: Segment) -> str:
    body, n = seg.text, seg.lines
    lines = body.splitlines()
    if seg.kind == "traceback":
        signature = _error_signature(body) or (lines[-1].strip() if lines else "error")
        lang = ("Python" if re.search(r"^\s*(Traceback|File \")", body, re.M) else
                "Java" if _JAVA_FRAME.search(body) or "Exception in thread" in body else
                "Go" if "goroutine" in body else "Rust" if "panicked" in body else
                "JavaScript" if _JS_FRAME.search(body) else "")
        text = f"{lang + ' ' if lang else ''}error: {signature} ({n} lines)"
    elif seg.kind == "diff":
        files = re.findall(r"^\+\+\+ b/(\S+)", body, re.M) or re.findall(
            r"^diff --git a/\S+ b/(\S+)", body, re.M)
        added = sum(ln.startswith("+") and not ln.startswith("+++") for ln in lines)
        removed = sum(ln.startswith("-") and not ln.startswith("---") for ln in lines)
        shown = ", ".join(dict.fromkeys(files[:4]))
        more = f" +{len(set(files)) - 4} more" if len(set(files)) > 4 else ""
        text = f"diff of {len(set(files)) or 1} file(s) (+{added}/-{removed})" + (
            f": {shown}{more}" if shown else "")
    elif seg.kind == "json":
        data = _json(body)
        if isinstance(data, dict):
            keys = list(data)[:6]
            text = f"JSON object with {len(data)} key(s): " + ", ".join(map(str, keys)) + (
                ", …" if len(data) > 6 else "")
        elif isinstance(data, list):
            text = f"JSON array of {len(data)} item(s)"
        else:
            text = "JSON value"
    elif seg.kind == "table":
        rows = [ln for ln in lines if not re.match(r"^\s*\|?\s*:?-{2,}", ln)]
        header = [c.strip() for c in rows[0].strip().strip("|").split("|")] if rows else []
        text = (f"table, {max(0, len(rows) - 1)} row(s) x {len(header)} column(s): "
                + ", ".join(h for h in header[:6] if h))
    elif seg.kind == "log":
        levels: dict[str, int] = {}
        for ln in lines:
            m = _LOG_LEVEL.search(ln)
            if m:
                level = m.group(1).replace("WARNING", "WARN")
                levels[level] = levels.get(level, 0) + 1
        first = next((ln.strip() for ln in lines if re.search(
            r"\b(ERROR|FATAL|CRITICAL|Exception|Traceback)\b", ln)), None)
        counts = ", ".join(f"{v} {k}" for k, v in sorted(levels.items(), key=lambda x: -x[1]))
        text = f"log, {n} line(s)" + (f" ({counts})" if counts else "") + (
            f"; first error: {first}" if first else "")
    elif seg.kind == "shell":
        commands = [ln.strip()[2:] for ln in lines if re.match(r"^\s*[$>] ", ln)]
        text = f"shell session, {len(commands) or n} command(s): " + "; ".join(
            commands[:3] or [lines[0].strip()])
    else:
        names = [a or b for a, b in _DEFINES.findall(body)]
        lang = seg.lang or "code"
        text = f"{lang} code, {n} line(s)" + (
            ", defines " + ", ".join(dict.fromkeys(names[:6])) if names else "")
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS - 1] + "…"


def identifiers(text: str, limit: int = 40) -> list[str]:
    """Distinct identifiers and file paths in technical text, in order of appearance."""
    found: dict[str, None] = {}
    for m in _IDENT.finditer(text):
        word = m.group(0).rstrip(".:")
        if len(word) >= 4:
            found.setdefault(word, None)
        if len(found) >= limit:
            break
    return list(found)


def split_identifier(word: str) -> list[str]:
    """``getUserById`` -> get, user, by, id; ``user_id`` -> user, id; ``api/views.py`` ->
    api, views, py. Lower case, for search."""
    parts = re.split(r"[^A-Za-z0-9]+", word)
    out: list[str] = []
    for part in parts:
        out += [p.lower() for p in re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+",
                                               part)]
    return [p for p in out if p]


def search_terms(text: str) -> str:
    """Extra words that make identifiers findable by their parts (for recall)."""
    words: dict[str, None] = {}
    for ident in identifiers(text, limit=200):
        for part in split_identifier(ident):
            if len(part) > 1:
                words.setdefault(part, None)
    return " ".join(words)


def readable(text: str) -> str:
    """A message as prose with each pasted block replaced by its typed summary in place:
    ``[pasted Python error: KeyError: 'user_id' (at api/views.py:88 in get) (40 lines)]``."""
    parts: list[str] = []
    for seg in segment(text):
        if seg.kind == "prose":
            parts.append(" ".join(seg.text.split()))
        else:
            parts.append(f"[pasted {seg.summary}]")
    return " ".join(p for p in parts if p)


def technical(text: str) -> list[Segment]:
    return [s for s in segment(text) if s.kind != "prose"]
