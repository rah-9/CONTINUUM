"""``continuum`` command-line interface. All output is JSON."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any

from continuum.core.canonical import canonical_json
from continuum.core.errors import ContinuumError
from continuum.core.runtime import RunResult
from continuum.core.tracing import configure_logging
from continuum.interfaces.sdk import Continuum


def _print(value: Any) -> None:
    if isinstance(value, RunResult):
        value = {**asdict(value), "status": value.status.value}
    json.dump(value, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="continuum", description=__doc__)
    p.add_argument("--log-level", default="WARNING",
                   help="JSON trace log level on stderr (e.g. INFO for every event)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("db-init", help="create the PostgreSQL schema")
    sub.add_parser("doctor", help="check database, schema, tasks and model profiles")
    sub.add_parser("tasks", help="list pre-registered tasks")
    sub.add_parser("runs", help="list run ids")
    run = sub.add_parser("run", help="start a pre-registered task")
    run.add_argument("task_id")
    run.add_argument("--model", dest="model_alias",
                     help="alias from config/models.yaml or a provider/model-id")
    res = sub.add_parser("resume", help="resume a run (never a live replay)")
    res.add_argument("run_id")
    res.add_argument("--model", dest="model_alias",
                     help="continue on this alias or provider/model-id "
                          "(e.g. anthropic/claude-opus-5-5); verified work carries over")
    st = sub.add_parser("status", help="show durable run status")
    st.add_argument("run_id")
    ins = sub.add_parser("inspect", help="side-effect-free diagnostic replay")
    ins.add_argument("run_id")
    tr = sub.add_parser("trace", help="export the run's JSON-lines trace (read-only)")
    tr.add_argument("run_id")
    tr.add_argument("--out", help="write to this file instead of stdout")
    rv = sub.add_parser("resolve", help="record a human decision for an action")
    rv.add_argument("run_id")
    rv.add_argument("action_id")
    rv.add_argument("decision", choices=["confirmed", "abort", "retry_at_risk", "approve",
                                         "reject"])
    rv.add_argument("--actor", required=True)
    rv.add_argument("--reason", required=True)
    rv.add_argument("--evidence", dest="evidence_ref")
    cn = sub.add_parser("cancel", help="stop new dispatches for a run")
    cn.add_argument("run_id")
    cn.add_argument("--actor", required=True)
    cn.add_argument("--reason", required=True)
    sv = sub.add_parser("serve", help="minimal loopback HTTP API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sub.add_parser("mcp", help="local stdio MCP facade (run_task, get_status)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    sdk = Continuum()
    try:
        match args.cmd:
            case "db-init":
                sdk.init_db()
                _print({"ok": True})
            case "doctor":
                report = sdk.doctor()
                _print(report)
                return 0 if report["ok"] else 1
            case "tasks":
                _print(sdk.tasks())
            case "runs":
                _print(sdk.runs())
            case "run":
                _print(sdk.start(args.task_id, model_alias=args.model_alias))
            case "resume":
                _print(sdk.resume(args.run_id, model_alias=args.model_alias))
            case "status":
                _print(sdk.status(args.run_id))
            case "inspect":
                _print(sdk.inspect(args.run_id))
            case "trace":
                lines = "".join(canonical_json(r) + "\n" for r in sdk.trace(args.run_id))
                if args.out:
                    with open(args.out, "w", encoding="utf-8") as fh:
                        fh.write(lines)
                    _print({"ok": True, "out": args.out})
                else:
                    sys.stdout.write(lines)
            case "resolve":
                _print(sdk.resolve_action(args.run_id, args.action_id, args.decision,
                                          actor=args.actor, reason=args.reason,
                                          evidence_ref=args.evidence_ref))
            case "cancel":
                _print(sdk.cancel(args.run_id, actor=args.actor, reason=args.reason))
            case "serve":
                # Optional extras ([http], [mcp]) are imported only when used.
                from continuum.interfaces.http_api import serve

                serve(sdk, host=args.host, port=args.port)
            case "mcp":
                from continuum.interfaces.mcp_facade import run_stdio

                run_stdio(sdk)
    except ContinuumError as exc:
        _print({"error": str(exc), "code": exc.code})
        return 2
    finally:
        sdk.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
