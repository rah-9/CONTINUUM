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

## D-019 — The runtime is split into single-responsibility components

`runtime.py` had grown to 960 lines mixing lifecycle, model sessions, verification and budgets. It is now a controller (560 lines) that composes `RunJournal` (fenced appends), `BudgetGuard`, `MilestoneVerification` and `ModelSessions`, alongside the existing gateway and reconciler. Each invariant now has one owner:

- the gateway dispatches
- verification confers progress
- the journal fences writes
- sessions own model I/O

The refactor ran against the unchanged test suite before any new behaviour was added.

## D-020 — Capability-aware model handoff with provenance

Fallback order respects the operator's `model_policy`, adjusted for the failure:

- After `context_overflow`, models with a larger context window go first.
- Non-deployable profiles are skipped explicitly and traced.
- Models whose context cannot hold the packet are also skipped explicitly and traced.

The packet carries `handoff = {reason, handoff_count, from_model_id}`. This is runtime fact, not narrative, so the new model knows it is continuing someone else's run. Packet sizing uses the provider's tokenizer when available.

## D-021 — Estimated cost from the pricing snapshot

When a provider reports token counts but no cost, cost is computed from the profile's `price` snapshot and marked `estimated`. Unknown tokens or a missing price keep the cost unknown (`has_unpriced_usage`), never zero (docs/01 §5).

## D-022 — MCP plugin surface for coding agents; human decisions excluded

**Context.** Continuum should plug into any coding agent. MCP is the common protocol.

**Decision.** The local stdio server exposes eight tools:

- delegation: `list_tasks`, `run_task`, `resume_run` (optionally on another model) and `cancel_run`
- read-only inspection: `get_status`, `inspect_run`, `pending_decisions` and `list_runs`

**Approving, confirming, `retry_at_risk` and `abort` are deliberately not tools.** A model acting through MCP would otherwise approve its own side effects, which breaks the core invariant that only humans resolve uncertain or gated effects. `pending_decisions` returns the exact `continuum resolve` command for the user to run.

Ready-made client configs ship in `integrations/`: a Claude Code plugin with a skill and a marketplace entry, plus configs for Cursor, VS Code, Codex and Claude Desktop. The server is still pre-registered-tasks-only. Remote MCP discovery and native IDE extensions stay out of scope. The spec moves to 0.4.

## D-023 — Provider-safe tool names, and testing against provider rules

**Context.** While checking model switching, a defect surfaced. The LiteLLM adapter sent tool names like `store.put` unchanged, but OpenAI and Anthropic reject any function name outside `^[a-zA-Z0-9_-]{1,64}$`. Every live run would have failed with an HTTP 400. Fake models could not catch this because they do not enforce provider rules.

**Decision.**

- `ToolNames` maps names bijectively (`store.put` ↔ `store_put`), refuses collisions, and maps proposals back before the gateway sees them.
- Generated tool-call ids are paired with their results.
- `drop_params=True` lets LiteLLM translate parameters per model family.
- Switching tests run the real adapter against emulators that enforce OpenAI's and Anthropic's request rules.

## D-024 — Switching to new model versions without code changes

`ModelRouter` accepts an optional resolver. Any `provider/model-id` that is not configured, for example `anthropic/claude-opus-5-5`, resolves on demand. `config/models.yaml` entries may also use `profile: auto`.

Either way, the profile comes from LiteLLM's model registry. It is accepted only when the registry reports a context window, an output limit and native tool calling. A zero registry price is treated as unknown. Every profile is snapshotted in `ModelSessionStarted`, so each session's model and limits stay auditable.

Bare names without a provider prefix are refused, so a typo cannot silently route somewhere else.

## D-025 — Schema v2: invariants enforced by PostgreSQL

The database now rejects states that would violate core invariants, independently of application code:

- valid status enums (kept equal to the Python enums by a test)
- a verified milestone always has its fingerprint, evidence and timestamp (I3)
- a confirmed action always has an outcome
- an attempt is finished exactly when it is no longer `started`
- a terminal run always has `finished_at`
- digests and hashes are well formed
- usage counters are non-negative
- `TRUNCATE` on `events` is blocked

