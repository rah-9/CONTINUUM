# Product requirements — Continuum

**Version:** 0.2 · **Phase:** scoped local MVP · **Owner:** project maintainer

## 1. Objective and problem

Continuum is a model-agnostic **agent continuity runtime**. It owns durable task state, verified milestone evidence and a ledger of external effects independent of any provider's chat history.

It addresses **two failure modes**:

1. **Lost progress:** after a crash, model switch, timeout or malformed response, verified work is repeated or discarded.
2. **Unsafe repeats:** a tool may have committed an external effect before its response was lost; retrying can cause duplicate uploads, updates or messages.

Secondary correctness issue: an agent's self-report must not substitute for independent verification.

**Research claim to test:** compared with a fair tool-loop baseline, portable verified state plus a durable action ledger improves post-failure completion and preserves work while reducing unintended duplicate effects. A negative outcome is a legitimate benchmark result.

## 2. Scope / explicit exclusions

**In v1:** one run at a time per ownership lease; milestone-driven workflow; PostgreSQL append-only events and transactional projections; read/write tool gateway; durable action identity and retries; unknown-action reconciliation; deterministic verifiers; two-provider handoff supported and three independently tested provider configurations as benchmark goal; Python SDK/CLI; minimal localhost HTTP access; a thin local stdio MCP task-delegation facade; local mocks and integrated Continuum Lab.

**Out of v1:** generic planning, multi-agent coordination, arbitrary external MCP servers, hosted multi-tenancy, vector DB, long-term chat memory, dashboard, multi-host/distributed database deployment, distributed orchestration, sandboxed arbitrary code/shell execution, live side-effect replay, VS Code/browser plugins and remote MCP tool **discovery/hosting**. Document their future interface boundaries only; do not implement speculatively.

Tools run as **trusted, explicitly registered Python adapters** with schema validation, allowlists and fixed endpoints. Arbitrary tool code supplied by a model is disallowed.

## 3. User stories and acceptance requirements

- **U1 / continuity:** terminate the process after milestone 2, resume with provider B, and do not repeat milestones whose evidence and dependencies remain valid.
- **U2 / ambiguous effect:** mock service commits upload but drops response; restart sees `unknown`, recovers the committed effect and does not commit a duplicate.
- **U3 / opaque effect:** after ambiguous send with no trustworthy check or idempotency contract, pause and require human resolution; never auto-repeat.
- **U4 / portability:** reject incompatible model capabilities explicitly; serialize a valid continuation packet within its context limit.
- **U5 / accuracy:** only verifier outputs mark milestones verified. If inputs/outputs change, invalidate affected milestones and descendants.
- **U6 / safety:** two processes racing to resume one run cannot both acquire controller ownership; stale owners cannot authorize another tool attempt.
- **U7 / proof:** Continuum Lab compares matched baseline/Continuum runs, publishing failure counts, uncertainty and cost.

## 4. Required behavior

**R1 Task spec:** immutable snapshot of goal, instructions, milestones, verifier versions, dependencies, allowed tool manifests, operation-key policy, model routing and hard budgets. Milestones form an acyclic dependency graph.

**R2 Durable state:** one PostgreSQL transaction appends the event **and applies its projections atomically**. Rebuild from events is deterministic. No durable transition relies only on volatile model text.

**R3 Single owner:** lease and monotonically increasing fencing epoch per run; enforce epoch on every durable state update and immediately before tool dispatch. Recovery treats earlier unfinished dispatches as possibly committed.

**R4 Tool gateway:** every model proposal and trusted verifier/reconciler read is validated and ledgered; side-effecting calls require stable operation identity, immutable args, permission checks and an appropriate manifest-defined retry/reconciliation contract. A repeated proposal for the **same operation** reuses the existing action, not a new effect.

**R5 Unknown recovery:** never turn a timeout into `failed_clean`; a negative query is not enough to establish non-execution unless the manifest explicitly proves an authoritative, race-safe absence contract. Otherwise stay unresolved/ask a human.

**R6 Idempotency:** retries of one logical action use the same key while the target's documented dedupe guarantee remains valid. Keys never change on model handoff. If retention/behavior cannot be relied upon, do not auto-retry an ambiguous write.

