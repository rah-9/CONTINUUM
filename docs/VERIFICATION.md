# Verification strategy and results

How Continuum is tested, what each layer proves, and the latest results. The requirement → test mapping lives in [`tests/traceability.yaml`](../tests/traceability.yaml), and `tests/unit/test_traceability.py` fails if any requirement loses its tests or a referenced test disappears.

## Test layers

| Layer | Path | Kind of testing | What it proves |
|---|---|---|---|
| Unit / property | `tests/unit` | functional, hypothesis properties | spec validation, the transition table, identity and idempotency keys, canonical encoding, packet determinism, fingerprints, redaction, adapter normalization, doc/DDL sync, integration configs, traceability |
| Integration | `tests/integration` | functional, end-to-end against PostgreSQL | R1–R12 and U1–U7 flows, atomic store and fencing, gateway, reconciliation table, drift, budgets, handoff, replay, SDK/CLI/HTTP/MCP, schema constraints, lab safety gates |
| Contract | `tests/contract` | provider contract | provider-faithful OpenAI/Anthropic request rules, switching across vendors, versions and chats, error taxonomy; live providers opt-in |
| Property (stateful) | `tests/property` | model-based / randomized | random interleavings of proposals, faults, crashes, takeovers, reconciliation and human decisions keep the I1/I2/I4/I5 and no-unsafe-repeat invariants, in all three connector modes |
| Chaos | `tests/chaos` | fault injection, real processes | a crash at every write-ahead/commit boundary followed by resume; two-process lease races; `os._exit` after a remote commit; a stale owner returning; heartbeat through a slow tool |
| Non-functional | `tests/nonfunctional` | NFRs | no DB lock across I/O, secret hygiene end to end, reproducibility, parallel processes on one DB, performance baselines |
| Coding continuity | `tests/integration/test_coding.py`, `test_mcp_stdio.py`, `tests/unit/test_repo_observer.py` | functional, end-to-end over real MCP stdio | cross-model handoffs (C1–C11), content fingerprint, re-verification, redaction, shell-only agents |
| Brief quality | `tests/integration/test_brief_quality.py` | functional + size bounds | the brief stays bounded however long the session (B1), commits since the last checkpoint (B2–B3), notes reach the next model whole (B4), the code map shows where the session worked (B5) |
| Audit regression | `test_coding_hardening.py` (H1–H21), `test_core_hardening.py` (K1–K9), `test_pass2_hardening.py` (Q1–Q17) | security + robustness | every finding from three full-codebase audits stays fixed |
| Continuity scenarios | `tests/integration/test_scenarios.py` (S1–S11), `tests/unit/test_transcript.py` | end-to-end through the apps' entry points (hook payloads, transcripts, MCP-equivalent calls, CLI) | app switch with an abrupt end and no agent notes, another checkout, repeated and concurrent hooks, a hook with the database down, long sessions with recall, non-git projects, secrets in chat, setup, schema upgrade ([MEMORY-ALGORITHM.md](MEMORY-ALGORITHM.md) §5) |
| Memory quality | `continuum_lab.memory_bench`, `tests/nonfunctional::test_nf6_long_chat_memory_quality`, `tests/unit/test_memory.py` (with a Hypothesis property test) | planted-fact benchmark + invariants | what the user said anywhere in a 10–160-message chat reaches the next model: requirements, changes of mind, answers, agent decisions; no pasted noise; within budget ([MEMORY-QUALITY.md](MEMORY-QUALITY.md)) |
| Portability | `tests/integration/test_portability.py` | cross-OS behaviour | UTF-8 output on any console, launch-directory independence, Windows check-command resolution |
| Regression | everywhere | regression | every defect found has a test, see below |
| Experiment | `continuum-lab` | paired B0/C1 study | safety and progress metrics with uncertainty (`docs/lab-reports/`) |
| Real handoff trials | `eval/handoff_trials/` | real models, hidden acceptance tests | transfer quality: does the next model finish the work correctly, at what token cost ([`HANDOFF-TRIALS.md`](HANDOFF-TRIALS.md)) |