New indexes cover sessions and artifacts by run, artifacts by action, and unfinished attempts. A `schema_meta` row records the version. Constraints are added through an idempotent helper, so `db-init` upgrades existing databases in place.

The full suite, including chaos, ran with zero violations. That is evidence the runtime never writes an invalid state.

## D-026 — Verification is traceable and multi-layered

`tests/traceability.yaml` maps every requirement to its tests:

- R1–R12, U1–U7 and I1–I8
- the non-functional requirements NF1–NF9
- the safety gates G1–G6

A unit test enforces the mapping. Two layers were added:

- property-based stateful tests (Hypothesis) over the ledger, gateway, recovery and human decisions
- non-functional tests: no DB lock across I/O, end-to-end secret hygiene, reproducibility, parallel processes, performance baselines

CI runs every layer. See `docs/VERIFICATION.md`.

## D-027 — Lease time is fractional

`extract(epoch from clock_timestamp())::bigint` rounds to the nearest second, so a lease acquired at `x.4 s` read as expired at `x.5 s`. Every lease was up to a second shorter than its TTL, and a 1 s TTL could last about 0.1 s.

Lease expiry and DB time are now `DOUBLE PRECISION` seconds with no rounding. Existing databases are altered idempotently by `db-init`.

The defect surfaced as an intermittent chaos-test failure under load. It was reproduced deterministically by acquiring at the worst-case sub-second offset (`test_lease_ttl_is_not_shortened_by_rounding`), and only then fixed.

## D-028 — Coding-session continuity is a separate journal, judged by observation

**Context.** Users switch models in the middle of coding: Opus 4.8 → 5.5 in Claude Code, GPT-5.6 → 6.1 in Codex, or into a new chat. Runs (task specs, milestones, the ledger) model work that Continuum drives. A coding agent drives itself and edits files directly, so a run doesn't fit.

**Decision.**
- Add `coding_sessions`/`coding_events`/`coding_checks` (schema v3), separate from runs. The journal is append-only and immutable, with a gapless per-session `seq` under the session row lock.
- Continuum **observes** the repository itself (git HEAD + `diff HEAD --binary` + untracked non-ignored files, or file hashes outside git). It never trusts an agent's description of the code.
- A task is verified only by checks that are *valid now*: passed, on the current fingerprint, with an unchanged definition digest. Otherwise completion is refused with the reason. A task without checks can be marked done but stays unverified.
- The resume brief separates `verified_facts` from `agent_notes` (claims). This mirrors invariant 8: unverified model claims are not carried over as facts.
- Interpreter caches (`__pycache__`, `.pyc`) are excluded from the fingerprint. Running tests must not invalidate the evidence it produced (regression test).

**Alternatives rejected.** Storing chat transcripts (not authoritative, vendor-specific and large). Trusting agent-reported test results (a model cannot verify its own progress). Copying code into the database (git already holds it).

## D-029 — Checks are human-registered commands; agents can only run them

**Context.** Verification needs to run something (tests, a linter, a build). AGENTS.md excludes an arbitrary code runner, and a model must not define its own bar for "done".

**Decision.**
- Checks are registered only by a person through the CLI (`continuum coding checks add REPO NAME --actor X -- argv…`), stored with who and when. There is **no** MCP tool to register or change one.
- Agents can only run a check by name. It runs as an argv list (no shell), in the repository root, under a timeout (1–3600 s). Output is redacted, kept as a tail, and digested.
- No model supplies a command, so this is not an arbitrary code runner. It executes the commands a human would otherwise run by hand.
- The verification evidence records the definition digest. Changing a definition invalidates every task it verified, and the brief flags checks (re)registered during the session.

**Limitation (accepted).** An agent with shell access can impersonate the human CLI, and Continuum cannot authenticate a local user. The design is tamper-**evident** rather than tamper-proof: the who, the when, the flag and the invalidation. Teams that need more should keep checks under code review (for example, mirror them in CI) and review the flags.

