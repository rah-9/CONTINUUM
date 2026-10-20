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

**Out of v1:** generic planning, multi-agent coordination, arbitrary external MCP servers, hosted multi-tenancy (several people sharing one self-hosted database, each isolated by their own PostgreSQL role, is in scope: R15), vector DB, long-term chat memory, dashboard, multi-host/distributed database deployment, distributed orchestration, sandboxed arbitrary code/shell execution (running a *human-registered* coding check under R13 is not arbitrary execution: no model supplies a command), live side-effect replay, native IDE/browser extensions and remote MCP tool **discovery/hosting**. (Shipping MCP *client configuration* so agents can launch the local stdio server is in scope.) Document their future interface boundaries only; do not implement speculatively.

Tools run as **trusted, explicitly registered Python adapters** with schema validation, allowlists and fixed endpoints. Arbitrary tool code supplied by a model is disallowed.

## 3. User stories and acceptance requirements

- **U1 / continuity:** terminate the process after milestone 2, resume with provider B, and do not repeat milestones whose evidence and dependencies remain valid.
- **U2 / ambiguous effect:** mock service commits upload but drops response; restart sees `unknown`, recovers the committed effect and does not commit a duplicate.
- **U3 / opaque effect:** after ambiguous send with no trustworthy check or idempotency contract, pause and require human resolution; never auto-repeat.
- **U4 / portability:** reject incompatible model capabilities explicitly; serialize a valid continuation packet within its context limit.
- **U5 / accuracy:** only verifier outputs mark milestones verified. If inputs/outputs change, invalidate affected milestones and descendants.
- **U6 / safety:** two processes racing to resume one run cannot both acquire controller ownership; stale owners cannot authorize another tool attempt.
- **U8 / coding handoff:** a coding agent on model A (e.g. Claude Opus 4.8, or GPT-5.6 in Codex) records tasks, decisions and check results while it works. Model B, in a new chat or process, resumes the same repository and sees the verified state of the code, which tasks are proven done, what the previous agent decided and what to do next. Work verified on code that has since changed, or by a check whose definition changed, must be re-verified.
- **U9 / across apps, machines and abrupt ends:** the user works in one coding app (e.g. Claude Code) and continues in another (e.g. Codex), in another chat, or in another checkout of the same repository (another machine, a cloud container). This includes the case where the first agent never used Continuum and its chat ended without warning. The next agent still receives the user's own requests verbatim (captured by the app's hooks, never by a model), where the code changed, and the previous agent's last report labelled as a claim. Nothing is joined by guessing: another checkout is recognised only by the same remote and branch. Capture never blocks or breaks the app. The algorithm and its scenario matrix are in `MEMORY-ALGORITHM.md`.
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

**R10 Interfaces:** SDK (`start`, `resume`, `status`, `resolve_action`), local CLI, minimal HTTP (`POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/resume`, `POST /runs/{id}/actions/{action_id}/resolve`) and a local stdio MCP server for pre-registered task specs: delegation and read-only inspection (`list_tasks`, `run_task`, `resume_run`, `cancel_run`, `get_status`, `inspect_run`, `pending_decisions`, `list_runs`) plus coding-session continuity (R13). Human decisions (`resolve_action`) and check registration are **never** exposed through MCP, because a model must not approve or confirm its own side effects. HTTP must remain bound to loopback in v1. Do not expose an unauthenticated production service.

**R11 Replay:** side-effect-free diagnostic replay from recorded events and tool/model responses only. Resume is a separate controlled operation, never marketed as live replay.

**R12 Tests:** deterministic fake adapter, mock read/write services, crash boundary tests, evidence drift, lease races, duplicate model proposals, idempotency expiration, eventual-consistency traps and baseline comparison.

**R13 Coding-session continuity:** a coding session is an append-only journal per repository (`coding_sessions`/`coding_events`, docs/03), independent of any model's chat history. Continuum observes the repository itself to fingerprint the code: inside git, the tree of the working copy's *content* (tracked files on disk plus untracked, non-ignored files), so committing identical content is not a change; outside git, file hashes. A task is *verified* only when every check it names was registered by a human, passed on the current fingerprint, and still has the same definition; an agent's own claim of success is recorded as unverified. The resume brief separates verified facts from agent notes (claims), is bounded however long the session runs, and includes an observed code map of the files the session changed, so the next model need not re-read the repository. Continuum itself calls no LLM to build it. Checks run as fixed argv lists without a shell, with a timeout, in the repository root; output is redacted before persistence. Agents may start, resume, add tasks, record notes and checkpoints, run registered checks and complete tasks; they may never register or change checks.

**R14 Erasure and retention:** a human can erase a coding session completely (every event and the session row) with `continuum coding forget`, or every closed session older than N days. The erasure is one transaction with an immutable, content-free tombstone (who, when, why, how many events). History stays append-only otherwise: UPDATE and TRUNCATE are always refused, DELETE only inside an erasure. Erasure is never an MCP tool (D-039).

**R15 Per-person isolation:** several people may share one database. Each connects as their own PostgreSQL role and sees, changes and erases only their own coding sessions, events, checks and tombstones, enforced by row-level security. The schema owner administers access (`continuum admin grant|revoke|reassign|users`), retention and support (D-040).

**R16 What the agent did:** every tool call the user's app reports (Claude Code, Codex, Gemini CLI, Cursor; built-in tools, shell commands, MCP tools, skills, subagents) is journaled with its input and full redacted result, live from hooks and from transcripts, each call once. What each call changed in the files is observed by Continuum with content snapshots, not taken from the app or a model. A call with no recorded result, or an external call that ended interrupted, timed out or disconnected, has an unknown outcome; while an external one is unresolved, new external calls are denied until a person records what happened (AGENTS.md invariants 5 and 8). Hooks never wait for the database. The brief summarizes the work with an event id on every line, recall searches full outputs, and `coding_show` opens any record (D-042, D-043, D-045). Each app gets its own hook protocol, and `continuum coding setup` installs hooks for Claude Code, Codex, Gemini CLI and Cursor; changes outside git are observed too (D-045). A change of mind that names what it replaces marks the earlier requirement replaced, from the user's words only (D-044).

**R17 Lossless memory with a real grasp of the chat:** nothing the user typed or showed is cut away: a message of any length is kept whole, images and documents in chats and tool results are stored and shown to the next model on request (`coding_view_image`). Pasted technical content (stack traces, diffs, logs, JSON, tables, shell sessions, code) is recognised by structure and summarised by type (error signatures, files and line counts, keys, columns, defined names) instead of being dropped. The conversation's real shape is kept: messages on a branch the user abandoned (rewound, or edited and resent) are withdrawn from the requirements but stay in the journal and recall. Every stretch of a long chat is indexed by its most distinctive words, requirements the work actually touched rank first, and recall finds identifiers by their parts and fuses its two rankings. No model is used (D-046).

**R18 Unknown outcomes resolved by proof, never by assumption:** a `git push` whose result was lost is checked read-only against the remote (`git ls-remote`); when the remote branch shows exactly the pushed commit, the call is recorded as applied with that evidence and the block lifts. A probe never records that something did not happen; otherwise a person decides (D-047, AGENTS.md invariant 5).

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

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._
