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
| Regression | everywhere | regression | every defect found has a test, see below |
| Experiment | `continuum-lab` | paired B0/C1 study | safety and progress metrics with uncertainty (`docs/lab-reports/`) |

Run them:

```bash
make check                                    # ruff + mypy --strict + all pytest layers
CONTINUUM_PROPERTY_EXAMPLES=150 pytest tests/property    # deep randomized search
pytest -m nonfunctional -s                    # prints measured baselines
continuum-lab run --suite safety --out results/safety    # 912-cell experiment
CONTINUUM_LIVE=1 pytest -m live              # real providers (needs keys + profiles)
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

## Latest results (2026-10-13)

- `make check`: ruff clean, `mypy --strict` clean. All layers pass; 5 live tests are skipped (opt-in).
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

## Coding continuity (v0.6.0)

`tests/integration/test_coding.py` C1–C11 and `test_mcp_stdio.py` cover:
- Opus 4.8 → 5.5 and GPT-5.6 → 6.1 handoffs, in new processes and over real MCP stdio between separate server processes
- re-verification after an edit or a check-definition change
- failing, timed-out and unregistered checks
- unverified claims
- secret redaction
- immutability and closed sessions
- concurrent gapless appends
- non-git projects
- shell-only agents via the CLI

## Known limits of this evidence

- Fake and emulated providers prove protocol and safety properties, not a live model's reasoning quality. Live runs need verified profiles and keys.
- Mock services prove the **modeled** connector contracts. A real connector needs its own contract test before its manifest is trusted.
- Performance numbers are single-host baselines for regression detection, not capacity claims.
