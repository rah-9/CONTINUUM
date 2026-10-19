# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Trial 7: compare Continuum's journal with Claude Code's own record of the session.

Ground truth is read independently of Continuum's parser: every ``tool_use`` and
``tool_result`` in the main transcript and the subagent transcripts, and the repository's
real changes (``git status``). Reports what was captured, what was missed, and how long
the hooks took.

  python check.py WORKDIR     (CONTINUUM_DATABASE_URL / _DB_SCHEMA / _HOME set)
"""

import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

import psycopg


def transcripts(proj: Path) -> list[Path]:
    key = str(proj).replace("/", "-").replace(".", "-").replace("_", "-")
    base = Path.home() / ".claude" / "projects"
    dirs = [d for d in base.iterdir() if d.name == key or d.name.startswith(key)]
    return sorted(p for d in dirs for p in d.rglob("*.jsonl"))


def truth(files: list[Path]) -> dict[str, dict]:
    calls: dict[str, dict] = {}
    for f in files:
        sub = "subagents" in f.parts or f.name.startswith("agent-")
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            content = (r.get("message") or {}).get("content") if isinstance(r, dict) else None
            if not isinstance(content, list) or not r.get("uuid"):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    calls.setdefault(part["id"], {"tool": part["name"], "sub": sub,
                                                  "error": None})
                elif part.get("type") == "tool_result" and part.get("tool_use_id") in calls:
                    calls[part["tool_use_id"]]["error"] = part.get("is_error") is True
    return calls


def main() -> None:
    work = Path(sys.argv[1])
    proj = work / "proj"
    files = transcripts(proj)
    real = truth(files)
    schema = os.environ["CONTINUUM_DB_SCHEMA"]
    with psycopg.connect(os.environ["CONTINUUM_DATABASE_URL"],
                         options=f"-c search_path={schema}") as conn:
        rows = conn.execute("SELECT seq, type, payload_json FROM coding_events "
                            "ORDER BY session_id, seq").fetchall()
        blobs = conn.execute("SELECT count(*), coalesce(sum(chars), 0) FROM coding_blobs"
                             ).fetchone()
    started = {p.get("call_id"): p for _, t, p in rows if t == "ToolCallStarted"}
    finished = {p["call_key"]: p for _, t, p in rows if t == "ToolCallFinished"}
    impacts = [p for _, t, p in rows if t == "FileImpactObserved"]
    unresolved = [p for _, t, p in rows if t == "ToolCallUnresolved"]
    by_key = {p["call_key"]: p for p in started.values()}

    missing = [cid for cid in real if cid not in started]
    no_result = [cid for cid in real if cid in started
                 and started[cid]["call_key"] not in finished]
    wrong_status = []
    for cid, t in real.items():
        if cid in started and started[cid]["call_key"] in finished:
            status = finished[started[cid]["call_key"]]["status"]
            if t["error"] is not None and (status != "ok") != t["error"]:
                wrong_status.append((t["tool"], status, t["error"]))
    changed = subprocess.run(["git", "-C", str(proj), "status", "--porcelain",
                              "--untracked-files=all"], capture_output=True, text=True
                             ).stdout.splitlines()
    git_files = {ln[3:] for ln in changed if "__pycache__" not in ln and ".claude" not in ln}
    observed = {f["path"] for p in impacts for f in p["files"]}
    print(f"transcripts read: {len(files)}")
    print(f"tool calls in Claude Code's record: {len(real)} "
          f"({sum(1 for t in real.values() if t['sub'])} inside subagents)")
    print(f"captured: {len(real) - len(missing)}/{len(real)}; with result: "
          f"{len(real) - len(missing) - len(no_result)}/{len(real)}; status mismatches: "
          f"{len(wrong_status)} {wrong_status}")
    print(f"missing: {[real[c]['tool'] for c in missing]}; "
          f"without result: {[real[c]['tool'] for c in no_result]}")
    kinds: dict[str, int] = {}
    for p in started.values():
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"by kind: {kinds}; skills: "
          f"{sorted({p['skill'] for p in started.values() if p.get('skill')})}; MCP: "
          f"{sorted({p['tool'] for p in started.values() if p['kind'] == 'mcp'})}; "
          f"subagent calls tagged: "
          f"{sum(1 for p in started.values() if p.get('agent_id'))}")
    mcp_out = [finished[k]["output_excerpt"][:120] for k, p in by_key.items()
               if p["tool"].startswith("mcp__tickets") and k in finished]
    print(f"MCP results: {mcp_out}")
    print(f"files changed (git): {sorted(git_files)}")
    print(f"files changed (observed by Continuum): {sorted(observed)}; "
          f"missed: {sorted(git_files - observed)}")
    print(f"unresolved: {[u['tool'] for u in unresolved]}")
    print(f"stored outputs/diffs: {blobs[0]} blobs, {blobs[1]} characters")
    times = [ln.split() for ln in (work / "hook-times.log").read_text().splitlines()]
    by_event: dict[str, list[int]] = {}
    for t in times:
        by_event.setdefault(t[1], []).append(int(t[0]))
    for event, ms in sorted(by_event.items()):
        ms.sort()
        print(f"hook {event}: n={len(ms)} median {statistics.median(ms):.0f} ms, "
              f"max {ms[-1]} ms")


if __name__ == "__main__":
    main()