## D-030 — Automatic schema migration on start; CLI argv after `--`

- `ensure_schema()` compares `schema_meta.version` with the code's version and migrates only when they differ (or on a fresh database). The CLI (except `db-init`/`doctor`) and the MCP server call it, so upgrading Continuum never leaves an agent facing a missing table.
- `continuum coding checks add … -- cmd args` splits raw argv at `--` before argparse. argparse `REMAINDER` swallowed options such as `--actor` when they appeared after the positionals.

## D-031 — Coding fingerprints fail closed; claims never render as facts

**Context.** A full audit of v0.6.0 found paths where evidence could look valid without being valid: a failed `git` call hashed as empty, diff drivers rewriting the hashed bytes, staged changes ignored before the first commit, and agent text rendered among the facts.

**Decision.**
- Observation **fails closed**. Any required git call that fails raises `observation_failed`, and nothing is hashed from partial data.
- Diffs are raw (`--no-ext-diff --no-textconv --no-color`), so repository config cannot change what is fingerprinted.
- Untracked symlinks are hashed as links.
- Only caches are excluded at any depth; build directories are excluded only at the root.
- The brief keeps every agent-written string (goal, titles, notes, checkpoint summaries, agent labels) out of `verified_facts`. In Markdown these strings are flattened to single lines.
- `human:` actor labels are produced only by the human CLI.
- `complete_task` uses optimistic concurrency on the session's `last_seq` and re-observes the code before committing.
- Repository paths are contained (`CONTINUUM_CODING_ROOTS`).
- Schemas never downgrade, and migrations are serialized with an advisory lock.

**Limitation.** The filesystem is not transactional. An edit between the final observation and the commit is possible, but that window is now microseconds rather than the length of a request. The next brief flags the result as `needs_reverification`.

## D-032 — An observation error is not drift; one operation per run per process

**Context.** The core-runtime audit (v0.6.2) found two high-severity issues.

- `recheck_verified` treated a verifier `ERROR` as `FAIL`. A single transient read timeout on resume could therefore invalidate verified work and terminally fail a run as `drift_requires_remediation`.
- The lease is keyed by controller id, which is per process. Concurrent HTTP requests in one `continuum serve` re-entered the same live lease and epoch, so fencing could not separate them. One request's `release` also expired the lease under another.

**Decision.**
- Drift requires evidence. Only `FAIL` or a fingerprint change invalidates. An `ERROR` records `VerifierError` and the run waits in `awaiting_reconciliation`; the next resume re-observes. This keeps invariant 7 (evidence-based progress) and stays conservative: nothing proceeds past the unobservable milestone.
- `Runtime._owned` claims `(dsn, schema, owner_id, run_id)` in a process-wide set for the duration of each lease-holding operation. A concurrent operation gets `already_owned`. The documented same-owner lease renewal semantics of `LeaseManager` are unchanged.
- Loopback HTTP also validates `Host` and `Origin`, to defend against DNS rebinding.

**Residual (documented, not changed).** `_settle_effects` dispatches authorized retries without re-checking for *another* unresolved effect between dispatches. Built-in flows cannot have two authorized writes pending at once (one pending approval at a time, and the gateway blocks new writes while an effect is unresolved), so no failing test can be built today. AGENTS.md forbids changing ledger behavior without one. Revisit this when multi-approval flows exist.

## D-033 — Portable by construction; checks are isolated from Continuum

**Context.** We installed the built wheel into a clean environment and drove `continuum mcp` from an unrelated directory, the way an IDE does, then ran a second full audit. Together they found failures that only appear outside the developer's checkout:
- a Windows pipe encoding
- IDE launch directories
- PEP 668 cloud images
- `.cmd` shims
- POSIX-only locks

The audit also found correctness gaps: NUL bytes, submodules, non-UTF-8 names, cost precision, terminal-run rewrites, process trees, colliding owner ids and active-time accounting.