Run them:

```bash
make check                                    # ruff + mypy --strict + all pytest layers
CONTINUUM_PROPERTY_EXAMPLES=150 pytest tests/property    # deep randomized search
pytest -m nonfunctional -s                    # prints measured baselines
continuum-lab run --suite safety --out results/safety    # 912-cell experiment
CONTINUUM_LIVE=1 pytest -m live              # real providers (needs keys + profiles)
eval/handoff_trials/export_feature/judge.sh <project>    # judge a real model-switch trial
```

## Defects found by testing (all fixed, all with regression tests)

| Found by | Defect | Regression test |
|---|---|---|
| Gateway test | Mock "commit then drop" never persisted (state saved only on success) | `test_ambiguous_write_becomes_unknown_and_blocks_new_writes` |
| Reconciliation test | Lab manifests claimed 24 h dedupe retention regardless of the connector | `test_expired_dedupe_key_falls_back_to_query_then_human` |
| Lab analysis | Deduped retry while the original was still in flight was not modeled | `test_deduped_retry_while_original_still_processing_does_not_duplicate` |
| Adversarial review | An existing retryable write could dispatch while *another* effect was unresolved | `test_existing_retryable_write_is_blocked_while_another_effect_is_unresolved` |
| Provider-faithful contract tests | Dotted tool names rejected by OpenAI/Anthropic (every live run would fail) | `test_every_catalog_tool_name_is_valid_for_every_vendor` |
| Contract test | Generated tool-call ids were not paired with their results | `test_history_with_missing_call_ids_is_still_valid` |
| Drift test | Drift needing re-sent effects looped until models were exhausted | `test_artifact_drift_invalidates_milestone_and_descendants_keeps_history` |
| Registry identity | Demo tasks resumed against the wrong binding | `test_demo_tasks_show_recovery_and_human_pause` |
| Chaos test under load | **Lease rounding:** `::bigint` rounds DB time, so a lease could expire up to 1 s early (a 1 s TTL lasted ~0.1 s); reproduced deterministically, fixed with fractional time | `test_lease_ttl_is_not_shortened_by_rounding` |
| Non-functional test | Starting an existing run id leaked a raw `UniqueViolation` | `test_starting_an_existing_run_id_is_a_typed_error` |
| Coding handoff test | Running the tests wrote `__pycache__`, changing the fingerprint, so a just-passed check never counted | `test_running_tests_does_not_change_the_fingerprint` |
| Shell-agent CLI test | `checks add … --actor X -- cmd` lost `--actor` to argparse `REMAINDER` | `test_c11_shell_only_agents_use_the_same_protocol_via_cli` |
| Shell-agent CLI test | CLI hit a missing table on a database from an older version | `test_ensure_schema_migrates_only_when_needed` |
| Replay of a real transcript (v0.9.0) | 27 of Claude's 47 messages kept (only the final one per turn; one per catch-up); Claude Code's uuid-less request copies read as messages | `test_s14_both_sides_of_the_chat_are_kept_exactly_through_an_outage`, `replay_capture.py` |
| Memory benchmark (v0.9.0) | The brief kept the first and newest six messages: 15% of requirements reached the next model in 40-message chats, and answers to the agent's questions were lost | `test_nf6_long_chat_memory_quality` |
| Real 30-message trial (v0.9.0) | A second identical message in one chat ("continue", "yes") was dropped at capture | `test_s13_repeated_words_in_one_chat_are_all_kept_and_reimports_add_nothing` |
| Real 30-message trial with Haiku (v0.9.0) | The user's "stop here" to the previous agent was labelled "the current instruction", and an old open question stalled the next model | `tests/unit/test_memory.py` (`ends_chat`), rule in `brief.RULES` |
| Real 30-message chat (v0.9.0) | A line of a pasted log ("note: … must …") was taken as a requirement | `test_pasted_output_is_never_a_requirement` |
| Scenario test (v0.8.0) | An agent started in `repo/src` and one in `repo` got two different sessions; a clone at another path started from nothing | `test_s1_…`, `test_s2_…` |
| Scenario test (v0.8.0) | A key pasted into chat was stored unless it was in this process's environment | `test_s7_secrets_pasted_into_chat_are_never_stored` |
| Real handoff trial (v0.8.0) | `continuum … \| head` crashed with a `BrokenPipeError` traceback | `test_p3_cli_output_into_a_closed_pipe_is_not_a_crash` |
| Code review (v0.8.0) | The code map read an untracked file whole (a multi-GB data file into memory) | bounded scan in `repo._scan_new_file` |
| Real handoff trial (v0.7.0) | Every `continuum` in paths and commands was redacted, because the default DB password is that dictionary word | `test_dictionary_word_url_passwords_never_corrupt_ordinary_text` |
| Real handoff trial (v0.7.0) | Decision reasons and next steps were clipped at 300 characters, so the next model lost the *why* | `test_b4_decision_reasons_and_next_steps_reach_the_next_model_whole` |
| Real handoff trial (v0.7.0) | The commit id was part of the fingerprint, so every commit flagged verified work as needing re-verification | `test_committing_or_staging_identical_content_keeps_the_fingerprint` |
| CI on git 2.55 (v0.7.0) | `git add --update -- .` fails in a fresh repository with no index, breaking the content fingerprint | `test_running_tests_does_not_change_the_fingerprint` |
| Wheel-install + second audit (v0.6.3) | Windows pipe crash; IDE launch dir; PEP 668 cloud install; config placeholders; path escape and NUL crashing the step loop; NUL in journal text; submodule and non-UTF-8 fingerprint blind spots; cost precision breaking I2; abort rewriting terminal runs; checks seeing DB credentials, surviving timeouts, failing on `.cmd`; colliding owner ids; active-time undercount; config/CLI/litellm crashes; unredacted verifier messages | `tests/integration/test_pass2_hardening.py` (Q1–Q17), `tests/integration/test_portability.py` |
| Core audit (v0.6.2) | Transient verifier error treated as drift (run failed); one process re-entered a live lease; DNS rebinding; orphan run on a bad model alias; raw tracebacks from config/MCP/CLI; zero registry cost recorded as $0; secrets in rejection reasons; malformed provider responses; unvalidated blob refs | `tests/integration/test_core_hardening.py` (K1–K9) |
| Full-codebase audit (v0.6.1) | 23 coding-layer issues: fail-open git fingerprint, diff drivers, no-HEAD staged changes, orphan sessions, closed-session check runs, `human:` spoofing, brief forging, schema downgrade, migration race, CLI `--`, rename parsing and others | `tests/integration/test_coding_hardening.py` (H1–H21) |

