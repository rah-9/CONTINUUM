# Architecture and execution flows

## 1. Minimal component graph

```text
Python SDK / CLI / localhost HTTP / local MCP facade
                |
       Run controller + lease
                |
      Step loop + packet builder ---- ModelAdapter ---- provider(s)
                |
          Permission + Tool Gateway ---- trusted registered tools
                |                    
      Durable action ledger <---- Reconciler ---- read-only service checks
                |
         Milestone verifier ---- artifact/evidence store
                |
     PostgreSQL event store + projections (ONE transaction per transition)
                |
          JSON trace export -> Continuum Lab (in-repo tests)
```

**Ownership:** model adapters propose actions but never execute tools; only gateway dispatches, **including read-only verifier/reconciliation probes** (labeled by trusted caller in the event log). Controller governs run state and budgets; verifier alone confers milestone progress. Runtime, ledger and evidence remain model-independent. PostgreSQL is the source of truth; workspace files are content-verified artifacts.

## 2. Run lifecycle

`created -> running -> completed | failed | cancelled`.

`running <-> recovering` for process/model interruption; `running <-> awaiting_reconciliation` for uncertain effects; `running <-> paused_for_approval` only for configured approval tools. These pause/recovery states are **non-terminal**. Hard budget exhaustion moves to `failed` (the exhausted policy cannot be reset by a model handoff).

No auto-resume from `awaiting_reconciliation` on a timer if uncertainty remains. Cancellation prevents *new* dispatches; it cannot roll back calls already sent.

## 3. Normal run flow

1. Validate spec (acyclic milestone DAG, allowlisted tools, existing verifier IDs, operation-key rules, budgets and referenced fixture versions).
2. Persist `RunCreated` with full canonical task spec, initialize projection and workspace, then acquire controller lease with new fencing epoch.
3. Start provider session, build initial packet, call model with model-call budget reservation.
4. Normalize each returned tool proposal. Reject malformed/unauthorized args at gateway. **Do not trust the model-supplied call ID as a business operation identity.**
5. Gateway calculates a **business operation key** from trusted task/manifest fields, finds or creates one logical action, verifies proposed immutable arguments and permissions, and dispatches using 04's state machine.
6. Persist tool outcome; pass bounded result reference to model. Invoke milestone verifier when prerequisites and outputs allow it, not simply because model says “done”.
7. On verifier pass, persist fingerprints/evidence and `MilestoneVerified`; on failure, give an actionable verifier error to model if retries/budget remain.
8. Complete only if all required milestones verify, no unresolved actions remain, and final independent checks pass.

## 4. Gateway / external effect flow

1. Validate JSON Schema, route + allowlist, effect policy, canonical args and operation-key fields.
2. In a short PostgreSQL transaction protected by active controller fencing epoch, find existing logical action by `(run_id, operation_key)` or create it. If existing immutable args differ: **reject as intent conflict**, do not silently create a second action.
3. Persist authorization (after approval if required). Persist `ActionAttemptStarted` and attempt row **before** remote dispatch. Commit transaction.
4. Immediately check lease ownership and cancellation before starting actual invocation. If ownership changed after a persisted start, treat attempt as uncertain until reconciliation. Never hold a DB transaction open over I/O.
5. On acknowledged success, persist confirmation. On definitively side-effect-free rejection, persist `failed_clean`. On ambiguous failure/timeout, persist `unknown`; **do not return a normal retriable error** to the model.
6. If a matching action is already confirmed, return its recorded result/reference instead of dispatching. If unknown, block it and enter reconciliation. Different intended sends to one recipient require separate **trusted** operation identities defined by the task, not a model-created index.

**Race caveat:** checking a lease immediately before dispatch does not prevent an old in-flight request from committing after lease expiry. An idempotency/reconciliation contract is still necessary for write safety.

## 5. Failure and handoff flow

1. On 429/5xx, context overflow, bad model output, crash or explicit handoff request, stop issuing **new** effects; persist failure/recovery transition if process is alive.
2. On restart, atomically acquire a new lease/epoch; reject stale controller updates. Rebuild or validate projections against event log. Any attempt with `started` but no terminal result becomes **unknown**, even if it possibly never left the process.
3. Check artifact bytes, dependency fingerprints, external source versions and verifier versions. Invalidate changed milestones and all dependent milestones; do not reverse previously confirmed external actions.
4. Reconcile unknown actions using authoritative proof or documented dedupe guarantee. A "not found" query from eventually consistent storage remains **unknown**. Without safe proof, set `awaiting_reconciliation` and stop (no handoff write continuation).
5. Once safe, select new provider, check capability profile/remaining aggregate budget, construct deterministic packet from valid evidence and pending work, then persist `ModelSessionStarted` / `ModelSwitched`.
6. Resume the first **eligible unverified** milestone; reuse confirmed action results even if a model proposes that operation again.

