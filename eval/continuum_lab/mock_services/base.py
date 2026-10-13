"""File-backed mock service core.

State lives in one JSON file guarded by an ``fcntl`` lock so ground truth
survives simulated and real process kills. Write behaviours are scripted per
operation and consumed in order:

* ``ok``                  commit and respond
* ``commit_then_drop``    commit, then lose the response (client sees timeout)
* ``drop_before_commit``  lose the request before commit (client sees timeout)
* ``commit_late``         accept, respond with timeout, commit only after
                          ``pending_ops`` further service operations
* ``reject``              definitive rejection before any effect

In ``deduped`` mode a request whose idempotency key matches a committed request
within retention, or one still processing, does not commit again.
"""

from __future__ import annotations

import fcntl
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from continuum.core.canonical import canonical_json
from continuum.tools.trusted.services import ServiceMode, ServiceRejected, ServiceTimeout

Behaviour = Literal["ok", "commit_then_drop", "drop_before_commit", "commit_late", "reject"]
State = dict[str, Any]


class MockService:
    kind = "service"

    def __init__(self, state_path: Path, *, mode: ServiceMode, retention_seconds: int = 86_400,
                 read_lag: int = 0, pending_ops: int = 2,
                 now_fn: Callable[[], float] = time.time) -> None:
        self.state_path = state_path
        self.mode: ServiceMode = mode
        self.retention_seconds = retention_seconds
        self.read_lag = read_lag
        self.pending_ops = pending_ops
        self.now_fn = now_fn
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._txn():
            pass

    # ---- persistence ------------------------------------------------------------------

    def _empty(self) -> State:
        return {"data": {}, "commits": [], "dedupe": {}, "script": {}, "pending": [],
                "lag": {}, "attempts": [], "seq": 0}

    @contextmanager
    def _txn(self) -> Iterator[State]:
        lock_path = self.state_path.with_suffix(".lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = (json.loads(self.state_path.read_text())
                         if self.state_path.exists() else self._empty())
                yield state
                tmp = self.state_path.with_suffix(".tmp")
                tmp.write_text(canonical_json(state))
                tmp.replace(self.state_path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    # ---- scripting / ground truth -------------------------------------------------------

    def script(self, op: str, *behaviours: Behaviour) -> None:
        with self._txn() as s:
            s["script"].setdefault(op, []).extend(behaviours)

    def ground_truth(self) -> list[dict[str, Any]]:
        """Every committed mutation, independent of agent-visible results."""
        with self._txn() as s:
            return list(s["commits"])

    def attempts_log(self) -> list[dict[str, Any]]:
        """Every write request that reached the service (committed or not)."""
        with self._txn() as s:
            return list(s["attempts"])

    def settle(self) -> None:
        """Apply every pending late commit and end read lag (end of experiment)."""
        with self._txn() as s:
            for p in s["pending"]:
                self._apply(s, p["target"], p["value"], p["key"])
            s["pending"] = []
            s["lag"] = {}

    # ---- core write path ------------------------------------------------------------------

    def _tick(self, s: State) -> None:
        still = []
        for p in s["pending"]:
            p["after"] -= 1
            if p["after"] <= 0:
                self._apply(s, p["target"], p["value"], p["key"])
            else:
                still.append(p)
        s["pending"] = still

    def _apply(self, s: State, target: str, value: dict[str, Any], key: str | None
               ) -> dict[str, Any]:
        s["seq"] += 1
        receipt = {"receipt_id": f"{self.kind}-{s['seq']}", "target": target,
                   "version": s["seq"]}
        s["data"][target] = {**value, "version": s["seq"], "receipt_id": receipt["receipt_id"]}
        s["commits"].append({"target": target, "value": value, "idempotency_key": key,
                             "seq": s["seq"], "at": self.now_fn()})
        if key is not None:
            s["dedupe"][key] = {"receipt": receipt, "at": self.now_fn()}
        s["lag"][target] = self.read_lag
        return receipt

    def _write(self, op: str, target: str, value: dict[str, Any], key: str | None
               ) -> dict[str, Any]:
        # Decide and persist inside the lock; raise only after state is saved, so a
        # committed-then-dropped effect is durably part of ground truth.
        error: Exception | None = None
        with self._txn() as s:
            self._tick(s)
            queue = s["script"].get(op, [])
            behaviour: Behaviour = queue.pop(0) if queue else "ok"
            s["attempts"].append({"op": op, "target": target, "idempotency_key": key,
                                  "behaviour": behaviour})
            receipt: dict[str, Any] = {}
            if behaviour == "reject":
                error = ServiceRejected("rejected", f"{op} rejected by service")
            elif behaviour == "drop_before_commit":
                error = ServiceTimeout(f"{op}: no response")
            elif self.mode == "deduped" and key is not None and \
                    any(p["key"] == key for p in s["pending"]):
                # Same key while the original is still processing: the service
                # serializes them; the original completes and this one dedupes.
                original = next(p for p in s["pending"] if p["key"] == key)
                s["pending"].remove(original)
                receipt = {**self._apply(s, original["target"], original["value"], key),
                           "deduplicated": True}
                if behaviour == "commit_then_drop":
                    error = ServiceTimeout(f"{op}: response lost")
            elif self.mode == "deduped" and key is not None and key in s["dedupe"] and \
                    self.now_fn() - s["dedupe"][key]["at"] < self.retention_seconds:
                receipt = {**s["dedupe"][key]["receipt"], "deduplicated": True}
                if behaviour == "commit_then_drop":
                    error = ServiceTimeout(f"{op}: response lost")
            elif behaviour == "commit_late":
                s["pending"].append({"target": target, "value": value, "key": key,
                                     "after": self.pending_ops})
                error = ServiceTimeout(f"{op}: accepted, response lost")
            else:
                receipt = self._apply(s, target, value, key)
                if behaviour == "commit_then_drop":
                    error = ServiceTimeout(f"{op}: response lost after commit")
        if error is not None:
            raise error
        return receipt

    def _read(self, target: str) -> dict[str, Any] | None:
        with self._txn() as s:
            self._tick(s)
            lag = s["lag"].get(target, 0)
            if lag > 0:
                s["lag"][target] = lag - 1
                return None  # eventually consistent: committed but not yet visible
            item = s["data"].get(target)
            return dict(item) if item is not None else None

    def commits_for(self, target: str) -> int:
        return sum(1 for c in self.ground_truth() if c["target"] == target)
