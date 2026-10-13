# Implementation plan and coding-agent workflow

## 1. Engineering rules

- One narrow feature per branch. Implement tests for state/ledger/recovery **before** relying on live provider calls.
- The coding agent must consult `AGENTS.md` and the relevant spec. Do not implement aspirational features that the v1 PRD excludes.
- Local, deterministic fake models and services are the default. Live API tests are opt-in and may incur cost.
- Any change to schemas, transition rules or packet semantics must update **all** affected docs in the same PR.

## 2. Target repository structure

```text
continuum/
  AGENTS.md
  README.md
  pyproject.toml
  docs/
    00-README.md ... 07-EVAL-PLAN.md
    DECISIONS.md                 # implementation starts this log
    CHANGELOG.md
  config/
    models.yaml                 # exact IDs filled during development
    tasks/                      # controlled verifiable tasks
  src/continuum/
    core/
      state.py                  # typed specs and state enums
      events.py                 # canonical event encoder
      store.py                  # PostgreSQL transactions and projections
      schema.sql                # canonical DDL (mirrored in 03)
      projections.py            # pure event -> row changes (live + rebuild)
      leases.py                 # owner and fencing epoch
      ledger.py                 # logical actions + attempts
      reconciliation.py
      evidence.py               # hashes/fingerprints
      runtime.py                # controller + resume
      handoff.py                # portable packet builder
    models/
      base.py
      litellm_adapter.py
      fake.py
    tools/
      manifest.py
      gateway.py
      trusted/                   # read, csv, mock DB/store/mail
    verify/
      registry.py
      builtins.py
    interfaces/
      sdk.py
      cli.py
      http_api.py               # loopback only
      mcp_facade.py             # local stdio, pre-registered task delegation
  eval/                          # Continuum Lab: part of this repo
    mock_services/
    fault_injection.py
    tasks/
    baseline.py
    runner.py
    metrics.py
    report.py
  tests/
    unit/
    integration/
    contract/
    chaos/
```

**Dependencies:** Python 3.12+, Pydantic v2, PostgreSQL 14+ via psycopg 3 (+ psycopg-pool), FastAPI only for HTTP, LiteLLM for live adapters, JSON Schema validator, pytest, hypothesis, ruff and mypy. Other packages need a measured reason recorded in `DECISIONS.md`. No Docker runtime, Redis, Temporal/DBOS, agent framework, UI stack or vector DB required in v1.

## 3. Four-week implementation sequence (deliver slices, not calendar guarantees)

### Week 1 — core state and identity

- W1-1: scaffold repo, types, strict spec/manifest validation, fake clock and deterministic IDs.
- W1-2: PostgreSQL schema, run-row-locked event+projection atomicity, rebuild test.
- W1-3: lease acquisition/renewal/epoch and concurrent-resume test using **two processes**.
- W1-4: immutable `(run_id, operation_key)` action identity, attempt rows, idempotency keys and transition tests.
- W1-5: crash boundary test around `ActionAttemptStarted` before/after simulated commit.

**Gate:** restarting an interrupted synthetic action produces either confirmed evidence or `unknown`; duplicate proposal cannot create a second logical action.

### Week 2 — safe gateway and evidence

- W2-1: trusted tool manifest and argument/permission validator; serialized gateway.
- W2-2: idempotent + queryable + opaque mock connectors and reconciler.
- W2-3: human `resolve_action` CLI/SDK (confirmed with evidence, abort, retry_at_risk).
- W2-4: milestone verifier registry, artifact hashing, input/dependency version fingerprints and cascading invalidation.
- W2-5: a complete fake-model CSV -> object-store workflow with commit-then-timeout recovery.

**Gate:** no blind retry after ambiguous opaque effect; a stale lease owner cannot dispatch; drift invalidates dependent milestones.

### Week 3 — model portability

- W3-1: fake adapter contract and provider error taxonomy.
- W3-2: LiteLLM transport and contract validation against two real providers; add third provider profile/test as resources allow.
- W3-3: context/packet builder and model handoff from verified state; reject unsupported capabilities.
- W3-4: aggregate budget enforcement and API/backoff handling.
- W3-5: end-to-end handoff after a committed unknown action (must reconcile first).

**Gate:** second model resumes only unfinished work and sees no unverified claim as proof. At least two provider configurations successfully complete the demo; third is benchmark goal.

### Week 4 — evaluation and minimal interface

- W4-1: baseline tool loop with transcript handoff, same trusted tool APIs and final verifier.
- W4-2: 15 checkable mock tasks, deterministic fault injection at verified progress boundaries and write-ack loss.
- W4-3: paired experiments, raw result JSON, Wilson/bootstrap intervals where appropriate, concise Markdown report.
- W4-4: SDK, CLI, minimal FastAPI loopback endpoints and local stdio MCP server (delegation + read-only inspection for registered task specs; see `integrations/`); repeatable demo and documentation.

**Gate:** deterministic safety tests pass; at least smoke + fault/handoff matrix completed and traceable. If cost prevents all live experiments, publish partial coverage, do not pretend the whole suite ran.

## 4. Task assignment template for coding agents

```text
Task ID: W2-2
Read: AGENTS.md; docs/03-STATE-SCHEMA.md; docs/04-EVENT-LOG-AND-LEDGER.md
Goal: implement reconciliation against trusted mock services
Files allowed: core/reconciliation.py, tools/trusted/, tests/integration/
Must preserve: I4, I5, I6 and no auto-repeat on negative eventual query
Test cases: commit_then_drop, absent_but_pending_commit, expired_dedupe_key, opaque timeout
Out of scope: MCP discovery, real email providers, UI
Done when: tests and type checks pass; docs and changelog updated if contracts change
```

## 5. Required CI/testing layers

- **Unit:** state transitions, operation key derivation, immutable argument conflicts, packet determinism, input fingerprints and budgeting.
- **Property:** event replay equivalence, gapless sequence, idempotency key stability, duplicate-proposal safety.
- **Integration:** gateway + PostgreSQL + fake adapters + mock services.
- **Chaos/process:** kill at each write-ahead/commit boundary; parallel resumers; expired lease with old in-flight response.
- **Contract/live opt-in:** three provider configurations with exact IDs and recorded model characteristics.
- **Security:** secrets redacted; path traversal rejected; arbitrary tool execution denied; model prompt injection cannot change allowlists.

**Mandatory code-review questions:** Is the DB event+projection update atomic? Can a model manufacture a fresh operation key for an existing write? Can a negative read race a late commit? Can the old controller act after takeover? Is verification tied to unchanged input/dependency evidence? Does any diagnostic command invoke a live tool? Is any unknown cost silently treated as zero?

## 6. Decisions already frozen for v1

- PostgreSQL-only (single host, one database) and local API only.
- Event store is authoritative; leases are ephemeral infrastructure metadata.
- Explicit task-defined milestones and operation identity; no model-generated milestones.
- Trusted Python tools and a local stdio MCP server limited to task delegation and read-only inspection (no human decisions); no arbitrary code executor or remote MCP tool discovery.
- `inspect/replay` is side-effect-free; actual continuation happens via `resume`.
- Continuum Lab is internal `eval/`; not a separate service/product.

Future components may be recorded in `DECISIONS.md` but should not generate implementation tickets until MVP evidence justifies them.
