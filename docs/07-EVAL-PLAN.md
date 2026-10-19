# Continuum Lab — integrated evaluation plan

Continuum Lab is **part of the Continuum repository**, not a second project. Its purpose is to falsify or support one claim: verified state + effect ledger improves post-failure progress retention and reduces unsafe repeats across models.

## 1. Hypotheses

- **H1:** after interruption, a larger share of *still-valid* previously verified milestones is preserved than in a plain retry/transcript-handoff baseline.
- **H2:** unintended committed duplicate effects are lower, particularly for queryable but non-idempotent services; opaque ambiguity becomes explicit unresolved/human state rather than silently repeating.
- **H3:** a model handoff completes more tasks correctly without increasing policy violations or silently losing work.
- **H4:** cost and time overhead remain measurable and proportionate to improved correctness. A win on safety with greater cost is reported as a tradeoff, not erased.

Null and adverse findings must be published.

## 2. Compared configurations

**B0: plain tool loop + retries.** Same task objective, allowed tools, provider IDs, endpoint behavior, permissions, budget caps and independent final scorer. API retry/backoff allowed. On model switch pass previous visible transcript (with a documented length cap). No persistent action ledger or verification-gated intermediate progress. It may use `check` tools when available; give it a clear warning to verify before repeating writes. Do **not** intentionally cripple it.

**C1: Continuum.** Durable milestones/evidence, ledgered actions, conservative reconciliation and structured packet handoff.

**B1: checkpoint-only baseline (stretch).** If feasible after C1 is stable, add a checkpoint-based runner with state restore but no effect-reconciliation ledger; do not claim superiority over existing checkpoint frameworks without this or an equivalent comparison.

**Fairness warning:** B0 and C1 necessarily use different context formats and recovery algorithms; claiming identical system prompts would be misleading. Hold task instructions, task-visible tool contracts, provider settings and scoring consistent; disclose scaffolding differences.

## 3. Ground-truth mock services

- **Mock database:** update/read records; independently store every committed mutation and logical business key.
- **Mock object store:** put/head/read files; store exact content SHA-256, operation receipts and commitment timestamps (do not equate ETag to SHA-256).
- **Mock messages:** send/query messages with immutable `message_id`/recipient/purpose; separate send events from user-visible delivery state.
- **File utilities:** read/normalize/write files within a fixed workspace; stable input snapshots and atomic writes.

Service modes (configurable per endpoint):

1. `deduped`: stable idempotency keys suppress repeat **commits** during a configured retention window.
2. `observable`: no dedupe, authoritative positive query; negative read may intentionally lag committed state.
3. `opaque`: no key-based dedupe and no reliable state query.

Service ground truth must be logged **outside the agent-visible tool results** and scored independently.

## 4. Controlled faults

- `rate_limit` and `provider_unavailable` during a model call.
- `process_kill` after event+projection commit, after attempt-start, after remote commit, before response, and before outcome commit.
- `commit_then_drop_response`: **primary duplicate-risk case**.
- `drop_before_commit` where connector can prove nonexecution; contrasted with delayed commit where an immediate negative read is misleading.
- `idempotency_ttl_expired`: time advance beyond connector retention.
- `old_owner_returns`: stale worker reply after lease takeover.
- `artifact_drift`: input or output hash changes after verified milestone.
- `duplicate_proposal`: replacement model requests an already confirmed business operation.

Fault triggers are deterministic by named boundary and seeded execution schedule. Where milestone checkpoints are sparse, 25/50/75% means **nearest eligible verified progress boundary**; record the *actual* achieved fraction, do not pretend it is exactly 25/50/75. Test write-ack loss as a separate mandatory event boundary even if progress fractions do not match.

## 5. Task suite (15 initial cases; every outcome independently checkable)

| Family | Count | Example tasks and checkers |
|---|---:|---|
| CSV / artifact | 5 | validate columns, normalize categories, detect duplicates, materialize result, verify uploaded file hash |
| Database updates | 5 | update selected IDs, partial batch, avoid forbidden IDs, dedup target updates, verify exact final rows and commit count |
| Notifications / publishing | 5 | templated recipient sends, exclusion lists, one upload+notify flow, approval-required notification, verify exact sends and content |

Use 3–5 fixed milestones per task, at least one read and one write for side-effect tasks; apply unknown-action modes to relevant writes. At least two cases require a human decision. A prompt-injection/permission smoke test is useful but **not** a required benchmark family in v1. No shell/code-generation benchmarks: unrelated to Continuum's continuity claim and would require new sandbox infrastructure.

## 6. Experimental sequence and budget discipline

