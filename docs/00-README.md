# Continuum — specification index

**Spec version:** 0.8 (2026-10-19: conversation consolidation and two-sided capture, D-037/D-038; PostgreSQL backend, D-001; MCP plugin surface, D-022; coding-session continuity, D-028/D-029; content fingerprint and code map, D-034/D-035; capture across apps and checkouts, D-036)  
**Status:** v0.9.0 implementation in `src/`, `eval/` and `tests/`; see `CHANGELOG.md`, `VERIFICATION.md`, `HANDOFF-TRIALS.md` and `MEMORY-QUALITY.md`

**Purpose:** keep multi-step agent progress in runtime-owned, model-independent state. After an interruption or model handoff, reuse *valid verified work* and reconcile any uncertain external effect **before** new side-effecting work.

This is a **single project**. Continuum Lab is its in-repository fault-injection/evaluation suite, not a separate product.

## Read in order

1. [01-PRD.md](01-PRD.md): problem, hard requirements, exclusions, acceptance
2. [02-ARCHITECTURE-AND-FLOWS.md](02-ARCHITECTURE-AND-FLOWS.md): component boundaries, runtime and recovery flows
3. [03-STATE-SCHEMA.md](03-STATE-SCHEMA.md): durable entities, PostgreSQL schema, invariants
4. [04-EVENT-LOG-AND-LEDGER.md](04-EVENT-LOG-AND-LEDGER.md): state transitions, identity, retry/reconciliation policy
5. [05-MODEL-ADAPTERS.md](05-MODEL-ADAPTERS.md): provider contract, capability limits and packet
6. [06-DEV-WORKFLOW.md](06-DEV-WORKFLOW.md): implementation sequence, repo structure, gates
7. [07-EVAL-PLAN.md](07-EVAL-PLAN.md): controlled tests, mocks, measurements
8. [../AGENTS.md](../AGENTS.md): binding coding-agent instructions
9. [MEMORY-ALGORITHM.md](MEMORY-ALGORITHM.md): the coding-continuity algorithm, its bounds and scenario matrix

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
- **Coding session:** an append-only journal of one piece of coding work in one repository (tasks, notes, checkpoints, check results) that any model or chat can resume. See docs/02 §10.
- **Coding brief:** what the next model receives on `coding_resume`: verified facts (code fingerprint, check results, the code map of files the session changed, commits since the last checkpoint) separated from agent notes (claims). Bounded and compact; built by plain code, no LLM.
- **Captured chat:** the user's own words (and the agent's last report) recorded by the coding app's hooks, not by a model. They reach the next model, in any app, as requirements. See `MEMORY-ALGORITHM.md`.
- **Registered check:** a command (argv, no shell) that a *human* registers for a repository, such as the test suite. A coding task is verified only by its checks passing on the current code.
- **Continuum Lab:** the test harness under `eval/` in this same repository.

## Explicit guarantees and limits

- For tasks and tools meeting the declared contracts, Continuum **attempts to retain verified progress and avoid unsafe duplicate dispatches**. It does **not** guarantee exactly-once effects across arbitrary external services.
- PostgreSQL (14+) is the **only** durable backend in v1, for a local single-host runtime (one database, controllers on one machine). Multiple workers claiming one run are blocked by lease/epoch rules, but external side effects still require reconciliation.
- v1 has a Python SDK, CLI, minimal localhost HTTP surface and a **local stdio MCP server** for task delegation and read-only inspection (`list_tasks`, `run_task`, `resume_run`, `cancel_run`, `get_status`, `inspect_run`, `pending_decisions`, `list_runs`), so any MCP-capable coding agent can use Continuum as a plugin. The same server offers **coding-session continuity** (`coding_*` tools including the read-only `coding_recall`, docs/02 §10). App hooks (`continuum hook <app>`) capture the conversation without any model so coding work survives a switch of model, version, vendor or chat. Human decisions and check registration are never exposed through MCP. Distributed/multi-host deployment, arbitrary MCP tool ingestion, code execution, dashboards and production hosting are **post-MVP**.

## Repo layout note

This file and `01`–`07` live in `docs/`; `AGENTS.md` is at the repo root. `docs/DECISIONS.md` records implementation decisions and `docs/CHANGELOG.md` contract changes.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._
