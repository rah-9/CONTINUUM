# Implementation decisions

Each entry: context, decision, consequences. Contract-level changes are also in `CHANGELOG.md`.

## D-001 — PostgreSQL replaces SQLite as the v1 durable backend

**Context.** The v0.2 spec froze SQLite for v1. The project owner directed PostgreSQL instead, with every other technology and behaviour unchanged.

**Decision.** PostgreSQL 14+ (tested on 16) via psycopg 3. The deployment model stays local and single-host: one database, controllers on one machine, loopback-only HTTP. Mapping of the SQLite mechanisms:

| Spec rule (SQLite) | PostgreSQL implementation |
|---|---|
| `BEGIN IMMEDIATE` serialises seq assignment | `SELECT last_event_seq FROM runs ... FOR NO KEY UPDATE` inside the append transaction |
| `busy_timeout = 5000` | session `lock_timeout = 5000` |
| `journal_mode = WAL` | PostgreSQL WAL (built in) |
| `unixepoch('now')` lease time | `extract(epoch from clock_timestamp())` |
| canonical JSON in `TEXT` | `JSONB` |
| money as decimal text | `NUMERIC(20,10)` |
| ISO timestamps in `TEXT` | `TIMESTAMPTZ` |
| immutable events (by convention) | `BEFORE UPDATE OR DELETE` trigger raises |

The fence check share-locks the lease row (`FOR SHARE`) inside the append transaction, so a takeover cannot interleave with a fenced append. `FOR NO KEY UPDATE` is used rather than `FOR UPDATE` so it does not conflict with the `KEY SHARE` lock that the `run_leases` foreign key takes.

**Consequences.** All docs were amended (spec 0.3). Tests isolate each case in its own schema (`search_path`). Multi-host deployment stays out of scope: the lease still cannot recall external in-flight requests.

## D-002 — Dependencies: `psycopg[binary]` and `psycopg-pool`

psycopg 3 is the maintained PostgreSQL driver with server-side parameter binding and typed JSONB/`NUMERIC` adaptation. `psycopg-pool` avoids reconnecting on every short transaction. No ORM: the DDL is canonical and `projections.py` is the only place that maps events to rows.

## D-003 — One projection function for live appends and rebuilds

`core/projections.py` turns `(event, occurred_at, seq)` into row changes. `Store` applies them in SQL inside the event transaction, and rebuild/inspect applies them to an in-memory `ProjectionState`. Because one function serves both, I2 (rebuild equivalence) cannot drift, and `projection_diff()` compares durable rows against a replay.

## D-004 — Write actions are scoped to the focus milestone

The operation key for milestone-scoped tools uses the current **focus milestone**: the first milestone, in spec order, whose dependencies are all verified (docs/02 §5.6). This is deterministic from durable state, so the same proposal after a crash or handoff yields the same key. The model cannot choose the milestone.

## D-005 — Immutable bound arguments

`TrustedTool.bind()` turns a proposal into immutable execution arguments, for example by adding `content_sha256` for `store.put` or `fields_sha256` for `db.update`. `args_sha256` is computed over the bound arguments, so "same path, different bytes" becomes an `intent_conflict`. If arguments change under redaction (they contain secret material), the gateway rejects them, because persisted arguments must be enough to re-execute the action.

## D-006 — Reads

Every read is its own action (`read:<action_id>`) and is never deduplicated. A read's ambiguous failure is recorded as `failed_clean`, since a read cannot commit an effect. An interrupted read found `unknown` at recovery moves to `authorized` with `read_only_no_effect` evidence.

## D-007 — Verifiers are configured instances

A `verifier_id` names a parameterised verifier instance registered by the task definition, such as `CsvNormalized(source, output, …)`. The spec pins `verifier_version`, and run creation rejects a version mismatch. On every resume and before completion, verifiers re-run. A failed check, or a fingerprint that no longer matches, invalidates the milestone and all its descendants.

## D-008 — Settling effects on resume, before any new write

On resume the runtime does three things, in order:

