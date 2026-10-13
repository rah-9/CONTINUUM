# Continuum — specification index

**Spec version:** 0.2 (reviewed 2026-10-13)  
**Status:** implementation-ready MVP design; not a claim that code exists

**Purpose:** keep multi-step agent progress in runtime-owned, model-independent state. After an interruption or model handoff, reuse *valid verified work* and reconcile any uncertain external effect **before** new side-effecting work.

This is a **single project**. Continuum Lab is its in-repository fault-injection/evaluation suite, not a separate product.

## Read in order

1. [01-PRD.md](01-PRD.md): problem, hard requirements, exclusions, acceptance
2. [02-ARCHITECTURE-AND-FLOWS.md](02-ARCHITECTURE-AND-FLOWS.md): component boundaries, runtime and recovery flows
3. [03-STATE-SCHEMA.md](03-STATE-SCHEMA.md): durable entities, SQLite schema, invariants
4. [04-EVENT-LOG-AND-LEDGER.md](04-EVENT-LOG-AND-LEDGER.md): state transitions, identity, retry/reconciliation policy
5. [05-MODEL-ADAPTERS.md](05-MODEL-ADAPTERS.md): provider contract, capability limits and packet
6. [06-DEV-WORKFLOW.md](06-DEV-WORKFLOW.md): implementation sequence, repo structure, gates
7. [07-EVAL-PLAN.md](07-EVAL-PLAN.md): controlled tests, mocks, measurements
8. [../AGENTS.md](../AGENTS.md): binding coding-agent instructions

## Normative precedence

For any conflict: **04 governs effects/retries; 03 governs persisted state; 02 governs orchestration; 05 governs model boundaries; 01 governs scope and acceptance**. Do not resolve genuine contradictions silently—fix all affected documents before implementing.

## Core vocabulary

- **Task spec:** developer-defined goal, allowed tools, verifiable milestones, limits and business operation identities.
- **Run:** one execution across zero or more model sessions, identified by `run_id`.
- **Milestone:** a checkable intermediate outcome with dependencies and a deterministic verifier.
- **Evidence:** verifier-produced references, digests and observations (not a model's claim).
- **Logical action:** one intended tool-side effect identified by immutable `action_id` and stable `operation_key`. One action can have multiple physical **attempts**.
- **Unknown:** a dispatched action may or may not have committed. It is *not* equivalent to failure.
- **Reconciliation:** establish the actual effect from authoritative evidence; retry only under an explicitly safe contract, otherwise pause for human review.
- **Handoff:** restart model reasoning from a deterministic packet assembled from runtime state.
- **Continuum Lab:** the test harness under `eval/` in this same repository.

## Explicit guarantees and limits

- For tasks and tools meeting the declared contracts, Continuum **attempts to retain verified progress and avoid unsafe duplicate dispatches**. It does **not** guarantee exactly-once effects across arbitrary external services.
- SQLite is the **only** durable backend required in v1, for a local single-host runtime. Multiple workers claiming one run are blocked by lease/epoch rules, but external side effects still require reconciliation.
- v1 has a Python SDK, CLI, minimal localhost HTTP surface and a thin **local stdio MCP task-delegation facade** (`run_task`, `get_status`) built last. PostgreSQL, arbitrary MCP tool ingestion, code execution, dashboards and production hosting are **post-MVP**.

## Repo layout note

Place this file and `01`–`07` in `docs/`; place `AGENTS.md` at repo root. `docs/DECISIONS.md` and `docs/CHANGELOG.md` are created by implementation tasks as needed.