**Decision.**
- **No working-directory assumptions.** State lives in `~/.continuum`. Config comes from `./config` only when it holds tasks. All text I/O is UTF-8. CLI output is UTF-8.
- **One install path everywhere.** Use `uv tool install` / `pipx` on any OS. The cloud setup script uses `uv tool --python 3.12` and links `continuum` onto PATH.
- **Checks are isolated.** Each check:
  - runs in its own process group, killed as a tree on timeout
  - writes output to a bounded file
  - gets an environment without `CONTINUUM_*`, so code under test cannot reach the journal database (and thereby register checks)
  - has its command resolved like a shell would, but never runs through one
- **Storage accepts anything a model or a tool can emit.** NUL characters are rejected in tool arguments and replaced in agent text and check output. Costs are quantized to the column's precision, so replay equals the durable projection.
- **Identity and terminal states.**
  - Default owner ids carry a per-process nonce.
  - An ended run's outcome is never rewritten.
- **Fingerprints stay fail-closed and complete.** Submodules are fingerprinted recursively, and file names are hashed as bytes.

## D-034 — The handoff brief is a bounded, compact context artifact

**Context.** We measured a 30-note, 15-checkpoint session. The handoff returned about 8.3k tokens, with the same content twice: once as JSON and once as Markdown. The brief grew without bound as the session went on. Each coding operation spent most of its time in 7 git subprocesses.

**Decision.**
- **MCP briefs are compact by default:** Markdown plus the task ids needed to call other tools. `detail="full"` returns the JSON. The CLI's `--json` and the SDK are unchanged (full).
- **The brief keeps the newest items within fixed caps.** Omissions are counted and shown ("N older notes omitted"); the journal remains complete. Bounding is by count and length rather than by a token estimate, so it is deterministic.
- **Commits since the last checkpoint are reported as observed facts.** If the checkpoint commit is no longer an ancestor of HEAD, the field is `None`. This information never affects verification.
- **Fingerprinting takes untracked files from `git status`** (same `.gitignore` semantics as `ls-files --others`). The index is scanned for submodules only when `.gitmodules` exists.

**Considered and rejected.** Batching the `runs.last_event_seq` update once per transaction would save about 14% of statements in a run. Those updates come from the pure projection shared with replay and rebuild (invariant I2), so the saving does not justify touching that path.

## D-035 — Fingerprint the content, not the history; judge Continuum by real handoffs

**Context.** Real Sonnet → Opus handoff trials (`docs/HANDOFF-TRIALS.md`) found three defects that no unit test had caught:
- After every commit, verified work was flagged as changed, because the fingerprint included the commit id.
- The default DSN password `continuum` made redaction rewrite every path and command.
- Bounded notes lost their reasons.

**Decision.**
- **Fingerprint = git tree of the working copy's content.** It is built in a private index copy from tracked files on disk plus the untracked, non-ignored files from `git status`. Committing, staging or amending identical content keeps the fingerprint, and any content change alters it. This costs about 165 ms on a 20k-file repo, instead of about 70 ms. Accuracy over speed, because a false "re-verify" makes the next model redo or re-check finished work, which is the cost Continuum exists to remove.
- **URL passwords are redacted inside URLs.** They are redacted elsewhere only if they look like secrets.
- **The brief keeps fewer, whole notes** rather than many clipped ones.
- **The brief carries a code map** (`verified_facts.session_changes`): files changed since the session's base commit, with +/- lines, git's enclosing-function context and names defined in added lines. It is observed from git, never from model claims, and is capped (40 files, 8 names each). The rules tell the next model to start from it rather than explore the repository. In Trial 3 it cut work tokens by about 20% with the same correct result. It is informational only and never affects verification.
- **The success measure is transfer quality in real model switches.** That means hidden tests, chat-only requirements, redone work, and the tokens spent. Micro-benchmarks are not the measure. Trials are part of evaluating any change to the brief or the fingerprint.