1. **Protocol smoke:** 3 tasks × 1 model × B0/C1, no faults; inspect traces and identify prompt/tool inequivalence.
2. **Safety matrix (fake adapter):** all 15 tasks × relevant commit/timeout/lease/drift faults. Run many deterministic seeds cheaply, including adversarial event boundaries. This is the primary correctness gate.
3. **Live clean check:** 15 tasks × 3 provider configurations × B0/C1 × 2 repeats = **180 runs** if budget permits. Record actual coverage.
4. **Live interrupted subset:** 6 representative tasks × 3 progress points × B0/C1 × 2 repeats = **72 runs per specified handoff pairing or selected provider setting**. Run at least one cross-provider pair, then expand to A→B, B→C, C→A as funds allow. Pair matched tasks/fault conditions.
5. **Service modes:** dedicated paired tests for deduped, observable-lagging and opaque providers, not necessarily full combinatorial expansion.

Stop or reduce live coverage when the preset evaluation budget is hit; report missing cells. Never multiply all factors blindly. Two repeats are an exploratory minimum, **not** proof of significance.

## 7. Metric definitions

Compute against mock-service **ground truth**, not agent prose. Report counts, denominators and per-model/task/fault breakdowns.

- **Verified completion:** runs whose required milestone verifiers pass with valid fingerprints and independent final ground truth / all attempted runs. An unresolved run is **not** a success.
- **Handoff success:** attempted eligible cross-provider handoffs resulting in verified completion / handoffs attempted. Report separately handoffs blocked for safety, incompatibility or budget.
- **Preserved progress:** still-valid milestones verified before injected failure and **not re-executed** during continuation / still-valid pre-fault verified milestones. Exclude correctly invalidated milestones and report exclusion count. `0/0` is not applicable, never 100%.
- **Committed duplicate effects:** sum of externally committed actions beyond task-defined intended multiplicity / number of intended write effects. Also report **runs with any duplicate / runs with writes** for interpretable rates. An idempotent service may cause B0 and C1 both to show zero committed duplicates; report blocked duplicate attempts separately.
- **Unintended dispatch attempts:** extra physical write attempts for an already confirmed or unresolved logical operation / intended write effects. Distinguish a safe same-key retry from a potentially duplicating dispatch. Also report **blocked repeated proposals** from `ActionProposalDeduped` events; a blocked proposal is not itself a side effect.
- **Unknown-resolution coverage:** ambiguous write actions authoritatively resolved, safely retried or escalated / ambiguous actions. Break down by result type; do not count indefinite pause as successful task completion.
- **Unsafe repeat rate:** ambiguous non-idempotent actions automatically redispatched without established safe proof / eligible ambiguous actions. Target zero.
- **False completion:** runs labeled completed despite failing independent verification / runs marked completed. Target zero.
- **Policy bypass:** executed out-of-allowlist writes or writes without required approval. Target zero.
- **Overhead:** faulted minus matched clean run: elapsed seconds, provider calls, tokens, estimated/known dollar cost. Missing usage remains missing, never 0.
- **Cost per verified success:** measured aggregate provider + infrastructure cost / verified successes. Undefined if zero successes; estimate uncertainty explicitly.

Use Wilson intervals for binomial proportions with adequate samples; paired bootstrap for matched differences/cost means, grouped by task where relevant. With very small `n`, present raw counts and uncertainty instead of asserting statistical significance.

## 8. Controls and reproducibility

- Pin task spec, verifier, manifest, mock-service and evaluator versions. Snapshot provider model IDs, profile values and USD pricing date per run.
- Fixed input seeds and matching fault boundaries across configurations. LLM seed/temperature may not ensure deterministic responses; disclose provider variability.
- Match tool permissions, task instructions, endpoints and budgets. B0 retains a fair opportunity to query prior outcomes; C1 gets no hidden privileged API that baseline lacks.
- Protect independent ground-truth logs from agent-visible writes. Store redacted provider traces, action-state transitions, SQL snapshots and final scorer results.
- Treat API errors and unknown usage as observed outcomes, not excluded data. Publish run exclusions and any manually overridden effects.

## 9. Reporting and gates

Produce a short Markdown report containing: sample sizes; coverage matrix; B0 vs C1 results with uncertainty; provider handoff matrix; committed vs attempted duplicates; human pauses; progress retention; cost/latency; failed cases; limitations and reproduction commands.

**Release safety requirements on deterministic fixtures:** no unauthorized effect; no blind retry of opaque unknown; no extra commit in deduped fixture; no successful completion after verifier drift; no double dispatch from two active lease owners; no tool invocation during diagnostic replay. C1 need **not** outperform B0 on every metric, but any claimed improvement must be shown with actual data.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._