## 6. Reconciliation and human decision

- **Read-only:** re-execute after interruption if the data's freshness conditions still hold.
- **Guaranteed idempotent write:** retry **the same action/key/args** only within provider's documented dedupe scope/retention. The resulting acknowledgement or query evidence confirms completion.
- **Queryable write:** authoritative positive observation confirms. Negative observation alone usually does **not** prove absence; auto-retry only if connector explicitly supplies a race-safe absence guarantee. Otherwise pause.
- **Opaque / irreversible:** no automatic retry after ambiguous dispatch. Human may record corroborating proof, skip/abort, or explicitly authorize an at-risk retry (which is separately tagged in trace and never described as dedupe-safe).

Human-confirmed statements are not equivalent to deterministic milestone evidence. A human skip cannot make a required milestone verified.

## 7. Verification and artifact drift

Verifier receives a read-only view of the task, artifacts and ledger. It returns `{status, evidence_refs, input_fingerprint, verifier_version}`. Store evidence artifacts by content hash. Re-check on resume and before final completion. Any changed input, artifact, dependency or verifier version invalidates affected milestone and its transitive dependents. Only a verifier **FAIL** or a changed fingerprint is drift. A verifier **ERROR**, meaning the observation itself failed (timeout, unreachable service), is not evidence: Continuum records `VerifierError`, invalidates nothing, and stops in `awaiting_reconciliation` until the next resume can observe again.

If an invalidated milestone (or a descendant) already has **confirmed** write effects, its redo would need those operations again, and confirmed operation keys are never re-sent. The run therefore stops explicitly with `failure_code = drift_requires_remediation` rather than looping; a new task spec version or a human remediation workflow is required (docs/03 §5).

For non-file effects, verification observes mock service ground truth or a documented authoritative source. Model narrative or a successful HTTP response alone is insufficient if a stronger task verifier is specified.

## 8. Replay, interfaces and threading

- `inspect/replay`: replay recorded events, model decisions and tool results into an isolated state projection; **never call providers or tools**. This is a diagnostic feature, distinct from run resume. `inspect` returns a timeline (model decisions, action outcomes, reconciliation, milestones) and a projection diff; `trace` exports every event as JSON lines with its redacted blobs inlined, for Continuum Lab.
- Structured local tracing: every committed event, model call and tool dispatch is logged as one JSON line (IDs, types, statuses and durations only; never arguments or payloads).
- `resume`: real continuation under lease and ledger safeguards. Do **not** implement a `live replay` mode in v1.
- SDK/CLI execute locally. Minimal HTTP binds to `127.0.0.1` by default. It also refuses any request whose `Host` or `Origin` is not loopback, which defends against DNS rebinding from a web page. Production authentication and deployment are explicitly excluded.
- Within one process a run is claimed for the duration of an operation, in addition to the lease. Two concurrent requests in one `continuum serve` share a controller id, so without the claim they would share one lease epoch.
- A v1 local **stdio MCP server** lets any MCP-capable agent delegate and follow pre-registered tasks (`list_tasks`, `run_task`, `resume_run`, `cancel_run`, `get_status`, `inspect_run`, `pending_decisions`, `list_runs`); it does not install a harness inside the host. Human decisions are not MCP tools: `pending_decisions` returns the exact command a *person* runs. The `coding_*` tools (§10) carry coding work across models; there is no tool to register or change a check. `resume_run(model_alias=...)` continues the same run on another model from verified state. Arbitrary remote MCP tool discovery remains post-MVP. Tool annotations from external MCP servers are untrusted until an operator creates a trusted manifest.

## 9. Required crash boundaries

Inject kill before and after: event/projection commit; lease acquisition; action creation; attempt-start commit; remote service commit; response receipt; action confirmation commit; artifact write/rename; milestone verification commit; model switch commit. Each restart must preserve invariants and either continue safely or pause explicitly.

## 10. Coding-session continuity

Coding agents (Claude Code, Codex, Cursor, …) do their own editing; Continuum does not drive them. It keeps the **state of the coding work** outside any chat so the next model can continue from it.