## D-036 — Capture the user's words through app hooks; recognise a repository by its remote

**Context.** A switch of *app* (Claude Code → Codex), an abrupt end (quota, crash, closed window) or a different checkout (laptop → cloud container) broke continuity in three ways:
- What the user said in chat reached the next model only if the previous agent had written it down. Agents often don't, and a cut-off agent can't.
- Sessions were keyed by the exact path. A subdirectory, a re-clone or a container path started an empty session.
- Rules lived in whichever instruction file one app reads.

**Decision.**
- **The user's words are captured by the app, not by a model.** `continuum hook <app>` reads the app's own hook payload (Claude Code hooks, Codex `notify`, any app with a JSON payload) and stores `ChatCaptured` events: every user message, and the newest agent message per capture as the turn's report.
  - Transcripts are parsed by structure (role/content, parts, message events), so new app versions degrade instead of breaking.
  - Harness-injected text is removed. Text is redacted, including well-known token formats, and bounded (16k chars).
  - Messages are deduplicated by a key over source, chat, role and text under the session lock, so hooks can re-import a whole transcript on every turn.
- **Capture never needs the agent's cooperation and never hurts the app.** The first user message starts a session if none is active. Hooks always exit 0, wait at most 3 s for the database and never read a stdin that does not close.
- **The brief puts the user's words first, labelled as requirements, newest wins.** It keeps the first request plus the newest 6, whole up to 2,000 characters. Short replies ("continue") are hidden except the newest. The last agent report is shown as a claim. Computed *Attention* lines name the gaps: no capture, a request newer than the last next step, uncheckpointed code, re-verification, a failing check, a moved session, a changed branch.
- **Repository identity:** the git top level, then the normalized credential-free remote URL **with the same branch**. A session found that way moves to the new checkout (`AgentJoined.repo_root`). Checks registered for the same remote apply. Another branch is reported, never joined.
- **Recall instead of a bigger brief:** `coding_recall` is deterministic keyword search over the whole journal. It is read-only and therefore allowed over MCP.
- **One command sets up a repository** (`continuum coding setup`). It writes the rules into AGENTS.md (CLAUDE.md imports it), Claude Code hooks, and `.mcp.json`. It never edits user-level config.

**Considered and rejected.**
- An LLM summarising chats: extra cost, and it would turn the user's words into a model's claim.
- Embeddings or a vector index for recall: a dependency and a stateful index for little gain over keyword search on one session.
- Capturing every agent message: noise. The final report per turn, plus the code and checks, carries the state.
- Joining by remote alone: two branches of one repository are different work.
- Exposing capture as an MCP tool: a model could then write "the user's words".

## D-037 — Consolidate the whole conversation; recognise what is *not* a requirement

**Context.** The v0.8.0 brief showed the first user message and the newest six. A benchmark of planted facts (`continuum_lab.memory_bench`) showed what that loses. In 40-message chats only 15% of explicit requirements reached the next model, and so did 23% of changes of mind, no answers to the agent's questions and no agent decisions. Pasted logs leaked in. In a real 30-message trial, the requirements for the remaining work had been stated in messages 8–27.

**Decision.**
- **Read every captured message** and keep, within a budget, what is still binding:
  - changes of mind and answered questions first
  - then sentences with strong and weak requirement cues
  - then every other statement
- **Exclusion over inclusion.** Questions, small talk, short replies and pasted output (code blocks, traces, logs, diffs; a pasted block runs until a blank line) are excluded. Recognizing what is *not* a requirement generalizes better than listing every way to phrase one. On a phrasing set written after the last change, recall rose from 47–74% with cue lists alone to 100%.
- **Show the ledger in conversation order with message numbers,** so "the higher number wins" is applicable. Keep the newest occurrence of a repeated sentence.
- **The budget grows with the chat** (4,400 chars + 50 per message beyond 40, capped at 10,000). This is the measured capacity limit: at 160 verbose messages a fixed budget dropped recall to 23–78%.
- **Pair short replies with the question they answer.** Keep choices and caveats from the agent's reports as claims.
- **Label the most recent message as such.** Flag a chat-ending message, because it was addressed to the previous agent. Add a rule not to stall on the previous agent's open questions. Both were found by Haiku misreading the brief.
- **Capture keys count occurrences,** so repeated words are kept, while transcript re-imports stay idempotent.
- **Pin quality with a benchmark test.** Brief changes must keep ≥95% recall and zero noise.