## Latest results (2026-10-19)

- `make check`: ruff clean, `mypy --strict` clean. **362 passed**, 5 live tests skipped (opt-in). CI runs the same gates against PostgreSQL 16.
- **Hardened schema:** the whole suite, including chaos, runs against the new CHECK constraints with zero violations. The runtime never writes a state the database considers invalid.
- **Property search:** 150 random interleavings × 12 steps per connector mode (450 scenarios). All invariants held at every step.
- **Non-functional baselines** (this container, PostgreSQL 16, fake models):
  - about 450 fenced event+projection appends/s
  - end-to-end task p50 ≈ 130 ms
  - 6 parallel processes complete in ≈ 1.6 s
  - no DB lock held during any tool or model call
  - a secret echoed by a model and a tool never reached events, blobs or logs
- **Lab safety matrix** (912 paired cells, rerun on schema v2, identical to before):
  - Continuum: **0** committed duplicates (baseline 140)
  - **0** false completions and **0** policy bypasses
  - **100%** preserved progress (baseline 73%)
  - **222/222** ambiguous actions resolved or escalated
  - clean completion 0.78 vs 0.72, paired +0.057 [+0.011, +0.103]
  - full report: `docs/lab-reports/`

## Transfer quality with real models (v0.7.0)

The goal is that the next model finishes the work correctly **without re-reading the codebase or re-paying for it**. Sonnet → Opus on a copy of this repository; model A built and committed Part 1, two requirements existed only in A's chat, model B was told only *"continue where we left off"*. Work tokens are total tokens minus the ~54k any agent spends before doing anything ([method and raw numbers](HANDOFF-TRIALS.md)):