1. It dispatches `authorized` writes that came from an explicit decision: a retry after earlier attempts (human `retry_at_risk`, or race-safe absence) or a human approval.
2. It reconciles `unknown` actions.
3. If anything remains unresolved, it stops in `awaiting_reconciliation`.

An idempotent same-key retry whose outcome is ambiguous a second time escalates to `needs_human`.

## D-009 — Human decisions share one endpoint

`resolve_action` accepts `confirmed` (requires an `evidence_ref`), `abort`, and `retry_at_risk` for unresolved effects, and `approve`/`reject` for approval-gated actions. `abort` skips the action and fails the run with `human_abort`. A human-confirmed effect still needs its milestone verifier to pass.

## D-010 — Local file tools are idempotent writes

`csv.normalize` and `report.write` are `write` tools with idempotency `supported` and no retention limit, because their output is a deterministic atomic write within the workspace. Interrupted local writes are retried as the same action with the same key.

## D-011 — Continuum Lab package layout

`eval/` holds the importable package `continuum_lab` (`eval/continuum_lab/...`), because `eval` would shadow the builtin's name in tracebacks and tooling. The mock services persist state in a lock-guarded JSON file, so ground truth survives real process kills.

## D-012 — Baseline (B0) choices

B0 runs the same plan-following fake agent, tools, permission checks, budgets and scorer as C1. Its differences:

- It sends no idempotency keys, because a plain loop has no stable action identity.
- It is allowed to "check before repeating" a write whose outcome was an error or missing.
- After a crash it hands off to the next model with its persisted transcript.
- It auto-approves approval-gated writes through a scripted operator, counted as a human escalation.

The report discloses these differences.

## D-013 — MCP SDK 2.x

The local stdio facade uses `mcp>=2` (`MCPServer`; `FastMCP` was renamed in 2.0). It exposes only `run_task` and `get_status` for pre-registered tasks. The facade logic (`McpFacade`) does not depend on the transport, so it can be tested without stdio.

## D-014 — Explicit stop when drift needs remediation

**Context.** Before this change, a drifted milestone whose writes were already confirmed could never be redone, because its operation keys deduplicate. The run then looped through model handoffs until it failed with `models_exhausted`, wasting model calls and hiding the cause.

**Decision.** After drift re-verification, on resume and before completion, the runtime checks every invalidated milestone, including descendants. If any of them has a confirmed write action, it records `RunFailed(drift_requires_remediation)` immediately. Milestones without confirmed writes are simply redone. This follows docs/03 §5: "stop and require a distinct task/spec version or human-approved remediation".

## D-015 — Background lease heartbeat

`LeaseKeeper` renews the lease on a daemon thread while a controller runs. The SDK sets the interval to TTL/3. The keeper calls the same fenced `renew` as the main loop, so it can only extend the same `(owner, epoch)` and never take a lease over. If a renewal fails, the keeper stops, and the existing pre-dispatch and fenced-write checks halt the controller. Tests and the lab leave it disabled (`heartbeat_interval_seconds=None`), because they move lease time with a fake clock.

## D-016 — Tracing contains identifiers, never payloads

The JSON trace logger (`continuum.trace`) records only IDs, event types, tool names, statuses, attempt numbers and durations. Arguments, results and model text stay in the event log and in redacted blobs. This keeps logs safe to ship anywhere. The full diagnostic data is still available through read-only `inspect` (timeline) and `trace` (JSON lines with blobs inlined).

## D-017 — The registered task id is the run's identity

Several registry entries can reuse one task definition, for example the demo tasks. The registry stamps the registered id onto the spec's `task_id`, so `resume`, `status` and `resolve` find the same binding, with the same tool and mock-service state, that started the run.

## D-018 — Lab reports "clean completion"

Verified completion counts a run as successful when its final state is correct. A baseline run that uploaded twice can still meet that bar. The lab now also reports **clean completion**: verified completion with zero committed duplicates and zero effects on unintended targets. It is shown next to verified completion and also as a paired difference. Neither metric replaces the other.