**Considered and rejected.**
- An LLM summariser: cost, and it would turn the user's words into a model's paraphrase (a claim).
- Embedding search for selection: a dependency and an index for a problem that ranking within a budget solves deterministically.
- A larger fixed window of recent messages: it buys recall only for the recent part of the chat, and costs tokens on every handoff.

## D-038 — Keep both sides of the chat, by the transcript's own identities

**Context.** Replaying Trial 5's real transcript showed the journal held 27 of Claude's 47 messages:
- Only each turn's final agent message was kept.
- A catch-up after an outage kept only the last agent message overall.
- Claude Code writes uuid-less request/cache copies of messages, which the parser read again.

**Decision.**
- **Keep every agent text message from transcripts** (tool calls and tool output excluded).
- **Key transcript messages by the transcript's id.** Fall back to the occurrence count for formats without ids.
- **Skip records without an id** when the transcript has ids.
- **Store a transcript copy of a live-captured message as a hidden alias.** Re-imports stay idempotent, and the live message takes the copy's timestamp.
- **Order the chat by transcript timestamps.**

**Rejected.**
- Matching live and transcript messages by text alone: it loses repeated words, which was the v0.8 bug.
- Re-ordering stored events: the journal is immutable.

## D-039 — Erasure is a human act that leaves a content-free record

**Context.** A client must be able to delete what a session captured (privacy requests, retention policies), but the journal is append-only by trigger (invariant 3), and no model may remove memory.

**Decision.**
- **Schema v5.** The `coding_events` trigger allows DELETE only when the transaction set `continuum.erasure = on`. UPDATE and TRUNCATE are never allowed.
- **`continuum coding forget`** (CLI only, `--yes` required) locks the session, deletes its events and row, and appends a tombstone to the immutable `coding_erasures` table, all in one transaction.
- **The tombstone holds no content:** session id, repository path hash, event count, `human:<actor>`, redacted reason, time.
- **Retention** (`--older-than-days`) erases closed sessions untouched for N days; active ones only on request.

**Rejected.**
- Redacting events in place: it changes history, and a partial redaction is hard to prove complete.
- Disabling the trigger during erasure: it is schema-wide, so a concurrent writer could change history meanwhile.
- An MCP tool: models must not erase memory.

## D-040 — Several people share a database, isolated by PostgreSQL roles and row-level security

**Context.** A client deploys one database for a team. Each person's chat is personal data, and an application-level filter is one bug away from showing it to a colleague.

**Decision.**
- **Identity is the PostgreSQL role.** Each person logs in as their own role; Continuum stores no users or passwords.
- **Ownership column.** `coding_sessions`, `coding_checks` and `coding_erasures` carry `owner_role` (default `current_user`). Events are visible exactly when their session is (policy on the session row), so the immutable event rows needed no change.
- **RLS policies** `owner_role = current_user` for reads and writes. A role cannot insert a row for another role, move its own row to another role, or see that another's row exists.
- **The schema owner is the operator.** RLS does not apply to it, so retention and support work across people. Repository lookups add the same filter explicitly, so the operator's own coding stays separate.
- **Checks are per person** (primary key includes `owner_role`).
- **`continuum admin grant|revoke|reassign|users`**, CLI only, refused (`not_operator`) unless the caller owns the schema.

**Rejected.**
- A `user_id` setting in the client: any client could claim to be anyone.
- A schema per person: migrations, retention and support multiply, and the operator loses one place to administer.
- FORCE RLS on the owner: retention and erasure for everyone would need a second privileged path.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._
