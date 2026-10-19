# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Replay a real Claude Code transcript through the hook sequence it produced, and check
that the journal kept the whole conversation: every user message (repeats included) and
every agent text message, with no duplicates.

    python replay_capture.py TRANSCRIPT.jsonl [--outage-turns N]

Per user turn: UserPromptSubmit (the prompt), then Stop (the transcript as it was at the
end of that turn). The first N turns' hooks fail (database down), as in Trial 5; a final
SessionEnd re-reads the whole transcript. Needs PostgreSQL (CONTINUUM_TEST_DATABASE_URL).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import uuid
from collections import Counter
from pathlib import Path

from continuum.coding import hooks
from continuum.coding.service import CodingContinuity
from continuum.coding.transcript import _record_id, extract
from continuum.core.clock import SystemClock
from continuum.core.ids import RandomIds
from continuum.core.store import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript")
    ap.add_argument("--outage-turns", type=int, default=4)
    args = ap.parse_args()
    lines = Path(args.transcript).read_text().splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    truth = extract(records)
    # the record index at which each user message appears
    # (only identified records are messages; the others are request/cache copies)
    starts = [i for i, r in enumerate(records) if _record_id(r) is not None and any(
        m.role == "user" for m in extract([r]))]
    dsn = os.environ.get("CONTINUUM_TEST_DATABASE_URL",
                         "postgresql://continuum:continuum@localhost:5432/continuum_test")
    schema = "replay_" + uuid.uuid4().hex[:8]
    store = Store(dsn, schema=schema, clock=SystemClock(), ids=RandomIds())
    store.migrate()
    coding = CodingContinuity(store, clock=SystemClock(), ids=RandomIds())
    try:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "proj"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            partial = Path(tmp) / "t.jsonl"
            base = {"session_id": "chat-1", "cwd": str(repo)}
            for t, start in enumerate(starts):
                end = starts[t + 1] if t + 1 < len(starts) else len(records)
                if t < args.outage_turns:
                    continue  # the database was down: these hooks only logged an error
                [prompt] = [m.text for m in extract([records[start]]) if m.role == "user"]
                hooks.handle("claude-code", {**base, "hook_event_name": "UserPromptSubmit",
                                             "prompt": prompt}, coding)
                partial.write_text("\n".join(lines[:end]) + "\n")
                hooks.handle("claude-code", {**base, "hook_event_name": "Stop",
                                             "transcript_path": str(partial)}, coding)
            partial.write_text("\n".join(lines) + "\n")
            hooks.handle("claude-code", {**base, "hook_event_name": "SessionEnd",
                                         "transcript_path": str(partial)}, coding)
            sid = coding.latest_active(str(repo))
            events = coding._events(sid)[1]
            view_order = [c["text"] for c in coding._view(sid).chat]
        if os.environ.get("REPLAY_DEBUG"):
            for e in events:
                p = e["payload_json"]
                if e["type"] == "ChatCaptured" and p["role"] == "user" and (
                        p["text"].startswith("Add `rm") or p["text"] == "continue"):
                    print("  ", e["seq"], repr(p["text"][:20]), p["key"][-30:], p.get("alias"))
        shown = [e["payload_json"] for e in events
                 if e["type"] == "ChatCaptured" and not e["payload_json"].get("alias")]
        for role in ("user", "agent", "action"):
            want = Counter(m.text for m in truth if m.role == role)
            got = Counter(p["text"] for p in shown if p["role"] == role)
            missing, extra = want - got, got - want
            if os.environ.get("REPLAY_DEBUG"):
                print("  extra:", [t[:40] for t in extra.elements()],
                      "missing:", [t[:40] for t in missing.elements()])
            print(f"{role:6s}: transcript {sum(want.values())}, journal "
                  f"{sum(got.values())}, missing {sum(missing.values())}, "
                  f"duplicated {sum(extra.values())}")
        print("brief sees the transcript's order:", view_order == [m.text for m in truth])
    finally:
        store.close()
        with __import__("psycopg").connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE'.encode())


if __name__ == "__main__":
    main()