1. A human registers checks once per repository: `continuum coding checks add REPO tests --actor alice -- pytest -q`. Definitions are stored with who registered them and when.
2. The agent calls `coding_resume(repo_path, agent=<model id>)` first in every chat. If there is no active session it calls `coding_start(goal, tasks)`. Each start or resume records `SessionStarted`/`AgentJoined` with an observed `RepoState`.
3. While working it records `NoteRecorded` (decision, context, next_step, blocker) and `CheckpointCaptured` (summary plus observed state).
4. `coding_run_check(name)` runs the registered argv in the repository root without a shell, under a timeout. The command is resolved like a shell would (`npm` → `npm.cmd` on Windows), runs in its own process group (the whole tree is killed on timeout), writes output to a file (bounded tail), and gets an environment without `CONTINUUM_*` variables, so code under test cannot reach the journal's database. Continuum then records `CheckRan` with the code fingerprint observed **before** the run, the definition digest, the exit code and a redacted output tail. If the check itself modified the tree, `modified_tree` is set and the brief flags it. A closed session never runs a check. A command that cannot start (not found, not executable) is recorded as a failed run, not a crash.
5. `coding_complete_task(task_id)` succeeds only if each named check's latest run is *valid now*: passed, on the current fingerprint and with an unchanged definition. Otherwise it is refused with the reason. The append is also refused if the journal moved on after the evidence was read (`last_seq` changed), or if the code changed between evaluation and commit. A task without checks may be marked done, but stays `verified: false`.
6. On resume the brief is rebuilt from the journal plus a **fresh observation**. It is bounded (newest notes and checkpoints within fixed caps, with omissions counted). It includes the commits made since the last checkpoint as observed facts (`None` if history was rewritten) and a **code map** (`session_changes`): the files changed since the session's base commit, with +/- lines, git's enclosing-function context and the names defined in added lines. The rules tell the next model to start there. Both are informational and never affect verification. MCP returns it compact (Markdown plus ids) unless `detail="full"`. Verified tasks whose code or check definitions changed are flagged `needs_reverification`. Checks (re)registered during the session are flagged so a person can review them.

7. **Capture (any app, no model).** The app's hooks call `continuum hook <app>` with their own payload: Claude Code on every prompt, turn end, compaction and session end; Codex through `notify` after every turn. The user's words and the turn's final agent report are stored as `ChatCaptured` events (redacted, bounded, deduplicated by a key over source, chat, role and text, in one transaction under the session lock). Transcripts are parsed by structure, not by one app's schema. With no active session, the first user message starts one. At session start, Claude Code's hook prints the brief into the new chat's context. A hook always exits 0 and never blocks the app.
8. **Locating the session.** By the git top level of the path, then by the normalized remote URL **and** the same branch in another checkout (the session moves there, recorded in `AgentJoined.repo_root`). Checks registered for another checkout of the same remote apply. Another branch is never joined by guessing.
9. **Recall.** `coding_recall(query)` searches every text in the journal (goal, tasks, notes, checkpoints, captured chat, check output) by keyword and returns bounded snippets. It is read-only.

The full algorithm, its bounds and its scenario matrix are in `MEMORY-ALGORITHM.md`.

Every event is one transaction that locks the session row, appends with the next gapless `seq` and updates `last_seq`. `coding_start` writes the session row, `SessionStarted` and the initial `TaskAdded` events in **one** transaction, after validating every task.

The fingerprint is the git tree of the working copy's **content**, built in a private copy of the index (tracked files as they are on disk, plus untracked, non-ignored files from `git status`), in five git calls. Committing, staging or amending identical content keeps it; any edit changes it (D-035). It never fails open. Initialized submodules are fingerprinted recursively (`git diff` alone shows only `-dirty`). File names are hashed as raw bytes, so names that are not UTF-8 are neither skipped nor fatal. NUL characters in agent text or check output are stored as U+FFFD. If a git command fails, observation raises `observation_failed` instead of hashing less content. Diffs are raw: no external diff drivers or textconv. Staged changes count even before the first commit. Untracked symlinks are hashed as links, never followed. Only caches are excluded at any depth; `build/`, `dist/` and `target/` are excluded only at the project root.

Repository paths are contained: never `/` or the home directory itself, and inside `CONTINUUM_CODING_ROOTS` when that is set. Agent labels are redacted and may not start with `human:`, which is reserved for the human CLI. In the Markdown brief, agent-written text is flattened to single lines so it cannot imitate the brief's own sections. Events are immutable, and closed sessions reject writes. Model notes, including checkpoint summaries and captured agent reports, are presented as claims under `agent_notes`, never inside `verified_facts`. Captured user messages are presented as the user's words under `user_requests` (newest wins), never as facts.

**Limit:** an agent with shell access could run the human CLI to register a check. Continuum cannot authenticate the local user, so it makes this **tamper-evident**, not impossible. See D-029.