| Model B (Opus) | hidden acceptance tests | chat-only requirements | work tokens | files read |
|---|---|---|---|---|
| Continuum, brief without code map | **4/4** | 2/2 | ~17.3k | 4 |
| Continuum, brief with code map | **4/4** | 2/2 | **~13.8k** | **3** |
| baseline, run 1 | 1/4 | 0/2 | ~21.3k | 12+ |
| baseline, run 2 | 1/4 | 0/2 | ~29.4k | 14+ |

| **App switch after an abrupt end** (Trial 4): A never used Continuum, B in a Codex-style setup | **4/4** | **2/2** | not measured (cost $0.56, 1.8 min) | 16 tool calls |
| baseline (Trial 4) | 1/4 | 0/2 | not measured (cost $0.63, 5.1 min) | 20 tool calls |
| **30-message chat** (Trial 5), B = Haiku / Sonnet / Opus | **13/13** each (Haiku in 2 runs) | **4/4** export requirements from messages 8–27 | brief ~2.1k tokens vs ~135k of history in A's chat | — |
| baseline (Trial 5), Haiku / Sonnet / Opus | 9/13 each | 0/4 | — | — |

The baseline never learned Part 2 existed; reaching a correct result would cost another explanation turn plus the work (~35–45k work tokens). The code map cut Continuum's work tokens by about 20%. Continuum itself calls no LLM, so these are the only model costs.

## Measured performance and context size (v0.7.0)

20,000-file git repo, 200 changed/untracked files, a session with 30 notes and 15 checkpoints:

| | v0.6.3 | v0.7.0 |
|---|---|---|
| fingerprint (`observe`) | 120–170 ms, 7 git calls; commit id included (false "changed" after every commit) | ~165 ms, **5 git calls** (asserted by `test_nf5_coding_observation_cost`); content tree, so commits are not changes |
| brief sent over MCP | ~8.3k tokens (JSON + duplicate Markdown) | **~1.3k tokens typical, < 4k worst case** (compact Markdown, notes whole, asserted by B1) |
| full JSON brief (`detail="full"`) | grows with the session | < 10k tokens and does not grow when the session doubles (B1) |
| where to start | the next model explores the repo | code map: files changed since the session began, +/- lines, enclosing functions, definitions (B5) |

A full fenced runtime run (`inventory_upload_v1`, fake models) issues 271 SQL statements for 37 events in about 300 ms.

## Coding continuity (v0.6.0 – v0.7.0)

`tests/integration/test_coding.py` C1–C11, `test_brief_quality.py` B1–B5 and `test_mcp_stdio.py` cover:
- Opus 4.8 → 5.5 and GPT-5.6 → 6.1 handoffs, in new processes and over real MCP stdio between separate server processes
- re-verification after an edit or a check-definition change
- failing, timed-out and unregistered checks
- unverified claims
- secret redaction
- immutability and closed sessions
- concurrent gapless appends
- non-git projects
- shell-only agents via the CLI
- commit and stage without false re-verification (content fingerprint)
- bounded, compact briefs with omission counts and the session code map

## Known limits of this evidence

- Fake and emulated providers prove protocol and safety properties, not a live model's reasoning quality. Live runs need verified profiles and keys.
- Mock services prove the **modeled** connector contracts. A real connector needs its own contract test before its manifest is trusted.
- Performance numbers are single-host baselines for regression detection, not capacity claims.
- The real handoff trials are small (four Opus runs on one task, Sonnet → Opus). They show a clear direction, not a statistical result. A GPT → Claude trial is packaged in `eval/handoff_trials/` but has not run yet: it needs an OpenAI key and network access in the trial environment.