**R7 Verification:** deterministic verifier records artifact hashes, input/dependency fingerprints, verifier version and independent service observations as applicable. Confirming a tool action is **not** automatically verifying its milestone.

**R8 Handoff:** packet derived from task spec, projections and evidence; unverified model narrative is excluded from authoritative sections. Unknown effects are reconciled or run pauses **before any new write**. Different provider tool-call formats never reach the gateway unvalidated.

**R9 Budgets:** enforce max model calls, tokens, spending, active-execution duration (excluding manual pause and process downtime), and recovery count. A provider switch can avoid provider-specific limits but **cannot bypass the run's aggregate budget**. Exhausted hard budgets stop the run; resume requires a new authorized policy/version, not silent reset.

**R10 Interfaces:** SDK (`start`, `resume`, `status`, `resolve_action`), local CLI, minimal HTTP (`POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/resume`, `POST /runs/{id}/actions/{action_id}/resolve`) and a thin local stdio MCP facade with `run_task` and `get_status` for pre-registered task specs. HTTP must remain bound to loopback in v1. Do not expose an unauthenticated production service.

**R11 Replay:** side-effect-free diagnostic replay from recorded events and tool/model responses only. Resume is a separate controlled operation, never marketed as live replay.

**R12 Tests:** deterministic fake adapter, mock read/write services, crash boundary tests, evidence drift, lease races, duplicate model proposals, idempotency expiration, eventual-consistency traps and baseline comparison.

## 5. Non-functional requirements

- PostgreSQL row locks (`FOR NO KEY UPDATE` on the run row) + `lock_timeout`, short transactions, no DB lock held over model or tool network calls. A single machine is supported; no cross-host guarantee.
- Per-run workspace with path containment, atomic file replacement and sha256 evidence. External inputs are snapshotted or versioned; changed inputs invalidate dependent milestones.
- Secrets are supplied only from environment/config, never serialized to task events, packets or reports. Redact/error-filter before persistence. Treat tool outputs as untrusted data.
- Structured JSON local tracing is required; OTel integration is optional after MVP. No arbitrary shell, network sandbox or Docker runtime required for core v1.
- Monetary usage may be estimated; unknown usage must be marked unknown, not represented as zero. Include retries in accounting.

## 6. Success and release criteria

Evaluate with the same tasks, tool behavior, scorer and resource limits as a baseline using retries and transcript handoff. Report independently:

- verified task completion, successful model handoff, correctly preserved verified milestones;
- unintended duplicate committed effects, attempted duplicates blocked, unresolved/human-escalated effects;
- recovery latency, tokens, total cost and cost per success;
- unauthorized tool dispatches and false milestone completions.

**Safety release gates (deterministic fixtures):** zero automatic repeated commits on correctly configured idempotent fixtures; zero auto-repeat of unresolved opaque actions; zero permission bypasses; no duplicate concurrent dispatch from competing owners in protected fixture; no false milestone verification after drift. **Research outcome gates:** publish honest comparative results and confidence intervals; no claim of superiority from an underpowered benchmark.

Target **15–20 checkable tasks**, three provider configurations, two or more repetitions. This is a *study target*, not a promise of statistical significance.

## 7. Risks and assumptions

- Idempotency behavior and TTL differ across external providers. Tests verify a connector's declared contract; incorrect manifests invalidate guarantees.
- Event sourcing adds complexity. Keep one PostgreSQL event transaction and simple derived tables; no distributed event bus or hash-chained audit log.
- Source-level task steps cannot always be matched semantically across different models. MVP requires developer-defined milestone IDs and operation-key extraction rules.
- A local controller lease cannot stop a request already in flight at an external service. Reconcile after takeover; do not overclaim fencing.
- Reliable *negative* outcome evidence may not exist. Favor a human pause over an unsafe retry.
- LLM calls are stochastic, and two repetitions are exploratory. Publish per-run traces and exact model IDs/pricing snapshot.

## 8. Delivery checkpoints

Week 1: state/event transactions, lease and ledger. Week 2: gateway, reconciliation and verifiers with fake agents. Week 3: model adapter and cross-provider handoff. Week 4: CLI/local API, fault injection, baseline and report. See `06-DEV-WORKFLOW.md` for exact acceptance tests.
